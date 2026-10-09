"""Florida state employer careers: SuccessFactors "jobs2web" career site read as HTML.

Fixtures are real pages captured 2026-10-09 (search trimmed to five rows, two details).
"""

from __future__ import annotations

import pytest
from classb_support import assert_enabled_class_b, check_jobs2web, check_jobs2web_paging

from jobhunter.core import db
from jobhunter.core.fetch import reset_shared_state


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


def test_row_is_an_enabled_class_b_http_row():
    assert_enabled_class_b("FL", "http")


def test_search_and_resolve(conn, tmp_path):
    check_jobs2web("FL", "jobs.myflorida.com", conn, tmp_path, employer_in_list=False)


def test_pages_by_startrow(conn, tmp_path):
    check_jobs2web_paging("FL", conn, tmp_path)
