"""Migration 0030 (packet_document.runner and api_equiv_usd, specs/017 phase 1b)."""

from __future__ import annotations

import sqlite3

import pytest

from jobhunter.core import db

ISO = "2026-10-01T12:00:00+00:00"


def _upto(monkeypatch, version):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= version])
    return real


def test_0030_keeps_rows_and_adds_runner_with_its_check(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "t.db")
    real = _upto(monkeypatch, 29)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    c.execute(
        "INSERT INTO job_group (id, canonical_job_id, member_count, method, created_at) "
        "VALUES (1, NULL, 1, 'exact_hash', ?)",
        (ISO,),
    )
    c.execute(
        "INSERT INTO application (id, job_group_id, status, created_at, updated_at) "
        "VALUES (1, 1, 'preparing', ?, ?)",
        (ISO, ISO),
    )
    c.execute(
        "INSERT INTO application_packet (id, application_id, created_at, updated_at) "
        "VALUES (1, 1, ?, ?)",
        (ISO, ISO),
    )
    c.execute(
        "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
        "check_report, created_at) VALUES (1, 'resume', 1, 'base', '{}', '', '{}', ?)",
        (ISO,),
    )
    monkeypatch.setattr(db, "_load_migrations", real)
    assert db.migrate(c) == [30]
    row = c.execute("SELECT runner, api_equiv_usd, kind FROM packet_document").fetchone()
    assert tuple(row) == (None, None, "resume")
    for runner in ("cli", "api"):
        c.execute(
            "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
            "check_report, created_at, runner) VALUES (1, 'resume', ?, 'generated', '{}', '', "
            "'{}', ?, ?)",
            (2 if runner == "cli" else 3, ISO, runner),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
            "check_report, created_at, runner) VALUES (1, 'resume', 9, 'generated', '{}', '', "
            "'{}', ?, 'bedrock')",
            (ISO,),
        )
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
