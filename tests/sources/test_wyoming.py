"""Wyoming (hire.wyo.gov) adapter tests. MockTransport serving saved fixtures: no network."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext, RobotsDisallowed, reset_shared_state
from jobhunter.core.models import EmploymentType, JobStub, Query, SourceRow
from jobhunter.pipeline.runner import ADAPTERS
from jobhunter.sources.adapters.wyoming import (
    WyomingAdapter,
    build_params,
    search_text,
)
from jobhunter.sources.registry import load_registry

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "wyoming"
HOST = "hire.wyo.gov"
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
ALLOW_ALL = "User-agent: *\nAllow: /\n"
# What the host really serves at /robots.txt: the Next.js HTML shell, not a robots file.
HTML_SHELL = (
    '<!DOCTYPE html><html lang="en"><head></head><body><div id="__next"></div></body></html>'
)


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


def row() -> SourceRow:
    return next(r for r in load_registry() if r.key == "wy-hire")


class Api:
    """Serves robots.txt, search pages keyed by ``skip``, and details by job id."""

    def __init__(self, pages: dict[int, dict] | None = None, robots: str = ALLOW_ALL) -> None:
        self.requests: list[httpx.Request] = []
        self.robots = robots
        self.pages = pages if pages is not None else {0: fixture("search_p1.json")}
        self.details = {}
        for name in ("detail_nlx.json", "detail_internal.json"):
            d = fixture(name)
            self.details[d["data"]["_id"]] = d

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if path == "/employer-api/api/jobs/search":
            page = self.pages.get(int(request.url.params["skip"]))
            if page is None:
                return httpx.Response(200, json={"statusCode": 200, "data": [], "totalRecords": 0})
            return httpx.Response(200, json=page)
        if path.startswith("/employer-api/api/jobs/search/"):
            return httpx.Response(200, json=self.details[path.rsplit("/", 1)[-1]])
        return httpx.Response(404)

    def searches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/employer-api/api/jobs/search"]


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
    return list(WyomingAdapter().search(src, query or Query(title="software engineer"), since, ctx))


def paged(total: int = 10) -> dict[int, dict]:
    p1, p2 = fixture("search_p1.json"), fixture("search_p2.json")
    p1["totalRecords"] = p2["totalRecords"] = total
    return {0: p1, 5: p2}


# ─── registry and wiring ───────────────────────────────────────────────────


def test_registry_row_and_runner_wiring():
    src = row()
    assert src.family == "wyo"
    assert src.tier == "api"
    assert src.config["api_base"] == "https://hire.wyo.gov/employer-api/api"
    assert ADAPTERS["wyo"] is WyomingAdapter


# ─── query mapping ─────────────────────────────────────────────────────────


def test_search_text_joins_title_and_keywords():
    assert search_text(Query(title="Software Engineer")) == "Software Engineer"
    assert search_text(Query(title="SRE", keywords=["python", " "])) == "SRE python"
    assert search_text(Query(keywords=["rust"])) == "rust"
    assert search_text(Query()) == ""


def test_build_params_newest_first_with_post_date_floor():
    p = build_params(row(), Query(title="nurse"), datetime(2026, 10, 2, tzinfo=UTC), 60, 30)
    assert p == {
        "skip": 60,
        "limit": 30,
        "sort": "desc",
        "searchText": "nurse",
        "postDate": "10/02/2026",
    }


def test_build_params_omits_empty_query_and_ancient_watermark():
    p = build_params(row(), Query(), datetime(1970, 1, 1, tzinfo=UTC), 0, 30)
    assert p == {"skip": 0, "limit": 30, "sort": "desc"}


def test_config_location_and_job_source_are_forwarded():
    src = row().model_copy(update={"config": {"location": "Casper", "job_source": "internal"}})
    p = build_params(src, Query(title="x"), SINCE, 0, 10)
    assert p["location"] == "Casper"
    assert p["jobSource"] == "internal"


def test_search_request_shape(conn, tmp_path):
    src = row()
    api = Api()
    run(src, make_ctx(src, conn, api, tmp_path))
    req = api.searches()[0]
    assert req.url.host == HOST
    assert dict(req.url.params) == {
        "skip": "0",
        "limit": "30",
        "sort": "desc",
        "searchText": "software engineer",
        "postDate": "10/01/2026",
    }
    assert req.headers["Accept"] == "application/json"


# ─── paging and watermark ──────────────────────────────────────────────────


def test_pages_by_skip_until_total_records_reached(conn, tmp_path):
    src = row().model_copy(update={"config": {"page_size": 5}})
    api = Api(pages=paged(total=10))
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert [r.url.params["skip"] for r in api.searches()] == ["0", "5"]
    assert [r.url.params["limit"] for r in api.searches()] == ["5", "5"]
    assert len(stubs) == 10
    assert len({s.external_id for s in stubs}) == 10


def test_stops_when_a_short_page_exhausts_total_records(conn, tmp_path):
    src = row().model_copy(update={"config": {"page_size": 5}})
    api = Api(pages=paged(total=5))
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert len(api.searches()) == 1
    assert len(stubs) == 5


def test_watermark_stops_after_a_page_entirely_older_than_since(conn, tmp_path):
    src = row().model_copy(update={"config": {"page_size": 5}})
    pages = paged(total=15)
    for j in pages[5]["data"]:
        j["subValues"]["createdAt"] = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
    pages[10] = pages[5]
    api = Api(pages=pages)
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert len(stubs) == 5
    assert [r.url.params["skip"] for r in api.searches()] == ["0", "5"]


def test_watermark_skips_stale_rows_inside_a_page(conn, tmp_path):
    src = row()
    pages = paged(total=5)
    pages[0]["data"][2]["subValues"]["createdAt"] = int(
        datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000
    )
    stale_id = pages[0]["data"][2]["_id"]
    api = Api(pages={0: pages[0]})
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert len(stubs) == 4
    assert stale_id not in {s.external_id for s in stubs}


def test_hidden_jobs_are_skipped(conn, tmp_path):
    src = row()
    page = fixture("search_p1.json")
    page["data"][0]["isHidden"] = True
    api = Api(pages={0: page})
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert page["data"][0]["_id"] not in {s.external_id for s in stubs}


def test_max_pages_from_registry_bounds_the_walk(conn, tmp_path):
    src = row().model_copy(
        update={"config": {"page_size": 5}, "pagination": {"kind": "offset", "max_pages": 1}}
    )
    api = Api(pages=paged(total=100))
    run(src, make_ctx(src, conn, api, tmp_path))
    assert len(api.searches()) == 1


# ─── stub mapping ──────────────────────────────────────────────────────────


def test_nlx_fed_stub_fields(conn, tmp_path):
    src = row()
    api = Api()
    stubs = {s.external_id: s for s in run(src, make_ctx(src, conn, api, tmp_path))}
    s = stubs["6a9e0cd5f2f993021845331e"]
    assert s.source_key == "wy-hire"
    assert s.title == "GM (General Manager)"
    assert s.url == "https://hire.wyo.gov/job/6a9e0cd5f2f993021845331e"
    assert s.location_raw == "Casper, WY"
    assert s.locations[0].city == "Casper"
    assert s.locations[0].state == "WY"
    assert s.locations[0].is_primary
    # jobCreatedAt (the real posting time) wins over the board's listing time.
    assert s.posted_at == datetime.fromtimestamp(1788742869.952, tz=UTC)
    assert s.closes_at and s.closes_at > s.posted_at
    # NLx-fed listings come back with an empty description; resolve fetches it.
    assert s.description_raw is None
    # The listing has no apply link either, so resolve is wanted.
    assert s.needs_resolve is True
    assert s.apply_url is None
    assert s.extra["origin"] == "NLX"


def test_internal_posting_stub(conn, tmp_path):
    src = row()
    api = Api()
    stubs = {s.external_id: s for s in run(src, make_ctx(src, conn, api, tmp_path))}
    s = stubs["6ac926070ee241d8513a2374"]
    assert s.title == "New Accounts - Customer Service Representatives"  # leading space stripped
    assert s.agency_raw == "Bank of Commerce"
    assert s.description_raw and s.description_raw.startswith("<p>")
    assert s.location_raw == "Rawlins, WY"
    assert s.extra["origin"] == "case manager"
    assert s.extra["external"] is False


# ─── resolve ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fix", ["detail_nlx.json", "detail_internal.json"])
def test_resolve_parses_description_apply_link_and_closing_date(conn, tmp_path, fix):
    src = row()
    api = Api()
    d = fixture(fix)["data"]
    stub = JobStub(
        source_key=src.key,
        external_id=d["_id"],
        title="x",
        url=f"https://hire.wyo.gov/job/{d['_id']}",
        extra={"job_id": d["_id"]},
    )
    detail = WyomingAdapter().resolve(stub, make_ctx(src, conn, api, tmp_path))
    assert api.requests[-1].url.path == f"/employer-api/api/jobs/search/{d['_id']}"
    assert detail.description_raw == d["jobDetails"]["Job Description"]
    assert detail.apply_url == d["howToApplyJob"]["redirectCompanyWebsite"]
    assert detail.apply_url.startswith("https://")
    assert detail.title == d["cardDetails"]["jobName"].strip()
    assert detail.agency_raw == d["cardDetails"]["companyName"]
    assert detail.closes_at == datetime.fromtimestamp(
        d["subValues"]["jobExpiryDate"] / 1000, tz=UTC
    )
    assert detail.needs_resolve is False
    assert detail.locations and detail.locations[0].state == "WY"


def test_resolve_maps_employment_type_text(conn, tmp_path):
    src = row()
    api = Api()
    d = fixture("detail_nlx.json")["data"]
    stub = JobStub(source_key=src.key, external_id=d["_id"], title="x", url="https://hire.wyo.gov/")
    detail = WyomingAdapter().resolve(stub, make_ctx(src, conn, api, tmp_path))
    assert detail.employment_type is EmploymentType.full_time


def test_resolve_falls_back_to_company_website_for_apply(conn, tmp_path):
    src = row()
    api = Api()
    d = api.details["6ac926070ee241d8513a2374"]["data"]
    d["howToApplyJob"] = {}
    stub = JobStub(source_key=src.key, external_id=d["_id"], title="x", url="https://hire.wyo.gov/")
    detail = WyomingAdapter().resolve(stub, make_ctx(src, conn, api, tmp_path))
    assert detail.apply_url == d["companyDetails"]["companyWebsite"]


# ─── robots ────────────────────────────────────────────────────────────────


def test_html_shell_at_robots_txt_counts_as_absent(conn, tmp_path):
    src = row()
    api = Api(robots=HTML_SHELL)
    stubs = run(src, make_ctx(src, conn, api, tmp_path))
    assert len(stubs) == 5
    assert api.requests[0].url.path == "/robots.txt"


def test_disallowing_robots_blocks_the_api_before_any_search(conn, tmp_path):
    src = row()
    api = Api(robots="User-agent: *\nDisallow: /employer-api/\n")
    with pytest.raises(RobotsDisallowed):
        run(src, make_ctx(src, conn, api, tmp_path))
    assert api.searches() == []
    assert [r.url.path for r in api.requests] == ["/robots.txt"]
