"""Schema version guard, pre-migrate backups, deployment marker (specs/018 Version skew, C12)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobhunter.core import backup, db

REAL = db._load_migrations()
NEWEST = REAL[-1][0]
OLDER = REAL[-3][0]  # a schema two migrations behind this code


def _at_version(monkeypatch, version: int):
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in REAL if m[0] <= version])


def make_db(path: Path, monkeypatch, version: int = NEWEST) -> Path:
    """A file database migrated up to ``version`` (as an older jobhunter left it)."""
    _at_version(monkeypatch, version)
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO source(key, class, name, family, tier, entry, policy) "
        "VALUES ('s1', 'A', 'n', 'vos', 'http', 'e', 'enabled')"
    )
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: REAL)
    return path


def add_fake_version(path: Path, version: int) -> None:
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO schema_version(version, name, applied_at) VALUES (?, 'from_the_future', 't')",
        (version,),
    )
    raw.commit()
    raw.close()


def digest(path: Path) -> str:
    # Fold the WAL in first so the comparison sees committed content, not file layout.
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dump = "\n".join(raw.iterdump())
    raw.close()
    return hashlib.sha256(dump.encode()).hexdigest()


# --- guard -----------------------------------------------------------------------------


def test_connect_refuses_a_database_newer_than_the_code_and_writes_nothing(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch)
    add_fake_version(path, NEWEST + 1)
    before = digest(path)
    with pytest.raises(db.SchemaTooNew) as err:
        db.connect(path)
    msg = str(err.value)
    assert (
        f"database is newer than this jobhunter (code has {NEWEST}, database has {NEWEST + 1})"
        in msg
    )
    assert digest(path) == before
    assert not (tmp_path / "backups").exists()


def test_guard_names_an_unknown_lower_numbered_migration(tmp_path, monkeypatch):
    # A migration from an unmerged branch below the newest number is still unknown.
    path = make_db(tmp_path / "jh.db", monkeypatch)
    gap = next(v for v in range(1, NEWEST) if v not in {m[0] for m in REAL})
    add_fake_version(path, gap)
    with pytest.raises(db.SchemaTooNew, match=rf"unknown migration\(s\): {gap}\b"):
        db.connect(path)


def test_migrate_refuses_newer_even_on_an_unchecked_connection(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch)
    add_fake_version(path, NEWEST + 1)
    conn = db.connect(path, check_schema=False)
    with pytest.raises(db.SchemaTooNew):
        db.migrate(conn)
    conn.close()


def test_guard_runs_on_every_new_connection_after_a_concurrent_migration(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch)
    db.connect(path).close()  # fine at the same schema
    add_fake_version(path, NEWEST + 1)  # a newer jobhunter migrated meanwhile
    with pytest.raises(db.SchemaTooNew):
        db.connect(path)


def test_new_and_current_databases_pass(tmp_path, monkeypatch):
    db.connect(tmp_path / "new.db").close()
    path = make_db(tmp_path / "jh.db", monkeypatch)
    db.connect(path).close()
    db.connect(":memory:").close()


# --- pre-migrate backup -----------------------------------------------------------------


def test_backup_is_taken_before_the_first_migration_runs(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch, OLDER)
    seen: list[tuple[int, Path]] = []
    real_backup = backup.pre_migrate_backup

    def spy(conn, data_dir, from_v, to_v, now=None):
        seen.append((db.current_version(conn), real_backup(conn, data_dir, from_v, to_v, now)))
        return seen[-1][1]

    monkeypatch.setattr(backup, "pre_migrate_backup", spy)
    conn = db.connect(path)
    applied = db.migrate(conn, now=datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC))
    conn.close()
    assert applied == [v for v, _, _ in REAL if v > OLDER]
    [(version_at_backup, saved)] = seen
    assert version_at_backup == OLDER  # nothing had been applied yet
    assert (
        saved == tmp_path / "backups" / "auto" / f"pre-migrate-{OLDER}-{NEWEST}-20261010T120000Z.db"
    )
    assert saved.stat().st_mode & 0o777 == 0o600
    check = sqlite3.connect(saved)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert check.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == OLDER
    assert check.execute("SELECT key FROM source").fetchall() == [("s1",)]
    check.close()
    assert not list(saved.parent.glob(".*tmp"))


def test_no_backup_for_a_new_database_or_when_nothing_is_pending(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "jh.db")
    assert db.migrate(conn)  # version 0: a new database has nothing to lose
    assert db.migrate(conn) == []  # up to date: nothing to back up
    conn.close()
    assert not (tmp_path / "backups").exists()


def test_backup_goes_under_the_given_data_dir(tmp_path, monkeypatch):
    path = make_db(tmp_path / "db" / "jh.db", monkeypatch, OLDER)
    data_dir = tmp_path / "data"
    conn = db.connect(path)
    db.migrate(conn, data_dir=data_dir)
    conn.close()
    assert len(list((data_dir / "backups" / "auto").glob("pre-migrate-*.db"))) == 1
    assert not (tmp_path / "db" / "backups").exists()


def test_a_failed_backup_stops_the_migration(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch, OLDER)

    def broken(*_a, **_k):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "pre_migrate_backup", broken)
    conn = db.connect(path)
    with pytest.raises(backup.BackupError):
        db.migrate(conn)
    assert db.current_version(conn) == OLDER
    conn.close()


def test_pruning_keeps_the_newest_three_and_touches_nothing_else(tmp_path):
    auto = tmp_path / "backups" / "auto"
    auto.mkdir(parents=True)
    names = [
        f"pre-migrate-{n}-{n + 1}-202610{d:02d}T010203Z.db"
        for n, d in ((5, 1), (6, 2), (7, 3), (8, 4), (9, 5))
    ]
    others = [
        "jobhunter-auto-20261001T010203Z.db",  # C6's dailies
        "jobhunter-20261009-120000-pre-jev.db",  # a manual-shaped name
        "pre-migrate-1-2-20200101T000000Z.db.bak",
        "notes.txt",
    ]
    for name in names + others:
        (auto / name).write_text("x")
    removed = backup.prune_pre_migrate(auto)
    assert sorted(p.name for p in removed) == sorted(names[:2])
    assert sorted(p.name for p in auto.iterdir()) == sorted(names[2:] + others)


def test_manual_backups_outside_auto_are_never_touched(tmp_path, monkeypatch):
    manual = tmp_path / "backups" / "jobhunter-20261009-120000-pre-jev.db"
    manual.parent.mkdir(parents=True)
    manual.write_text("x")
    for i in range(5):
        path = make_db(tmp_path / f"jh{i}.db", monkeypatch, OLDER)
        conn = db.connect(path)
        db.migrate(conn, data_dir=tmp_path, now=datetime(2026, 10, 10, 12, 0, i, tzinfo=UTC))
        conn.close()
    assert manual.read_text() == "x"
    assert len(list((tmp_path / "backups" / "auto").iterdir())) == 3


# --- deployment marker ------------------------------------------------------------------


def test_marker_blocks_automatic_migration(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch, OLDER)
    (tmp_path / "deployment.toml").write_text('mode = "container"\n')
    before = digest(path)
    conn = db.connect(path)
    with pytest.raises(db.MigrationsPending, match="run `jobhunter upgrade`"):
        db.migrate(conn)
    conn.close()
    assert digest(path) == before
    assert not (tmp_path / "backups").exists()


def test_an_unparseable_marker_still_blocks(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch, OLDER)
    (tmp_path / "deployment.toml").write_text("this is [not toml")
    conn = db.connect(path)
    with pytest.raises(db.MigrationsPending):
        db.migrate(conn)
    conn.close()


def test_explicit_migrate_works_with_the_marker_and_backs_up(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch, OLDER)
    (tmp_path / "deployment.toml").write_text('mode = "container"\n')
    conn = db.connect(path)
    assert db.migrate(conn, explicit=True)
    assert db.current_version(conn) == NEWEST
    conn.close()
    assert len(list((tmp_path / "backups" / "auto").glob("pre-migrate-*.db"))) == 1


def test_marker_does_not_block_a_current_database_or_memory(tmp_path, monkeypatch):
    path = make_db(tmp_path / "jh.db", monkeypatch)
    (tmp_path / "deployment.toml").write_text('mode = "container"\n')
    conn = db.connect(path)
    assert db.migrate(conn) == []
    conn.close()
    mem = db.connect(":memory:")
    assert db.migrate(mem, data_dir=tmp_path)  # an in-memory scratch database still builds
    mem.close()
