"""North Carolina state employer careers: a public Workday tenant read through its JSON API.

Fixtures are real responses captured 2026-10-09 (search trimmed to five postings, two details).
"""

from __future__ import annotations

import pytest
from classb_support import assert_enabled_class_b, check_workday

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


def test_row_is_an_enabled_class_b_api_row():
    assert_enabled_class_b("NC", "api")


def test_workday_search_and_resolve(conn, tmp_path):
    check_workday("NC", "nc", "NC_Careers", conn, tmp_path)
