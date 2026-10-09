"""NLx adapter tests. httpx.MockTransport serving saved fixtures only: no network."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext, RobotsDisallowed, reset_shared_state
from jobhunter.core.models import JobStub, Query, SourceRow
from jobhunter.pipeline.runner import ADAPTERS
from jobhunter.sources.adapters.nlx import (
    NlxAdapter,
    build_params,
    markdown_to_html,
    query_string,
    slugify,
)
from jobhunter.sources.registry import load_registry

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "nlx"
SEARCH_HOST = "prod-search-api.jobsyn.org"
DETAIL_HOST = "microsites.dejobs.org"
SINCE = datetime(2026, 10, 1, tzinfo=UTC)


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture(autouse=True)
def _state():
    reset_shared_state()
    yield
    reset_shared_state()


def row(key: str) -> SourceRow:
    return next(r for r in load_registry() if r.key == key)


class Api:
    """Serves the NLx robots.txt on every host, search pages by offset, details by GUID."""

    def __init__(self, pages: dict[int, dict] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.robots = (FIX / "robots_usnlx.txt").read_text()
        self.pages = pages or {
            0: fixture("search_national_p1.json"),
            15: fixture("search_national_p2.json"),
        }
        self.details = {}
        for name in (
            "detail_national_1.json",
            "detail_national_2.json",
            "detail_state_1.json",
            "detail_state_2.json",
        ):
            d = fixture(name)
            self.details[d["guid"]] = d

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if request.url.host == SEARCH_HOST:
            offset = int(request.url.params.get("offset", "0"))
            page = self.pages.get(offset)
            if page is None:
                return httpx.Response(200, json={"jobs": [], "pagination": {}})
            return httpx.Response(200, json=page)
        if request.url.host == DETAIL_HOST:
            guid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            return httpx.Response(200, json=self.details[guid])
        return httpx.Response(404)

    def searches(self) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.url.host == SEARCH_HOST and r.url.path != "/robots.txt"
        ]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def make_ctx(src: SourceRow, conn, api: Api, tmp_path) -> FetchContext:
    clock = Clock()
    settings = Settings.model_validate({"paths": {"cache_dir": str(tmp_path / "cache")}})
    return FetchContext(
        src,
        settings,
        conn,
        transport=httpx.MockTransport(api.handle),
        clock=clock,
        sleep=clock.sleep,
    )


def run(src, ctx, query=None, since=SINCE):
    q = query or Query(title="software engineer")
    return list(NlxAdapter().search(src, q, since, ctx))


# ─── query mapping ─────────────────────────────────────────────────────────


def test_query_string_quotes_multiword_terms():
    assert query_string(Query(title="Software Engineer")) == '"Software Engineer"'
    assert query_string(Query(keywords=["rust", "site reliability"])) == 'rust "site reliability"'
    assert query_string(Query(title="SRE", keywords=["platform engineer"])) == (
        'SRE "platform engineer"'
    )


def test_national_row_has_no_state_filter():
    p = build_params(row("us-nlx"), Query(title="software engineer"), 30, 15)
    assert p == {
        "q": '"software engineer"',
        "sort": "date",
        "num_items": 15,
        "offset": 30,
        "page": 3,
    }


@pytest.mark.parametrize(
    ("key", "location"),
    [("ky-kyjobs", "kentucky"), ("mt-montanaworks", "montana"), ("ny-newyork", "new-york")],
)
def test_state_rows_filter_to_their_state(key, location):
    p = build_params(row(key), Query(keywords=["rust"]), 0, 15)
    assert p["location"] == location
    assert p["q"] == "rust"


def test_registry_rows_point_at_nlx_hosts():
    keys = ("us-nlx", "ky-kyjobs", "mt-montanaworks", "ny-newyork")
    origins = {k: row(k).config["x_origin"] for k in keys}
    assert origins == {
        "us-nlx": "usnlx.com",
        "ky-kyjobs": "kyjobs.usnlx.com",
        "mt-montanaworks": "montana.usnlx.com",
        "ny-newyork": "myjobsny.usnlx.com",
    }
    assert ADAPTERS["nlx"] is NlxAdapter


def test_search_sends_params_and_x_origin(conn, tmp_path):
    src = row("ky-kyjobs")
    api = Api(pages={0: fixture("search_state_ky.json")})
    run(src, make_ctx(src, conn, api, tmp_path), since=datetime(2026, 10, 9, 11, tzinfo=UTC))
    req = api.searches()[0]
    assert req.url.path == "/api/v1/solr/search"
    assert req.headers["X-Origin"] == "kyjobs.usnlx.com"
    assert dict(req.url.params) == {
        "q": '"software engineer"',
        "location": "kentucky",
        "sort": "date",
        "num_items": "15",
        "offset": "0",
        "page": "1",
    }


def test_national_search_sends_no_location(conn, tmp_path):
    src = row("us-nlx")
    api = Api()
    run(src, make_ctx(src, conn, api, tmp_path))
    assert all("location" not in r.url.params for r in api.searches())
    assert {r.headers["X-Origin"] for r in api.searches()} == {"usnlx.com"}


# ─── pagination and watermark ──────────────────────────────────────────────


def test_paginates_by_offset_until_has_more_pages_is_false(conn, tmp_path):
    src = row("us-nlx")
    p2 = fixture("search_national_p2.json")
    p2["pagination"]["has_more_pages"] = False
    api = Api(pages={0: fixture("search_national_p1.json"), 15: p2})
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert [r.url.params["offset"] for r in api.searches()] == ["0", "15"]
    assert len(stubs) == 30
    assert len({s.external_id for s in stubs}) == 30


def test_watermark_stops_after_a_page_entirely_older_than_since(conn, tmp_path):
    src = row("us-nlx")
    p2 = fixture("search_national_p2.json")
    for j in p2["jobs"]:
        j["date_new"] = "2026-09-20T00:00:00Z"
    p3 = copy.deepcopy(p2)
    api = Api(pages={0: fixture("search_national_p1.json"), 15: p2, 30: p3})
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert len(stubs) == 15
    assert [r.url.params["offset"] for r in api.searches()] == ["0", "15"]


def test_watermark_skips_stale_rows_inside_a_page(conn, tmp_path):
    src = row("us-nlx")
    p1 = fixture("search_national_p1.json")
    p1["jobs"][3]["date_new"] = "2026-09-01T00:00:00Z"
    p1["pagination"]["has_more_pages"] = False
    api = Api(pages={0: p1})
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert len(stubs) == 14
    assert p1["jobs"][3]["guid"] not in {s.external_id for s in stubs}


def test_max_pages_from_registry_bounds_the_walk(conn, tmp_path):
    src = row("us-nlx").model_copy(update={"pagination": {"kind": "offset", "max_pages": 1}})
    api = Api()
    run(src, make_ctx(src, conn, api, tmp_path))
    assert len(api.searches()) == 1


def test_offset_steps_by_the_page_size_the_site_returns(conn, tmp_path):
    src = row("mt-montanaworks")
    p1 = fixture("search_national_p1.json")
    p1["jobs"] = p1["jobs"][:10]
    p1["pagination"]["page_size"] = 10
    p2 = fixture("search_national_p2.json")
    p2["pagination"]["has_more_pages"] = False
    api = Api(pages={0: p1, 10: p2})
    run(src, make_ctx(src, conn, api, tmp_path))
    assert [r.url.params["offset"] for r in api.searches()] == ["0", "10"]


# ─── stub mapping ──────────────────────────────────────────────────────────


def test_stub_fields(conn, tmp_path):
    src = row("ky-kyjobs")
    api = Api(pages={0: fixture("search_state_ky.json")})
    stubs = run(src, make_ctx(src, conn, api, tmp_path), since=datetime(2026, 10, 9, tzinfo=UTC))
    s = next(x for x in stubs if x.external_id == "03DCC972F85148D7ACF0CF8BB8FC276A")
    assert s.source_key == "ky-kyjobs"
    assert s.title == "Staff Software Engineer"
    assert s.agency_raw
    assert s.location_raw == "Lexington, KY"
    assert s.posted_at == datetime(2026, 10, 9, 11, 52, 59, tzinfo=UTC)
    assert s.url == (
        "https://kyjobs.usnlx.com/lexington-ky/staff-software-engineer/"
        "03DCC972F85148D7ACF0CF8BB8FC276A/job/"
    )
    # The listing carries the full description, so no resolve is needed.
    assert s.needs_resolve is False
    assert s.description_raw and s.description_raw.startswith("<")
    # "other.application_link" is the employer's own application link.
    assert s.apply_url == "https://ipc.us/t/62D4EFB0B1114CCC"
    assert s.locations[0].state == "KY"
    assert s.locations[0].city == "Lexington"
    assert s.locations[0].lat is not None


def test_stub_without_direct_link_uses_the_apply_redirector(conn, tmp_path):
    src = row("us-nlx")
    api = Api()
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    s = next(x for x in stubs if x.external_id == "4D20959F66924549AA751B6D6794F8E2")
    assert s.apply_url == "https://nlx.jobsyn.org/4D20959F66924549AA751B6D6794F8E21"
    assert s.url.startswith("https://usnlx.com/lincoln-ne/")


def test_stub_without_description_needs_resolve(conn, tmp_path):
    src = row("us-nlx")
    p1 = fixture("search_national_p1.json")
    p1["jobs"][0]["description"] = ""
    p1["pagination"]["has_more_pages"] = False
    api = Api(pages={0: p1})
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert stubs[0].needs_resolve is True
    assert stubs[0].description_raw is None
    assert all(not s.needs_resolve for s in stubs[1:])


def test_markdown_to_html_blocks():
    html = markdown_to_html("### Pay\n**$20** an hour\n\n- one\n- two\n\nplain <b>")
    assert html == (
        "<h3>Pay</h3><p>$20 an hour</p><ul><li>one</li><li>two</li></ul><p>plain &lt;b&gt;</p>"
    )


def test_slugify_matches_site_rule():
    assert slugify("New York City, NY") == "new-york-city-ny"
    assert slugify("Coeur d\u2019Alene, ID") == "coeur-dalene-id"
    assert slugify("New York") == "new-york"


# ─── resolve ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "guid", "fix"),
    [
        ("us-nlx", "1690B673AC014890B022A02DFA32FF7E", "detail_national_2.json"),
        ("ky-kyjobs", "8FEF54D4954946E1B08CDE84235E49AE", "detail_state_2.json"),
    ],
)
def test_resolve_parses_description_and_apply_link(conn, tmp_path, key, guid, fix):
    src = row(key)
    api = Api()
    ctx = make_ctx(src, conn, api, tmp_path)
    stub = JobStub(
        source_key=key,
        external_id=guid,
        title="x",
        url=f"https://{src.config['x_origin']}/x/x/{guid}/job/",
        extra={"guid": guid},
    )
    detail = NlxAdapter().resolve(stub, ctx)
    d = fixture(fix)
    assert [r.url.path for r in api.requests if r.url.host == DETAIL_HOST][-1] == (
        f"/ALL_JOBS/{guid}.json"
    )
    assert detail.description_raw == d["html_description"]
    assert "<p>" in detail.description_raw
    assert detail.apply_url == d["link"]
    assert detail.title == d["title_exact"]
    assert detail.agency_raw == d["company_exact"]
    assert detail.needs_resolve is False
    assert detail.closes_at is None


def test_resolve_prefers_direct_application_link_and_reads_deleted_at(conn, tmp_path):
    src = row("ky-kyjobs")
    api = Api()
    guid = "03DCC972F85148D7ACF0CF8BB8FC276A"
    api.details[guid]["deleted_at"] = "2026-10-20T00:00:00Z"
    stub = JobStub(source_key=src.key, external_id=guid, title="x", url="https://kyjobs.usnlx.com/")
    detail = NlxAdapter().resolve(stub, make_ctx(src, conn, api, tmp_path))
    assert detail.apply_url == "https://ipc.us/t/62D4EFB0B1114CCC"
    assert detail.closes_at == datetime(2026, 10, 20, tzinfo=UTC)


# ─── robots ────────────────────────────────────────────────────────────────


def test_feed_paths_are_never_requested(conn, tmp_path):
    src = row("us-nlx")
    api = Api()
    ctx = make_ctx(src, conn, api, tmp_path)
    run(src, ctx)
    assert not any("feed" in r.url.path for r in api.requests)
    # robots.txt disallows /*feed/ and /*feeds/; a feed URL is refused before any I/O.
    before = len(api.requests)
    with pytest.raises(RobotsDisallowed):
        ctx.get("https://usnlx.com/jobs/feeds/rss?q=software")
    paths = [r.url.path for r in api.requests[before:]]
    assert paths == ["/robots.txt"]
