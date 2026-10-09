"""Maine Legislature careers: the Legislature site of the Maine Workday tenant.

It had no postings on 2026-10-09, so the saved answer is the real empty one and the row is
otherwise the same shape as the Executive and Judicial rows (their tests cover parsing).
"""

from __future__ import annotations

import json

import httpx
import pytest
from classb_support import SINCE, WORKDAY_ROBOTS, assert_enabled_class_b
from hc_support import Site, make_ctx, registry_row

from jobhunter.core import db
from jobhunter.core.fetch import reset_shared_state
from jobhunter.core.models import Query
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter

EMPTY = '{"total":0,"jobPostings":[],"facets":[],"userAuthenticated":false}'


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


def test_row_is_an_enabled_class_b_api_row():
    assert_enabled_class_b("ME", "api", "me-legislature-employer")
    src = registry_row("me-legislature-employer")
    assert src.entry.endswith("/Legislature")


def test_empty_board_yields_no_stubs(conn, tmp_path):
    src = registry_row("me-legislature-employer")
    site = Site(
        lambda request: httpx.Response(200, text=EMPTY),
        robots=WORKDAY_ROBOTS.replace("SITE", "Legislature"),
    )
    ctx = make_ctx(src, conn, site, tmp_path)
    stubs = list(HtmlConfigAdapter().search(src, Query(title="analyst"), SINCE, ctx))
    assert stubs == []
    (req,) = site.requests
    assert req.method == "POST" and req.url.path == "/wday/cxs/maine/Legislature/jobs"
    assert json.loads(req.content)["searchText"] == "analyst"
