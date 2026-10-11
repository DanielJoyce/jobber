"""``jobhunter db backup`` and ``jobhunter db restore`` from the CLI (specs/018 C6)."""

from __future__ import annotations

import os
import re
import sqlite3
import stat

import pytest
from typer.testing import CliRunner

from jobhunter.cli import MIGRATION_EXEMPT_COMMANDS, app
from jobhunter.core import backup, db, runlock
from jobhunter.ops import restore as rst

runner = CliRunner()
REAL = db._load_migrations()
NEWEST = REAL[-1][0]
OLDER = REAL[-3][0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr(rst, "busy_report", lambda db_path: [])
    return data


def make_db(path, monkeypatch, version=NEWEST, rows=0):
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in REAL if m[0] <= version])
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("CREATE TABLE scratch(n)")
    conn.executemany("INSERT INTO scratch VALUES (?)", [(i,) for i in range(rows)])
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: REAL)
    return path


def rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM scratch").fetchone()[0]
    finally:
        conn.close()


def version_of(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()


def test_both_commands_are_migration_exempt():
    assert {"db backup", "db restore"} <= MIGRATION_EXEMPT_COMMANDS


def test_manual_backup_is_private_and_prunes_nothing(env, monkeypatch):
    make_db(env / "jobhunter.db", monkeypatch, rows=3)
    old_auto = env / "backups" / "auto" / "jobhunter-auto-20200101T000000Z.db"
    old_auto.parent.mkdir(parents=True)
    old_auto.write_text("x")
    for _ in range(2):
        result = runner.invoke(app, ["db", "backup"])
        assert result.exit_code == 0, result.output
    made = sorted((env / "backups").glob("jobhunter-*Z.db"))
    assert made and all(backup.AUTO_RE.match(p.name) is None for p in made)
    assert stat.S_IMODE(os.stat(made[0]).st_mode) == 0o600
    assert rows(made[0]) == 3
    assert old_auto.exists()
    assert "integrity_check ok" in result.output


def test_auto_backup_prunes_only_automatic_backups(env, monkeypatch):
    make_db(env / "jobhunter.db", monkeypatch, rows=2)
    autod = env / "backups" / "auto"
    autod.mkdir(parents=True)
    stale = [autod / f"jobhunter-auto-2020010{d}T030000Z.db" for d in range(1, 10)]
    pre = [autod / f"pre-migrate-1-2-2020010{d}T000000Z.db" for d in range(1, 3)]
    manual = env / "backups" / "jobhunter-20261009-160902-pre-jev.db"
    for path in [*stale, *pre, manual]:
        path.write_text("x")
    result = runner.invoke(app, ["db", "backup", "--auto"])
    assert result.exit_code == 0, result.output
    [new] = [p for p in autod.glob("jobhunter-auto-*Z.db") if p not in stale]
    assert rows(new) == 2
    left = [p for p in stale if p.exists()]
    # Daily: the new one plus Jan 9..4. Weekly: 2020 weeks 2 (Jan 9) and 1 (Jan 5), both
    # already kept. So Jan 1..3 go.
    assert len(left) == 6
    assert all(p.exists() for p in pre) and manual.exists()
    assert result.output.count("pruned ") == 3


def test_backup_to_path_refuses_to_overwrite(env, monkeypatch, tmp_path):
    make_db(env / "jobhunter.db", monkeypatch, rows=1)
    target = tmp_path / "out.db"
    assert runner.invoke(app, ["db", "backup", "--to", str(target)]).exit_code == 0
    assert rows(target) == 1
    again = runner.invoke(app, ["db", "backup", "--to", str(target)])
    assert again.exit_code == 1 and "already exists" in again.output
    into_dir = runner.invoke(app, ["db", "backup", "--to", str(tmp_path)])
    assert into_dir.exit_code == 0
    assert len(list(tmp_path.glob("jobhunter-*Z.db"))) == 1
    both = runner.invoke(app, ["db", "backup", "--auto", "--to", str(tmp_path / "x.db")])
    assert both.exit_code == 2


def test_backup_never_migrates_and_works_with_a_newer_database(env, monkeypatch):
    live = make_db(env / "jobhunter.db", monkeypatch, version=OLDER)
    assert runner.invoke(app, ["db", "backup"]).exit_code == 0
    assert version_of(live) == OLDER
    assert not list((env / "backups").glob("auto/pre-migrate-*"))
    raw = sqlite3.connect(live)
    raw.execute("INSERT INTO schema_version VALUES (?, 'future', 't')", (NEWEST + 1,))
    raw.commit()
    raw.close()
    assert runner.invoke(app, ["db", "backup", "--auto"]).exit_code == 0


def test_backup_without_a_database_fails(env):
    result = runner.invoke(app, ["db", "backup"])
    assert result.exit_code == 1 and "no database" in result.output
    assert not (env / "jobhunter.db").exists()


def test_backup_runs_in_container_mode(env, monkeypatch):
    make_db(env / "jobhunter.db", monkeypatch)
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    # Container mode moves the default paths to /data; this config sets them explicitly.
    assert runner.invoke(app, ["db", "backup", "--auto"]).exit_code == 0


# ─── restore ────────────────────────────────────────────────────────────────


@pytest.fixture
def restorable(env, monkeypatch, tmp_path):
    live = make_db(env / "jobhunter.db", monkeypatch, rows=5)
    conn = sqlite3.connect(live)
    saved = backup.online_backup(conn, tmp_path / "saved.db")
    conn.execute("INSERT INTO scratch VALUES (99)")
    conn.commit()
    conn.close()
    return live, saved


def test_restore_replaces_the_live_database_and_reports(env, restorable):
    live, saved = restorable
    result = runner.invoke(app, ["db", "restore", str(saved)])
    assert result.exit_code == 0, result.output
    assert rows(live) == 5
    [safety] = (env / "backups").glob("jobhunter-*Z-pre-restore.db")
    assert rows(safety) == 6
    assert "integrity_check ok" in result.output
    assert re.search(r"^  scratch +6 -> 5$", result.output, re.M)
    after = runlock.RunLock(live, "t")  # released again
    assert after.try_acquire()
    after.release()


def test_restore_refuses_in_container_mode(env, restorable, monkeypatch):
    live, saved = restorable
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    result = runner.invoke(app, ["db", "restore", str(saved)])
    assert result.exit_code == 2
    assert "does not run in container mode" in result.output
    assert rows(live) == 6


def test_restore_refuses_while_the_run_lock_is_held(env, restorable):
    live, saved = restorable
    with runlock.RunLock(live, "run"):
        result = runner.invoke(app, ["db", "restore", str(saved)])
    assert result.exit_code == 1
    assert "another jobhunter job is running" in result.output
    assert rows(live) == 6
    assert not (env / "backups").exists()


def test_restore_refuses_while_in_use_unless_forced(env, restorable, monkeypatch):
    live, saved = restorable
    monkeypatch.setattr(rst, "busy_report", lambda db_path: ["console running: 4242 (x)"])
    refused = runner.invoke(app, ["db", "restore", str(saved)])
    assert refused.exit_code == 1
    assert "console running: 4242" in refused.output and "systemctl --user stop" in refused.output
    assert rows(live) == 6
    forced = runner.invoke(app, ["db", "restore", "--force", str(saved)])
    assert forced.exit_code == 0, forced.output
    assert "WARNING" in forced.output
    assert rows(live) == 5


def test_restore_refuses_a_newer_backup(env, restorable):
    live, saved = restorable
    raw = sqlite3.connect(saved)
    raw.execute("INSERT INTO schema_version VALUES (?, 'future', 't')", (NEWEST + 1,))
    raw.commit()
    raw.close()
    result = runner.invoke(app, ["db", "restore", str(saved)])
    assert result.exit_code == 1 and "newer than this jobhunter" in result.output
    assert rows(live) == 6


def test_restore_of_an_older_backup_does_not_migrate_it(env, monkeypatch, tmp_path):
    live = make_db(env / "jobhunter.db", monkeypatch, rows=1)
    old = make_db(tmp_path / "old.db", monkeypatch, version=OLDER, rows=2)
    conn = sqlite3.connect(old)
    saved = backup.online_backup(conn, tmp_path / "old-saved.db")
    conn.close()
    result = runner.invoke(app, ["db", "restore", str(saved)])
    assert result.exit_code == 0, result.output
    assert version_of(live) == OLDER
    assert "migrates it" in result.output
    assert not list((env / "backups").glob("auto/pre-migrate-*"))
