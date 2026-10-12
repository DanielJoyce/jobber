"""Migration 0041 (specs/022 "Data": ``ats_posting_ref`` and ``fill_session``, ticket A3).

Run on a database shaped like a real one at 0039, then checked against what the rest of the
schema does to applications: the nightly merge moves a fill session with its application, and
the inbox's shortlist undo keeps an application that has one.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobhunter.console import inbox
from jobhunter.core import db

ISO = "2026-10-12T12:00:00+00:00"


def _upto(monkeypatch, version):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= version])
    return real


def _seed(c):
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    for gid in (1, 2):
        c.execute(
            "INSERT INTO job_group (id, canonical_job_id, member_count, method, created_at) "
            "VALUES (?, NULL, 1, 'exact_hash', ?)",
            (gid, ISO),
        )
        c.execute(
            "INSERT INTO job (id, source_key, external_id, url, title, job_group_id, "
            "first_seen_at, last_seen_at) VALUES (?, 'co', ?, ?, 'Engineer', ?, ?, ?)",
            (gid, str(gid), f"https://boards.greenhouse.io/acme/jobs/40{gid}", gid, ISO, ISO),
        )
    c.execute(
        "INSERT INTO application (id, job_group_id, status, created_at, updated_at) "
        "VALUES (1, 1, 'interested', ?, ?)",
        (ISO, ISO),
    )


def _counts(c):
    return {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("job", "job_group", "application")
    }


def test_0041_adds_both_tables_and_keeps_every_row(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "t.db")
    real = _upto(monkeypatch, 40)
    db.migrate(c)
    _seed(c)
    before = _counts(c)
    monkeypatch.setattr(db, "_load_migrations", real)
    assert 41 in db.migrate(c)
    assert _counts(c) == before
    cols = {r[1] for r in c.execute("PRAGMA table_info(fill_session)")}
    assert {"application_id", "packet_id", "posting_key", "start_url", "tab_id"} <= cols
    assert {r[1] for r in c.execute("PRAGMA table_info(ats_posting_ref)")} == {
        "posting_key",
        "job_id",
        "set_by",
        "seen_at",
    }
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0041_constraints(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    _seed(c)
    c.execute("INSERT INTO ats_posting_ref VALUES ('greenhouse:acme:401', 1, 'pick', ?)", (ISO,))
    with pytest.raises(sqlite3.IntegrityError):  # one row per posting key
        c.execute("INSERT INTO ats_posting_ref VALUES ('greenhouse:acme:401', 2, 'add', ?)", (ISO,))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO ats_posting_ref VALUES ('k', 2, 'page', ?)", (ISO,))
    with pytest.raises(sqlite3.IntegrityError):  # a job that does not exist
        c.execute("INSERT INTO ats_posting_ref VALUES ('k2', 999, 'pick', ?)", (ISO,))
    with pytest.raises(sqlite3.IntegrityError):  # an application that does not exist
        c.execute(
            "INSERT INTO fill_session (application_id, ats, host, started_at, last_seen_at) "
            "VALUES (999, 'generic', 'h', ?, ?)",
            (ISO, ISO),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO fill_session (application_id, ats, host, started_at, last_seen_at, "
            "outcome) VALUES (1, 'generic', 'h', ?, ?, 'guessed')",
            (ISO, ISO),
        )


def test_shortlist_undo_keeps_an_application_with_a_fill_session(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    _seed(c)
    c.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (1, 'interesting', ?)",
        (ISO,),
    )
    c.execute(
        "INSERT INTO application_event (application_id, at, status, note, source) "
        "VALUES (1, ?, 'interested', ?, 'manual')",
        (ISO, inbox.SHORTLIST_NOTE),
    )
    c.execute(
        "INSERT INTO fill_session (application_id, ats, host, started_at, last_seen_at) "
        "VALUES (1, 'generic', 'h', ?, ?)",
        (ISO, ISO),
    )
    inbox._remove_label(c, 1)  # without the fill_session check, the FK would refuse the DELETE
    assert c.execute("SELECT COUNT(*) FROM application WHERE id = 1").fetchone()[0] == 1
