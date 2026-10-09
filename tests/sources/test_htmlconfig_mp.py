"""CNMI Department of Labor job vacancy announcements (marianaslabor.net): classic ASP tables.

On 2026-10-09 the active list was empty ("There are no active JVAs at this time"), so
``list_rows.html`` is the inactive list (jvapub_listinact.asp), which shares the active list's
table layout. Details are real JVA pages with the employer phone and email scrubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from hc_support import Site, fixture_text, make_ctx, registry_row

from jobhunter.core import db
from jobhunter.core.fetch import RobotsDisallowed, reset_shared_state
from jobhunter.core.models import Query
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter

BOARD = "mp"
KEY = "mp-marianaslabor"
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


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


def site(listing: str = "list_rows.html") -> Site:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jvapub_list.asp":
            return httpx.Response(200, text=fixture_text(BOARD, listing))
        if request.url.path == "/jvapub_view.asp":
            jva = request.url.params.get("jvaID")
            name = {"104013": "detail_1.html", "104011": "detail_2.html"}.get(jva or "")
            if name:
                return httpx.Response(200, text=fixture_text(BOARD, name))
        return httpx.Response(404)

    return Site(route, robots="")  # robots.txt is absent on this host: "" is allow-all


def search(s: Site, conn, tmp_path, since=LONG_AGO):
    src = registry_row(KEY)
    ctx = make_ctx(src, conn, s, tmp_path)
    return src, ctx, list(HtmlConfigAdapter().search(src, Query(keywords=["x"]), since, ctx))


def test_row_is_enabled_htmlconfig():
    src = registry_row(KEY)
    assert (src.family, src.policy.value, src.tier.value) == ("htmlconfig", "enabled", "http")


def test_empty_active_list_yields_nothing(conn, tmp_path):
    s = site("list_empty.html")
    _, _, stubs = search(s, conn, tmp_path)
    assert stubs == []
    assert s.paths() == ["GET /jvapub_list.asp"]


def test_rows_become_stubs_and_skip_the_header(conn, tmp_path):
    _, _, stubs = search(site(), conn, tmp_path)
    assert {s.external_id for s in stubs} == {"104013", "104011", "104012", "104005", "104004"}
    by_id = {s.external_id: s for s in stubs}
    s = by_id["104011"]
    assert s.title == "Maintenance Technician"
    assert s.agency_raw == "Pacific Biomedical Services, Inc."
    assert s.url == "https://marianaslabor.net/jvapub_view.asp?jvaID=104011"
    assert s.posted_at == datetime(2024, 4, 2, 14, 0, tzinfo=UTC)  # 4/3/2024 00:00 Guam
    assert s.closes_at == datetime(2024, 4, 23, 14, 0, tzinfo=UTC)
    assert s.locations[0].state == "MP"
    assert s.needs_resolve is True


def test_search_sends_one_get_sorted_by_opening_date(conn, tmp_path):
    s = site()
    search(s, conn, tmp_path)
    assert s.paths() == ["GET /jvapub_list.asp"]
    assert dict(s.requests[0].url.params) == {"sort": "1"}


def test_watermark_drops_old_postings(conn, tmp_path):
    _, _, stubs = search(site(), conn, tmp_path, since=datetime(2030, 1, 1, tzinfo=UTC))
    assert stubs == []


def test_resolve_reads_labelled_detail_cells(conn, tmp_path):
    s = site()
    _, ctx, stubs = search(s, conn, tmp_path)
    stub = next(x for x in stubs if x.external_id == "104011")
    d = HtmlConfigAdapter().resolve(stub, ctx)
    assert d.agency_raw == "Pacific Biomedical Services, Inc."
    assert d.salary_raw and d.salary_raw.startswith("$9.54 to $15")
    assert d.location_raw == "Saipan, MP"
    assert d.locations[0].city == "Saipan" and d.locations[0].state == "MP"
    assert d.closes_at == datetime(2024, 4, 23, 14, 0, tzinfo=UTC)
    assert d.description_raw and "Job Duties" in d.description_raw
    assert "Qualification Requirements" in d.description_raw
    # The employer contact table (first table) is not part of the description.
    assert "Contact Pacific Biomedical" not in d.description_raw
    assert "P.O. Box" not in d.description_raw
    assert d.needs_resolve is False


def test_resolve_second_detail(conn, tmp_path):
    s = site()
    _, ctx, stubs = search(s, conn, tmp_path)
    stub = next(x for x in stubs if x.external_id == "104013")
    d = HtmlConfigAdapter().resolve(stub, ctx)
    assert d.title == "Sales Representative"
    assert d.salary_raw and "13.11" in d.salary_raw
    assert d.description_raw


def test_robots_disallow_is_respected(conn, tmp_path):
    s = site()
    s.robots = "User-agent: *\nDisallow: /jvapub\n"
    with pytest.raises(RobotsDisallowed):
        search(s, conn, tmp_path)
