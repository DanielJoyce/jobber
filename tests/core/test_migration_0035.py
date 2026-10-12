"""Migration 0035 (achievements storage, specs/021 "Data", ticket A1).

Run on a database shaped like a real one at 0034: contacts with no dates, events and
rejections from before the migration, next actions set (and blank) on applications.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobhunter.core import db

ISO = "2026-10-01T12:00:00+00:00"


def _upto(monkeypatch, version):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= version])
    return real


def _seed_0034(c):
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for gid, next_at in ((1, "2026-10-14"), (2, ""), (3, None), (4, "2026-10-20T09:00:00+00:00")):
        c.execute(
            "INSERT INTO job_group (id, canonical_job_id, member_count, method, created_at) "
            "VALUES (?, NULL, 1, 'exact_hash', ?)",
            (gid, ISO),
        )
        c.execute(
            "INSERT INTO application (id, job_group_id, status, next_action, next_action_at, "
            "created_at, updated_at) VALUES (?, ?, 'applied', 'call', ?, ?, ?)",
            (gid, gid, next_at, ISO, ISO),
        )
        c.execute(
            "INSERT INTO application_event (application_id, at, status, source) "
            "VALUES (?, ?, 'applied', 'email')",
            (gid, ISO),
        )
    c.execute("INSERT INTO contact (application_id, name) VALUES (1, 'A Recruiter')")
    c.execute(
        "INSERT INTO rejection (gmail_message_id, received_at, source, created_at, state) "
        "VALUES ('m1', ?, 'email', ?, 'confirmed')",
        (ISO, ISO),
    )


def _counts(c):
    return {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("application", "application_event", "contact", "rejection", "job_group")
    }


def test_0035_adds_tables_and_null_columns_and_backfills_next_actions(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "t.db")
    real = _upto(monkeypatch, 34)
    db.migrate(c)
    _seed_0034(c)
    before = _counts(c)
    monkeypatch.setattr(db, "_load_migrations", real)
    assert 35 in db.migrate(c)
    assert _counts(c) == before

    # Rows written before the migration carry no time: they predate it.
    assert c.execute("SELECT created_at FROM contact").fetchone()[0] is None
    assert {r[0] for r in c.execute("SELECT recorded_at FROM application_event")} == {None}
    assert c.execute("SELECT confirmed_at FROM rejection").fetchone()[0] is None

    # One backfill 'planned' row per application with a non-empty next action date.
    rows = c.execute(
        "SELECT application_id, kind, due_at, backfill, at FROM followup_log ORDER BY id"
    ).fetchall()
    assert [tuple(r)[:4] for r in rows] == [
        (1, "planned", "2026-10-14", 1),
        (4, "planned", "2026-10-20T09:00:00+00:00", 1),
    ]
    assert all(r["at"].endswith("+00:00") and r["at"][10] == "T" for r in rows)

    for table in ("achievement_unlock", "achievement_setting"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def test_0035_constraints(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    _seed_0034(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO followup_log (application_id, at, kind) VALUES (1, ?, 'reminded')", (ISO,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO followup_log (application_id, at, kind, backfill) "
            "VALUES (1, ?, 'done', 2)",
            (ISO,),
        )
    with pytest.raises(sqlite3.IntegrityError):  # foreign key to application
        c.execute(
            "INSERT INTO followup_log (application_id, at, kind) VALUES (999, ?, 'done')", (ISO,)
        )
    unlock = (
        "INSERT INTO achievement_unlock (achievement_id, tier, unlocked_at, detected_at, "
        "evidence, catalog_version) VALUES ('apply.momentum', 1, ?, ?, '{}', 1)"
    )
    c.execute(unlock, (ISO, ISO))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(unlock, (ISO, ISO))
    c.execute(
        "INSERT INTO achievement_setting (key, value, updated_at) VALUES ('enabled', '1', ?)",
        (ISO,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO achievement_setting (key, value, updated_at) VALUES ('enabled', '0', ?)",
            (ISO,),
        )
    c.close()


def test_0035_on_a_file_database_is_backed_up_first(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    c = db.connect(path)
    real = _upto(monkeypatch, 34)
    db.migrate(c)
    _seed_0034(c)
    monkeypatch.setattr(db, "_load_migrations", real)
    assert db.migrate(c, data_dir=tmp_path)
    [saved] = (tmp_path / "backups" / "auto").glob("pre-migrate-34-*Z.db")
    old = sqlite3.connect(saved)
    assert old.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 34
    old.close()
    c.close()
