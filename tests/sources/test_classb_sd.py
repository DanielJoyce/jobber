"""South Dakota state employer careers: HireClick job board (JSON list, HTML detail).

Fixtures are real responses captured 2026-10-09 (list trimmed to five postings, two details). The
list endpoint returns its JSON document as a JSON string, so the fixture is double-encoded too.
"""

from __future__ import annotations

import httpx
import pytest
from classb_support import SINCE, assert_enabled_class_b, fixture
from hc_support import Site, make_ctx, registry_row

from jobhunter.core import db
from jobhunter.core.fetch import reset_shared_state
from jobhunter.core.models import Query
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter

ROBOTS = "User-agent: *\nCrawl-delay: 10\n\nUser-agent: GPTBot\nDisallow: /\n"
LIST_PATH = "/api/Controllers/JobBoard/GetSettingsAndJobs"
DETAILS = {"242857": "detail_1.html", "249150": "detail_2.html"}


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


def _site() -> Site:
    search = fixture("SD", "search.json")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == LIST_PATH:
            return httpx.Response(200, text=search, headers={"content-type": "application/json"})
        job_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if request.url.path.startswith("/jb/") and job_id in DETAILS:
            return httpx.Response(
                200, text=fixture("SD", DETAILS[job_id]), headers={"content-type": "text/html"}
            )
        return httpx.Response(404)

    return Site(route, robots=ROBOTS)


def test_row_is_an_enabled_class_b_http_row_honouring_crawl_delay():
    assert_enabled_class_b("SD", "http")
    assert registry_row("sd-employer").rate_limit.rps == 0.1  # robots Crawl-delay: 10


def test_search_reads_the_double_encoded_list(conn, tmp_path):
    src = registry_row("sd-employer")
    s = _site()
    ctx = make_ctx(src, conn, s, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))

    (req,) = s.requests  # one request lists every posting; there is no query and no paging
    assert req.url.host == "statesdgovt.hireclick.com" and req.url.path == LIST_PATH
    assert req.url.params["orgGUID"] == "16343DEB-35A1-4A0B-9922-A7FC866656E9"
    assert len(stubs) == 5
    first = stubs[0]
    assert first.source_key == "sd-employer"
    assert first.external_id == "242857"
    assert first.url == "https://statesdgovt.hireclick.com/jb/dci-special-agent-i-or-ii/view/242857"
    assert first.title == "DCI Special Agent I or II"
    assert first.needs_resolve is True and first.posted_at is None
    assert first.locations[0].state == "SD"


def test_resolve_reads_dates_agency_and_salary_from_the_detail_page(conn, tmp_path):
    src = registry_row("sd-employer")
    s = _site()
    ctx = make_ctx(src, conn, s, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))

    detail = HtmlConfigAdapter().resolve(stubs[0], ctx)
    assert detail.title == "DCI Special Agent I or II"
    assert detail.description_raw and "<br" in detail.description_raw
    assert detail.posted_at is not None and detail.posted_at.tzinfo is not None
    assert detail.posted_at.year == 2026
    assert detail.agency_raw == "Attorney General, Division of Criminal Investigation"
    assert detail.salary_raw and detail.salary_raw.startswith("$33.48")
    assert detail.closes_at is None  # "Open Until Filled"
    assert detail.needs_resolve is False
    assert "@" not in detail.description_raw.replace("hr@example.gov", "")

    second = HtmlConfigAdapter().resolve(stubs[1], ctx)
    assert second.agency_raw == "Department of Education"
    assert second.salary_raw and second.salary_raw.startswith("$79,200")
