"""USAJOBS adapter tests. httpx.MockTransport only: no network, no real key."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext, reset_shared_state
from jobhunter.core.models import Query, SourceRow
from jobhunter.pipeline.listing import upsert_stubs
from jobhunter.scoring.profile import Profile
from jobhunter.sources.adapters.usajobs import (
    UsajobsAdapter,
    UsajobsAuthMissing,
    build_params,
    queries_from_profile,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "usajobs"
API = "https://data.usajobs.gov/api/search"
SINCE = datetime(2026, 10, 3, tzinfo=UTC)


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


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setenv("USAJOBS_EMAIL", "tester@example.com")
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")


@pytest.fixture
def src() -> SourceRow:
    return SourceRow.model_validate(
        {
            "key": "us-usajobs",
            "class": "C",
            "name": "USAJOBS",
            "family": "usajobs",
            "tier": "api",
            "entry": API,
            "config": {
                "sanctioned_api": True,
                "auth": {"email_env": "USAJOBS_EMAIL", "key_env": "USAJOBS_API_KEY"},
                "results_per_page": 500,
            },
            "rate_limit": {"rps": 2},
        }
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('us-usajobs', 'C', 'USAJOBS', 'usajobs', 'api', ?, 'enabled')",
        (API,),
    )
    yield c
    c.close()


class Api:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.pages = {"1": fixture("page1.json"), "2": fixture("page2.json")}
        self.robots = httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return self.robots
        page = request.url.params.get("Page", "1")
        return httpx.Response(200, json=self.pages[page])

    def searches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/api/search"]


@pytest.fixture
def api() -> Api:
    return Api()


@pytest.fixture
def ctx(src, conn, api, tmp_path):
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
    return list(UsajobsAdapter().search(src, query or Query(keywords=["cyber"]), since, ctx))


# ─── params ────────────────────────────────────────────────────────────────


def test_build_params_full(src):
    q = Query(
        keywords=["cyber", "security"],
        title="IT Specialist",
        occupation_code="2210",
        salary_min=100000,
        grade_low="12",
        grade_high="14",
    )
    now = datetime(2026, 10, 9, tzinfo=UTC)
    p = build_params(src, q, SINCE, 3, now=now)
    assert p == {
        "Keyword": "cyber security",
        "PositionTitle": "IT Specialist",
        "JobCategoryCode": "2210",
        "RemunerationMinimumAmount": 100000,
        "PayGradeLow": "12",
        "PayGradeHigh": "14",
        "DatePosted": 6,
        "ResultsPerPage": 500,
        "Page": 3,
        "SortField": "opendate",
        "SortDirection": "desc",
    }


def test_date_posted_clamped(src):
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert build_params(src, Query(), datetime(2025, 1, 1, tzinfo=UTC), 1, now)["DatePosted"] == 60
    assert build_params(src, Query(), datetime(2026, 10, 20, tzinfo=UTC), 1, now)["DatePosted"] == 0


def test_results_per_page_default_and_override(src):
    now = datetime(2026, 10, 9, tzinfo=UTC)
    src.config["results_per_page"] = 25
    assert build_params(src, Query(), SINCE, 1, now)["ResultsPerPage"] == 25
    del src.config["results_per_page"]
    assert build_params(src, Query(), SINCE, 1, now)["ResultsPerPage"] == 500


# ─── search ────────────────────────────────────────────────────────────────


def test_headers_and_paging_stop_at_watermark(src, ctx, api):
    stubs = run(src, ctx)
    # page 2's "Old Posting" predates SINCE and stops the iteration; it is not yielded.
    assert [s.external_id for s in stubs] == ["800001", "800002", "800003", "800004"]
    searches = api.searches()
    assert [r.url.params["Page"] for r in searches] == ["1", "2"]
    h = searches[0].headers
    assert h["authorization-key"] == "test-key"
    assert h["user-agent"] == "tester@example.com"
    assert h["host"] == "data.usajobs.gov"
    assert searches[0].url.params["Keyword"] == "cyber"


def test_stops_before_fetching_page_two_when_consumer_stops(src, ctx, api):
    gen = UsajobsAdapter().search(src, Query(), SINCE, ctx)
    next(gen)
    assert len(api.searches()) == 1


def test_watermark_on_page_one_never_fetches_page_two(src, ctx, api):
    stubs = run(src, ctx, since=datetime(2026, 10, 7, 12, tzinfo=UTC))
    assert [s.external_id for s in stubs] == ["800001"]
    assert len(api.searches()) == 1


def test_missing_auth_env_names_variables(src, ctx, monkeypatch):
    monkeypatch.delenv("USAJOBS_EMAIL")
    monkeypatch.delenv("USAJOBS_API_KEY")
    with pytest.raises(UsajobsAuthMissing, match=r"USAJOBS_EMAIL.*USAJOBS_API_KEY"):
        run(src, ctx)


def test_custom_env_names(src, ctx, monkeypatch):
    src.config["auth"] = {"email_env": "MY_EMAIL", "key_env": "MY_KEY"}
    monkeypatch.setenv("MY_EMAIL", "a@example.com")
    with pytest.raises(UsajobsAuthMissing, match="MY_KEY"):
        run(src, ctx)


def test_sanctioned_api_ignores_robots_disallow(src, ctx, api):
    assert run(src, ctx)  # robots says Disallow: / yet the keyed API call goes through


def test_robots_enforced_without_sanctioned_flag(src, conn, api, tmp_path):
    from jobhunter.core.fetch import RobotsDisallowed

    src.config["sanctioned_api"] = False
    clock = Clock()
    settings = Settings.model_validate({"paths": {"cache_dir": str(tmp_path / "c2")}})
    c = FetchContext(
        src, settings, conn, transport=httpx.MockTransport(api.handle), clock=clock,
        sleep=clock.sleep,
    )  # fmt: skip
    with pytest.raises(RobotsDisallowed):
        run(src, c)


# ─── mapping ───────────────────────────────────────────────────────────────


def by_id(stubs):
    return {s.external_id: s for s in stubs}


def test_core_field_mapping(src, ctx):
    s = by_id(run(src, ctx))["800001"]
    assert s.title == "IT Specialist (INFOSEC)"
    assert s.url == "https://www.usajobs.gov/job/800001"
    assert s.apply_url == "https://www.usajobs.gov/job/800001/apply"
    assert s.agency_raw == "Department of the Army"
    assert s.posted_at == datetime(2026, 10, 8, tzinfo=UTC)
    assert s.closes_at is not None and s.closes_at.day == 22
    assert s.needs_resolve is False
    assert s.salary_raw == "$106,823 - $164,301 per year"
    assert s.extra["occupation_code"] == "2210"
    assert s.extra["pay_plan"] == "GS"
    assert (s.extra["grade_low"], s.extra["grade_high"]) == ("12", "13")
    assert s.extra["telework_eligible"] is True
    assert s.extra["remote_indicator"] is False
    assert s.extra["salary_period"] == "year"


def test_fourteen_locations(src, ctx):
    s = by_id(run(src, ctx))["800001"]
    assert len(s.locations) == 14
    assert sorted({loc.state for loc in s.locations}) == [
        "AZ", "CO", "DC", "ID", "MD", "NM", "OR", "TX", "UT", "VA", "WA"
    ]  # fmt: skip
    assert [loc.is_primary for loc in s.locations].count(True) == 1
    assert s.locations[2].city == "Fort Carson, Colorado"  # military city kept verbatim
    assert s.locations[0].lat == 39.7


def test_full_state_name_normalized_and_duplicates_collapsed(src, ctx):
    s = by_id(run(src, ctx))["800002"]
    assert [(loc.state, loc.city) for loc in s.locations] == [("CO", "Denver, Colorado")]


def test_overseas_location_has_no_state(src, ctx):
    s = by_id(run(src, ctx))["800003"]
    assert [loc.state for loc in s.locations] == [None]
    assert s.locations[0].city == "Yokosuka, Japan"


def test_salary_interval_mapping(src, ctx):
    stubs = by_id(run(src, ctx))
    assert stubs["800002"].salary_raw == "$45 - $59 per hour"
    assert stubs["800002"].extra["salary_period"] == "hour"
    assert stubs["800003"].salary_raw == "$90,000 - $120,000 per biweekly pay period"
    assert stubs["800003"].extra["salary_period"] is None
    assert "without compensation" in (stubs["800004"].salary_raw or "")


def test_description_assembled_as_html(src, ctx):
    d = by_id(run(src, ctx))["800001"].description_raw
    assert d is not None
    for heading in ("Summary", "Major Duties", "Qualifications", "Requirements", "Education"):
        assert f"<h2>{heading}</h2>" in d
    assert "<li>Plans and coordinates projects</li>" in d
    assert "<p>You will support mission systems.</p>" in d
    assert "one year of specialized experience" in d
    assert d.index("Summary") < d.index("Major Duties") < d.index("Education")


def test_resolve_is_offline_and_carries_federal_fields(src, ctx, api):
    stub = run(src, ctx)[0]
    before = len(api.requests)
    detail = UsajobsAdapter().resolve(stub, ctx)
    assert len(api.requests) == before
    assert detail.occupation_code == "2210"
    assert detail.pay_plan == "GS"
    assert (detail.grade_low, detail.grade_high) == ("12", "13")
    assert detail.description_raw == stub.description_raw


# ─── persistence ───────────────────────────────────────────────────────────


def test_apply_url_and_federal_fields_persist(src, ctx, conn):
    stubs = run(src, ctx)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert upsert_stubs(conn, stubs, now).inserted == 4
    r = conn.execute("SELECT * FROM job WHERE external_id = '800001'").fetchone()
    assert r["apply_url"] == "https://www.usajobs.gov/job/800001/apply"
    assert (r["occupation_code"], r["pay_plan"], r["grade_low"], r["grade_high"]) == (
        "2210", "GS", "12", "13",
    )  # fmt: skip
    assert r["needs_resolve"] == 0
    n = conn.execute("SELECT COUNT(*) FROM job_locations WHERE job_id = ?", (r["id"],)).fetchone()[
        0
    ]
    assert n == 14
    # Re-listing is an update, not a duplicate.
    assert upsert_stubs(conn, stubs, now).updated == 4


# ─── queries from profile ──────────────────────────────────────────────────


def test_queries_from_profile():
    profile = Profile.model_validate(
        {
            "target_titles": ["IT Specialist", "Management Analyst"],
            "hard": {"salary_floor": {"amount": 90000, "period": "year"}},
            "queries": [
                {"keywords": ["cyber"], "occupation_code": "2210"},
                {"title": "it specialist"},
            ],
        }
    )
    qs = queries_from_profile(profile)
    assert [(q.keywords, q.title, q.occupation_code) for q in qs] == [
        (["cyber"], None, "2210"),
        ([], "it specialist", None),
        (None, "Management Analyst", None),
    ]
    assert all(q.salary_min == 90000 for q in qs)


def test_salary_floor_ignored_unless_yearly():
    profile = Profile.model_validate(
        {
            "target_titles": ["Analyst"],
            "hard": {"salary_floor": {"amount": 50, "period": "hour"}},
        }
    )
    assert [q.salary_min for q in queries_from_profile(profile)] == [None]
