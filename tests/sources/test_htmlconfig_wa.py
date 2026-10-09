"""WorkSource Washington: a Salesforce LWR site; its public /job-search calls a guest Apex method.

The fixture is a real ``initializeJobSearch`` response (2026-10-09) cut to six jobs, with the
server's ``matchingAccountIds`` list and SOQL ``filters`` string removed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from hc_support import Site, fixture_text, make_ctx, registry_row

from jobhunter.core import db
from jobhunter.core.fetch import reset_shared_state
from jobhunter.core.models import Query
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter

BOARD = "wa"
KEY = "wa-worksourcewa"
ROBOTS = "User-agent: *\nAllow: /\nDisallow: */secur/forgotpassword.jsp?*\n"
APEX = "/worksourcewa/webruntime/api/apex/execute"


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


def site() -> Site:
    def route(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == APEX:
            return httpx.Response(200, text=fixture_text(BOARD, "search.json"))
        return httpx.Response(404)

    return Site(route, robots=ROBOTS)


def search(s: Site, conn, tmp_path, query=None, since=datetime(2026, 1, 1, tzinfo=UTC)):
    src = registry_row(KEY)
    ctx = make_ctx(src, conn, s, tmp_path)
    q = query or Query(title="engineer")
    return list(HtmlConfigAdapter().search(src, q, since, ctx))


def test_row_is_an_enabled_api_tier_htmlconfig_row():
    src = registry_row(KEY)
    assert (src.family, src.tier.value, src.policy.value) == ("htmlconfig", "api", "enabled")


def test_posts_the_guest_apex_call_with_the_title(conn, tmp_path):
    s = site()
    search(s, conn, tmp_path, Query(title="data engineer", keywords=["python"]))
    (req,) = s.requests
    assert req.method == "POST" and req.url.host == "worksource.my.site.com"
    assert req.headers["content-type"].startswith("application/json")
    body = json.loads(req.content)
    assert body["classname"] == "@udd/01pcs0000098deg"
    assert body["method"] == "initializeJobSearch"
    assert body["params"]["jobTitle"] == "data engineer"
    assert body["params"]["additionalFilters"] is None
    assert body["isContinuation"] is False


def test_only_one_request_is_made(conn, tmp_path):
    s = site()
    stubs = search(s, conn, tmp_path)
    assert len(stubs) == 6
    assert len(s.requests) == 1


def test_json_rows_become_stubs(conn, tmp_path):
    s = search(site(), conn, tmp_path)[1]
    assert s.source_key == KEY
    assert s.external_id.startswith("a0g")
    assert (
        s.url
        == f"https://worksource.my.site.com/worksourcewa/job-search/job-details?jobId={s.external_id}"
    )
    assert s.title and s.agency_raw
    assert s.posted_at is not None and s.posted_at.tzinfo is not None
    assert s.closes_at is not None
    assert s.description_raw and s.description_raw.startswith("<")
    assert s.needs_resolve is False  # the Apex response already carries the full description
    assert s.locations[0].state == "WA"


def test_location_text_and_city(conn, tmp_path):
    stubs = search(site(), conn, tmp_path)
    withloc = next(s for s in stubs if s.location_raw)
    assert withloc.locations[0].state == "WA"
    assert withloc.locations[0].city == withloc.location_raw.split(",")[0]


def test_salary_text_carries_the_pay_type(conn, tmp_path):
    stubs = search(site(), conn, tmp_path)
    salaried = [s for s in stubs if s.salary_raw]
    assert salaried and salaried[0].salary_raw.startswith("$")
    assert salaried[0].salary_raw.split()[-1] in {"Hourly", "Salary", "Yearly", "Monthly", "Weekly"}


def test_newest_first_watermark_stops_nothing_because_it_is_one_page(conn, tmp_path):
    stubs = search(site(), conn, tmp_path)
    newest = max(s.posted_at for s in stubs)
    kept = search(site(), conn, tmp_path, since=newest)
    assert [s.posted_at for s in kept] == [newest]
