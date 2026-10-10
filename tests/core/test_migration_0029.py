"""Migration 0029 (application packets, specs/017) on a database with real-shaped rows."""

from __future__ import annotations

import pytest

from jobhunter.core import db

ISO = "2026-10-01T12:00:00+00:00"
COUNTED = ("job", "job_group", "application", "application_event", "label", "rejection")


def _upto(monkeypatch, version):
    real = db._load_migrations
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= version])
    return real


def _seed(c):
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy, status) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled', 'ok'), "
        "('email-manual', 'C', 'Added from email', 'manual', 'manual', 'gmail', 'manual', "
        "'manual')"
    )
    for n, src in ((1, "wa"), (2, "wa"), (3, "email-manual")):
        c.execute(
            "INSERT INTO job_group (id, canonical_job_id, member_count, method, created_at) "
            "VALUES (?, NULL, 1, ?, ?)",
            (n, "manual" if src == "email-manual" else "exact_hash", ISO),
        )
        c.execute(
            "INSERT INTO job (id, source_key, external_id, url, title, employer, job_group_id, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 'Analyst', 'Acme', ?, ?, ?)",
            (n, src, f"x{n}", f"https://example.com/{n}", n, ISO, ISO),
        )
        c.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (n, n))
    c.execute("INSERT INTO label VALUES (1, 'interesting', NULL, ?)", (ISO,))
    for app_id, gid, status in ((1, 1, "interested"), (2, 3, "applied")):
        c.execute(
            "INSERT INTO application (id, job_group_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (app_id, gid, status, ISO, ISO),
        )
        c.execute(
            "INSERT INTO application_event (application_id, at, status, source) "
            "VALUES (?, ?, ?, 'manual')",
            (app_id, ISO, status),
        )
    c.execute(
        "INSERT INTO rejection (received_at, employer, employer_norm, title, job_group_id, "
        "application_id, source, created_at) VALUES (?, 'Acme', 'acme', 'Analyst', 3, 2, "
        "'manual', ?)",
        (ISO, ISO),
    )


def test_0029_adds_packet_tables_and_keeps_every_row(monkeypatch):
    real = _upto(monkeypatch, 28)
    c = db.connect(":memory:")
    db.migrate(c)
    _seed(c)
    before = {t: c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in COUNTED}
    before = {t: [tuple(r) for r in rows] for t, rows in before.items()}

    # Only 0029: later migrations add columns of their own (0031, 0032).
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in real() if m[0] <= 29])
    assert 29 in db.migrate(c)

    after = {
        t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()]
        for t in COUNTED
    }
    assert after == before
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("application_packet", "packet_document", "packet_answer"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # FKs hold: a packet needs a real application, a document a real packet.
    with pytest.raises(Exception, match="FOREIGN KEY"):
        c.execute(
            "INSERT INTO application_packet (application_id, created_at, updated_at) "
            "VALUES (999, ?, ?)",
            (ISO, ISO),
        )
    pid = c.execute(
        "INSERT INTO application_packet (application_id, created_at, updated_at) VALUES (1, ?, ?)",
        (ISO, ISO),
    ).lastrowid
    assert c.execute("SELECT status FROM application_packet").fetchone()[0] == "draft"
    with pytest.raises(Exception, match="CHECK"):
        c.execute("UPDATE application_packet SET status = 'sent' WHERE id = ?", (pid,))
    with pytest.raises(Exception, match="CHECK"):
        c.execute(
            "INSERT INTO packet_answer (packet_id, field_key, source) VALUES (?, 'k', 'model')",
            (pid,),
        )
    c.close()


def test_0029_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    assert db.migrate(c) == []
    assert db.current_version(c) >= 29
    c.close()
