"""Shared plumbing for the class B (state-employer) tests: Workday JSON and jobs2web HTML boards.

Fixtures live in ``tests/fixtures/classb/<st>/``. They are real responses captured on 2026-10-09,
trimmed to five postings and two details, with e-mail addresses and phone numbers replaced.
"""

from __future__ import annotations

import json
import re
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


def assert_enabled_class_b(st: str, tier: str, key: str | None = None) -> None:
    src = registry_row(key or f"{st.lower()}-employer")
    assert src.class_ is SourceClass.B
    assert (src.family, src.tier.value, src.policy.value, src.state) == (
        "htmlconfig",
        tier,
        "enabled",
        st,
    )


def workday_site(st: str, tenant: str, site: str) -> Site:
    """``st`` names the fixture directory (the state, or e.g. ``me_judicial``)."""
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


def check_workday(
    st: str, tenant: str, site: str, conn, tmp_path, *, key: str | None = None
) -> None:
    key = key or f"{st.lower()}-employer"
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


JOB_IDS = {
    "IL": ("1436885300", "1433847600"),
    "IN": ("1434837000", "1438374900"),
    "AR": ("1438868400", "1436802800"),
    "FL": ("1437723500", "1438638800"),
    "VT": ("1433820300", "1436886000"),
}
AGENCY = {
    "IL": "State of Illinois",
    "IN": "State of Indiana",
    "AR": "State of Arkansas",
    "FL": "State of Florida",
    "VT": "State of Vermont",
}


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
    assert detail.agency_raw == AGENCY[st]
    assert detail.needs_resolve is False
    assert "@" not in detail.description_raw.replace("hr@example.gov", "")

    second = HtmlConfigAdapter().resolve(stubs[1], ctx)
    assert second.description_raw
    # the Apply button's /talentcommunity/ path is robots-disallowed and never requested
    assert not any("/talentcommunity/" in p for p in s.paths())


TABLE_ROW = r'<tr class="data-row'
ROW_START = {"IL": r'<li class="job-tile'}
ROW_END = {"IL": "</ul>"}


def padded_page(st: str, rows: int, bump: int) -> str:
    """The saved search page repeated to ``rows`` rows, job ids shifted by ``bump`` (synthetic)."""
    text = fixture(st, "search.html")
    starts = [m.start() for m in re.finditer(ROW_START.get(st, TABLE_ROW), text)]
    end = text.index(ROW_END.get(st, "</tbody>"), starts[-1])
    segs = [text[a:b] for a, b in zip(starts, [*starts[1:], end], strict=True)]
    out = []
    for k in range(rows):
        shift = bump + k // len(segs)
        seg = segs[k % len(segs)]
        out.append(re.sub(r"/(\d{10})/", lambda m, s=shift: f"/{int(m[1]) + s}/", seg))
    return text[: starts[0]] + "".join(out) + text[end:]


def check_jobs2web_paging(st: str, conn, tmp_path) -> None:
    """startrow paging: 25 rows per page; a short page ends it; max_pages caps it."""
    src = registry_row(f"{st.lower()}-employer")
    assert src.pagination["kind"] == "offset" and src.pagination["param"] == "startrow"
    pages = {"0": padded_page(st, 25, 1), "25": padded_page(st, 5, 1000)}
    html = {"content-type": "text/html"}

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[request.url.params["startrow"]], headers=html)

    s = Site(route, robots="User-agent: *\nDisallow: /applybutton/\n")
    ctx = make_ctx(src, conn, s, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))
    assert [r.url.params["startrow"] for r in s.requests] == ["0", "25"]
    assert len(stubs) == 30 and len({st_.external_id for st_ in stubs}) == 30

    # always-full pages stop at max_pages
    src_cap = src.model_copy(update={"pagination": {**src.pagination, "max_pages": 3}})
    n = iter(range(100))

    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=padded_page(st, 25, 10 * next(n) + 10), headers=html)

    s2 = Site(endless, robots="User-agent: *\nDisallow: /applybutton/\n")
    ctx2 = make_ctx(src_cap, conn, s2, tmp_path / "cap")
    capped = list(HtmlConfigAdapter().search(src_cap, Query(title="analyst"), SINCE, ctx2))
    assert [r.url.params["startrow"] for r in s2.requests] == ["0", "25", "50"]
    assert len(capped) == 75


# JobAps (CT, MD): one server-rendered list page, bulletin pages under /<ST>/sup/.

JOBAPS_IDS = {
    "CT": ("260930-0912MP-001", "260930-0965FP-001", "0912MP", "0965FP"),
    "MD": ("26-000960-0001", "26-004582-0001", "000960", "004582"),
}


def jobaps_site(st: str) -> Site:
    search = fixture(st, "search.html")
    _, _, r2_1, r2_2 = JOBAPS_IDS[st]
    details = {r2_1: fixture(st, "detail_1.html"), r2_2: fixture(st, "detail_2.html")}
    html = {"content-type": "text/html"}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{st}/":
            return httpx.Response(200, text=search, headers=html)
        if request.url.path == f"/{st}/sup/bulpreview.asp":
            r2 = request.url.params.get("R2", "")
            if r2 in details:
                return httpx.Response(200, text=details[r2], headers=html)
        return httpx.Response(404)

    return Site(route, robots="User-agent: *\nAllow: /\n")


def check_jobaps(st: str, conn, tmp_path, *, salary_in_list: bool) -> None:
    key = f"{st.lower()}-employer"
    src = registry_row(key)
    first_id, second_id, _, _ = JOBAPS_IDS[st]
    s = jobaps_site(st)
    ctx = make_ctx(src, conn, s, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))

    # the list takes no query: one GET of the whole page; CT's employees-only tables are skipped
    (req,) = s.requests
    assert req.method == "GET" and req.url.host == "www.jobapscloud.com"
    assert req.url.path == f"/{st}/" and not req.url.params
    assert len(stubs) == 5
    first = stubs[0]
    assert first.source_key == key
    assert first.external_id == first_id
    assert first.url.startswith(f"https://www.jobapscloud.com/{st}/sup/bulpreview.asp?")
    assert first.title and first.agency_raw
    assert first.closes_at is not None and first.closes_at.tzinfo is not None
    assert first.locations[0].state == st
    assert first.needs_resolve is True and first.apply_url is None
    assert bool(first.salary_raw) is salary_in_list
    assert len({st_.external_id for st_ in stubs}) == 5

    detail = HtmlConfigAdapter().resolve(first, ctx)
    assert detail.description_raw and "<" in detail.description_raw
    assert detail.posted_at is not None and detail.posted_at.tzinfo is not None
    assert detail.closes_at is not None and detail.closes_at > detail.posted_at
    assert detail.salary_raw and "$" in detail.salary_raw
    assert detail.agency_raw and detail.agency_raw.startswith("State of ")
    assert detail.title and detail.needs_resolve is False
    assert "@" not in detail.description_raw.replace("hr@example.gov", "")

    other = HtmlConfigAdapter().resolve(stubs[1], ctx)
    assert other.external_id == second_id and other.description_raw
