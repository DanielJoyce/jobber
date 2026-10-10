"""Employer rejections: the 0023 table and the matching rules in core/rejections."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from jobhunter.core import db, rejections

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('co', 'A', 'T', 'x', 'http', 'https://example.test', 'enabled')"
    )
    yield c
    c.close()


def add_group(conn, n, employer, title):
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, first_seen_at, "
        "last_seen_at) VALUES (?, 'co', ?, 'https://example.test/j', ?, ?, 'x', 'x')",
        (n, str(n), title, employer),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', 'x')",
        (n, n),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    return n


def rec(conn, employer, title, **kw):
    kw.setdefault("received_at", "2026-09-01T12:00:00+00:00")
    return rejections.record(
        conn, employer=employer, title=title, source=kw.pop("source", "email"), now=NOW, **kw
    )


def test_migration_creates_table_and_index(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rejection)")}
    assert {
        "id",
        "gmail_message_id",
        "thread_id",
        "received_at",
        "employer",
        "employer_norm",
        "title",
        "job_group_id",
        "application_id",
        "source",
        "evidence",
        "created_at",
    } <= cols
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(rejection)")}
    assert "idx_rejection_employer_norm" in idx
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 23


def test_source_check_and_unique_message_id(conn):
    with pytest.raises(ValueError):
        rec(conn, "Acme", "X", source="guess")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rejection (received_at, source, created_at) VALUES ('x', 'guess', 'x')"
        )
    assert rec(conn, "Acme", "X", gmail_message_id="m1") is not None
    assert rec(conn, "Acme", "X", gmail_message_id="m1") is None  # idempotent
    # Manual rows have no message id; several are allowed.
    assert rec(conn, "Acme", "Y", source="manual") is not None
    assert rec(conn, "Acme", "Z", source="manual") is not None
    assert conn.execute("SELECT COUNT(*) FROM rejection").fetchone()[0] == 3


def test_employer_norm_drops_corporate_suffixes():
    assert rejections.employer_norm("Contoso Corporation") == "contoso"
    assert rejections.employer_norm("Northwind Analytics, Inc.") == "northwind analytics"
    assert rejections.employer_norm(None) == ""


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Senior Data Engineer", "Senior Data Engineer", True),
        ("Senior Data Engineer", "Sr. Data-Engineer", False),  # level words must agree
        ("Data Engineer, Platform", "Data Engineer - Platform", True),
        ("Software Engineer II", "Software Engineer III", False),
        ("Systems Engineer 1", "Systems Engineer 3", False),
        ("Data Engineer", "Senior Data Engineer", False),
        ("Data Engineer", None, False),
        # Word order, punctuation, case, plurals and spacing do not make a different posting.
        ("Platform Engineer - Data", "Data Platform Engineer", True),
        ("Full Stack Engineer", "Fullstack Engineer", True),
        ("Software Engineers", "software engineer", True),
        # A different team, specialty or extra word is a different role (the fuzzy ratio
        # alone scored all of these >= 90).
        ("Senior Software Engineer, Cloud", "Senior Software Engineer, Core", False),
        ("Staff Software Engineer, Ads", "Staff Software Engineer, AI", False),
        ("Senior Engineer - Search", "Senior Engineer - Research", False),
        ("Staff Software Engineer", "Staff Software Engineer - FE", False),
        ("Senior Software Engineer, Mobile QA", "Senior Software Engineer, Native Mobile", False),
        ("Software Engineer, AV Labs", "Software Engineer, ML AV Labs", False),
        ("Radiologic Technologist (Gen)", "Radiologic Technologist (MRI)", False),
        (
            "Housekeeping Aid - Service Technician",
            "Housekeeping Aid - Service Technician Leader",
            False,
        ),
    ],
)
def test_same_title(a, b, same):
    assert rejections.same_title(a, b) is same


def test_rejected_group_ids_direct_and_by_employer_title(conn):
    g1 = add_group(conn, 1, "Acme", "Welder")
    g2 = add_group(conn, 2, "Northwind Analytics Inc", "Senior Data Engineer")
    g3 = add_group(conn, 3, "Northwind Analytics", "Platform Engineer")
    g4 = add_group(conn, 4, "Northwind Bakery", "Senior Data Engineer")
    rec(conn, "Acme", None, job_group_id=g1)
    rec(conn, "Northwind Analytics", "Senior Data Engineer")
    assert rejections.rejected_group_ids(conn) == {g1, g2}
    assert g3 not in rejections.rejected_group_ids(conn)
    assert g4 not in rejections.rejected_group_ids(conn)


def test_annotate_sets_fact_only_for_other_roles_within_window(conn):
    rec(conn, "Northwind Analytics", "Senior Data Engineer", received_at="2026-09-01T00:00Z")
    rows = [
        {"group_id": 1, "employer": "Northwind Analytics", "title": "Platform Engineer"},
        {"group_id": 2, "employer": "Northwind Analytics", "title": "Senior Data Engineer"},
        {"group_id": 3, "employer": "Acme", "title": "Platform Engineer"},
    ]
    rejections.annotate(conn, rows, now=NOW, days=90)
    assert rows[0]["prior_rejection"] == (
        "candidate was rejected by this employer for Senior Data Engineer on 2026-09-01"
    )
    assert "prior_rejection" not in rows[1]  # the same posting: excluded, not annotated
    assert "prior_rejection" not in rows[2]
    late = [{"group_id": 1, "employer": "Northwind Analytics", "title": "Platform Engineer"}]
    rejections.annotate(conn, late, now=NOW, days=10)
    assert "prior_rejection" not in late[0]
