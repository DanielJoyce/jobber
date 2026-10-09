"""Shared plumbing for the class B (state-employer) tests: Workday JSON and jobs2web HTML boards.

Fixtures live in ``tests/fixtures/classb/<st>/``. They are real responses captured on 2026-10-09,
trimmed to five postings and two details, with e-mail addresses and phone numbers replaced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from hc_support import Site, make_ctx, registry_row

from jobhunter.core.models import Query, SourceClass
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "classb"
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
WORKDAY_ROBOTS = "User-agent: *\nAllow: /SITE/\nDisallow: /refreshFacet/\n"


def fixture(st: str, name: str) -> str:
    return (FIXTURES / st.lower() / name).read_text()


def assert_enabled_class_b(st: str, tier: str) -> None:
    src = registry_row(f"{st.lower()}-employer")
    assert src.class_ is SourceClass.B
    assert (src.family, src.tier.value, src.policy.value, src.state) == (
        "htmlconfig",
        tier,
        "enabled",
        st,
    )


def workday_site(st: str, tenant: str, site: str) -> Site:
    search = fixture(st, "search.json")
    details = {}
    for n in (1, 2):
        text = fixture(st, f"detail_{n}.json")
        details[n] = text
    paths = [p["externalPath"] for p in json.loads(search)["jobPostings"][:2]]
    prefix = f"/wday/cxs/{tenant}/{site}"

    def route(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"{prefix}/jobs":
            return httpx.Response(200, text=search)
        for n, path in enumerate(paths, start=1):
            if request.method == "GET" and request.url.path == prefix + path:
                return httpx.Response(200, text=details[n])
        return httpx.Response(404)

    return Site(route, robots=WORKDAY_ROBOTS.replace("SITE", site))


def check_workday(st: str, tenant: str, site: str, conn, tmp_path) -> None:
    key = f"{st.lower()}-employer"
    src = registry_row(key)
    s = workday_site(st, tenant, site)
    ctx = make_ctx(src, conn, s, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))

    # one POST of a JSON body; the first page was short of the page size so paging stops
    assert len(s.requests) == 1
    req = s.requests[0]
    assert req.method == "POST" and req.url.path == f"/wday/cxs/{tenant}/{site}/jobs"
    body = json.loads(req.content)
    assert body["searchText"] == "analyst" and body["limit"] == 20 and body["offset"] == 0
    assert body["appliedFacets"] == {}

    assert len(stubs) == 5
    first = stubs[0]
    assert first.source_key == key
    assert first.external_id.startswith("/job/")
    assert first.url == f"https://{req.url.host}/wday/cxs/{tenant}/{site}{first.external_id}"
    assert first.apply_url == f"https://{req.url.host}/{site}{first.external_id}"
    assert first.title and first.needs_resolve is True
    assert all(st_.locations or st_.location_raw is None for st_ in stubs)

    detail = HtmlConfigAdapter().resolve(first, ctx)
    assert detail.description_raw and "<" in detail.description_raw
    assert detail.posted_at is not None and detail.posted_at.tzinfo is not None
    assert detail.apply_url and detail.apply_url.startswith("https://")
    assert detail.needs_resolve is False
    assert "@" not in detail.description_raw.replace("hr@example.gov", "")

    second = HtmlConfigAdapter().resolve(stubs[1], ctx)
    assert second.description_raw and second.title
    assert s.paths()[-1].startswith("GET /wday/cxs/")


JOB_IDS = {"IL": ("1436885300", "1433847600"), "IN": ("1434837000", "1438374900")}


def jobs2web_site(st: str) -> Site:
    search = fixture(st, "search.html")
    details = {JOB_IDS[st][n]: fixture(st, f"detail_{n + 1}.html") for n in (0, 1)}
    html = {"content-type": "text/html"}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/":
            return httpx.Response(200, text=search, headers=html)
        if request.url.path.startswith("/job/"):
            job_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
            if job_id in details:
                return httpx.Response(200, text=details[job_id], headers=html)
        return httpx.Response(404)

    return Site(
        route, robots="User-agent: *\nDisallow: /applybutton/\nDisallow: /talentcommunity/\n"
    )


def check_jobs2web(st: str, host: str, conn, tmp_path, *, employer_in_list: bool) -> None:
    key = f"{st.lower()}-employer"
    src = registry_row(key)
    s = jobs2web_site(st)
    ctx = make_ctx(src, conn, s, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))

    (req,) = s.requests
    assert req.url.host == host and req.url.path == "/search/"
    assert req.url.params["q"] == "analyst"
    assert req.url.params["sortColumn"] == "referencedate"

    assert len(stubs) == 5
    first = stubs[0]
    assert first.source_key == key
    assert first.external_id == JOB_IDS[st][0]
    assert first.url == f"https://{host}/job/" + first.url.split("/job/", 1)[1]
    assert first.url.rstrip("/").endswith(first.external_id)
    assert first.posted_at is not None and first.posted_at.tzinfo is not None
    assert first.locations[0].state == st and first.locations[0].city
    assert first.needs_resolve is True
    assert bool(first.agency_raw) is employer_in_list

    detail = HtmlConfigAdapter().resolve(first, ctx)
    assert detail.title == first.title.strip() or detail.title
    assert detail.description_raw and "<" in detail.description_raw
    assert detail.closes_at is not None and detail.closes_at > detail.posted_at
    assert detail.agency_raw == f"State of {'Illinois' if st == 'IL' else 'Indiana'}"
    assert detail.needs_resolve is False
    assert "@" not in detail.description_raw.replace("hr@example.gov", "")

    second = HtmlConfigAdapter().resolve(stubs[1], ctx)
    assert second.description_raw
    # the Apply button's /talentcommunity/ path is robots-disallowed and never requested
    assert not any("/talentcommunity/" in p for p in s.paths())
