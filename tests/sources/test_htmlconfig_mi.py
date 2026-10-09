"""Michigan Talent Bank (jobs.mitalent.org): WebForms form search, __doPostBack paging, detail.

Fixtures were captured 2026-10-09 (tests/fixtures/htmlconfig/mi), trimmed and scrubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from hc_support import SINCE, Site, fixture_text, make_ctx, registry_row

from jobhunter.core import db
from jobhunter.core.fetch import reset_shared_state
from jobhunter.core.models import Query
from jobhunter.pipeline.runner import ADAPTERS
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter

BOARD = "mi"
KEY = "mi-mitalent"
KW = "ctl00$ctl00$MainContent$MainContentSub$"


@pytest.fixture(autouse=True)
def _state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


class MichiganSite(Site):
    def __init__(self, robots: str | None = None) -> None:
        super().__init__(
            self.route, robots if robots is not None else fixture_text(BOARD, "robots.txt")
        )
        self.posts: list[dict[str, list[str]]] = []

    def route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/job-search":
            return httpx.Response(200, text=fixture_text(BOARD, "search_form.html"))
        if request.method == "POST" and path == "/job-search":
            self.posts.append(parse_qs(request.content.decode(), keep_blank_values=True))
            return httpx.Response(302, headers={"location": "/job-seeker/jobsearch-results/"})
        if request.method == "GET" and path == "/job-seeker/jobsearch-results/":
            return httpx.Response(200, text=fixture_text(BOARD, "results_p1.html"))
        if request.method == "POST" and path == "/job-seeker/jobsearch-results/":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            self.posts.append(form)
            if form.get("__EVENTTARGET") == ["ctl00$MainContent$pageTop2"]:
                return httpx.Response(200, text=fixture_text(BOARD, "results_p2.html"))
            return httpx.Response(200, text="<html><body>no more</body></html>")
        if path.startswith("/job-seeker/job-details/JobCode/"):
            code = path.rsplit("/", 1)[-1]
            name = "detail_1.html" if code == "1693578" else "detail_2.html"
            return httpx.Response(200, text=fixture_text(BOARD, name))
        return httpx.Response(404)


def run(site, conn, tmp_path, query=None, since=SINCE, **row):
    src = registry_row(KEY, **row)
    ctx = make_ctx(src, conn, site, tmp_path)
    return (
        src,
        ctx,
        list(HtmlConfigAdapter().search(src, query or Query(keywords=["engineer"]), since, ctx)),
    )


def test_family_is_registered_and_row_is_enabled():
    assert ADAPTERS["htmlconfig"] is HtmlConfigAdapter
    src = registry_row(KEY)
    assert src.family == "htmlconfig"
    assert src.policy.value == "enabled"


def test_search_posts_the_form_with_hidden_state_and_query(conn, tmp_path):
    site = MichiganSite()
    run(site, conn, tmp_path, Query(keywords=["engineer"], posted_within_days=5))
    first = site.posts[0]
    # The WebForms state fields from the GET were carried into the POST.
    assert first["__VIEWSTATE"] == ["VS"]
    assert first[KW + "job_keywords"] == ["engineer"]
    assert first[KW + "job_time_range"] == ["7"]  # 5 days rounds up to the 1 week option
    assert first[KW + "submitBtn2"] == ["Search"]
    assert site.paths()[:3] == [
        "GET /job-search",
        "POST /job-search",
        "GET /job-seeker/jobsearch-results/",
    ]


def test_title_beats_keywords_in_the_single_search_box(conn, tmp_path):
    site = MichiganSite()
    run(site, conn, tmp_path, Query(title="data engineer", keywords=["python"]))
    assert site.posts[0][KW + "job_keywords"] == ["data engineer"]
    assert site.posts[0][KW + "job_time_range"] == ["30"]  # no age limit: the default window


def test_rows_become_stubs(conn, tmp_path):
    site = MichiganSite()
    _, _, stubs = run(site, conn, tmp_path, since=datetime(2026, 1, 1, tzinfo=UTC))
    first = stubs[0]
    assert first.source_key == KEY
    assert first.external_id.isdigit()
    assert (
        first.url == f"https://jobs.mitalent.org/job-seeker/job-details/JobCode/{first.external_id}"
    )
    assert first.title
    assert first.agency_raw
    assert first.posted_at is not None and first.posted_at.year == 2026
    assert first.location_raw and ", " in first.location_raw
    assert first.locations and first.locations[0].state == "MI"
    # The listing carries only a snippet, so the full posting is fetched later.
    assert first.needs_resolve is True
    assert first.description_raw is None


def test_pages_with_dopostback_until_max_pages_or_a_missing_link(conn, tmp_path):
    site = MichiganSite()
    # The fixtures are trimmed to 6 and 3 rows, so pretend the site's page size is 6.
    pagination = {**registry_row(KEY).pagination, "page_size": 6}
    _, _, stubs = run(
        site, conn, tmp_path, since=datetime(2026, 1, 1, tzinfo=UTC), pagination=pagination
    )
    # Page 2 is short, so page 3 is never requested.
    assert len(stubs) == 9
    assert len(site.posts) == 2
    targets = [p.get("__EVENTTARGET") for p in site.posts[1:]]
    assert targets[0] == ["ctl00$MainContent$pageTop2"]
    assert len({s.external_id for s in stubs}) == 9


def test_watermark_filters_old_rows(conn, tmp_path):
    # Every fixture row was posted 10/8/2026 (midnight Detroit = 04:00 UTC).
    _, _, kept = run(MichiganSite(), conn, tmp_path, since=datetime(2026, 10, 8, tzinfo=UTC))
    assert len(kept) >= 6
    _, _, none = run(MichiganSite(), conn, tmp_path, since=datetime(2026, 10, 9, tzinfo=UTC))
    assert none == []


def test_resolve_reads_detail_page(conn, tmp_path):
    site = MichiganSite()
    _, ctx, stubs = run(site, conn, tmp_path, since=datetime(2026, 1, 1, tzinfo=UTC))
    stub = stubs[0].model_copy(
        update={"url": "https://jobs.mitalent.org/job-seeker/job-details/JobCode/1693578"}
    )
    d = HtmlConfigAdapter().resolve(stub, ctx)
    assert d.title == "Project Manager"
    assert d.agency_raw == "Franchino Mold & Engineering"
    assert d.location_raw == "Lansing, Michigan 48906"
    assert d.locations[0].city == "Lansing" and d.locations[0].state == "MI"
    assert d.posted_at == datetime(2026, 10, 7, 4, 0, tzinfo=UTC)  # midnight Detroit (EDT)
    assert d.closes_at == datetime(2026, 11, 7, 5, 0, tzinfo=UTC)  # 11/7 midnight EST
    assert d.apply_url == "https://franchino.com/jobs/"
    assert d.description_raw and "<p>" in d.description_raw
    assert d.needs_resolve is False
    assert d.extra["description_format"] == "html"


def test_resolve_second_detail_without_apply_link_keeps_none(conn, tmp_path):
    site = MichiganSite()
    _, ctx, stubs = run(site, conn, tmp_path, since=datetime(2026, 1, 1, tzinfo=UTC))
    d = HtmlConfigAdapter().resolve(stubs[0], ctx)
    assert d.title and d.description_raw
    assert d.closes_at is not None
    assert d.external_id == stubs[0].external_id


def test_robots_disallow_stops_the_search(conn, tmp_path):
    from jobhunter.core.fetch import RobotsDisallowed

    site = MichiganSite(robots="User-agent: *\nDisallow: /\n")
    with pytest.raises(RobotsDisallowed):
        run(site, conn, tmp_path)
    assert site.requests == []
