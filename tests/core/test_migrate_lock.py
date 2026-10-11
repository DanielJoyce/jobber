"""Cross-process migration lock (bug dd65f64).

Real child processes, no network. The parent holds ``<db>.migrate.lock`` until every child
reports (through ``on_wait``) that it is blocked on it, then releases it, so the children race
for the lock deterministically instead of by timing luck.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jobhunter.core import backup, db

REAL = db._load_migrations()
NEWEST = REAL[-1][0]
OLDER = REAL[-3][0]

CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from jobhunter.core import db

    path = Path(sys.argv[1])
    conn = db.connect(path)
    backups = []
    def waiting(lock):
        print("WAITING", flush=True)
    applied = db.migrate(
        conn,
        data_dir=path.parent,
        on_backup=lambda p: backups.append(str(p)),
        on_wait=waiting,
        lock_timeout=60,
    )
    conn.close()
    print(json.dumps({"applied": applied, "backups": backups}), flush=True)
    """
)


def _spawn(db_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(db_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def _hold(lock: Path) -> int:
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _race(db_path: Path, n: int = 2) -> list[dict]:
    """Start ``n`` children while the lock is held, wait until all are blocked, release."""
    fd = _hold(db.migrate_lock_path(db_path))
    procs = [_spawn(db_path) for _ in range(n)]
    try:
        for p in procs:
            line = p.stdout.readline()  # blocks until the child reports it is waiting
            assert line.strip() == "WAITING", (line, p.stderr.read() if p.poll() else "")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


def _make_db(path: Path, monkeypatch, version: int) -> None:
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in REAL if m[0] <= version])
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: REAL)


def _versions(path: Path) -> list[int]:
    conn = db.connect(path)
    try:
        return sorted(db.applied_versions(conn))
    finally:
        conn.close()


def test_two_processes_migrating_a_fresh_db_both_succeed_and_one_applies(tmp_path):
    path = tmp_path / "jh.db"
    results = _race(path)
    applied = sorted((r["applied"] for r in results), key=len)
    assert applied[0] == []
    assert applied[1] == [v for v, _, _ in REAL]
    assert _versions(path) == [v for v, _, _ in REAL]
    # A fresh (version 0) database is never backed up, by either process.
    assert all(r["backups"] == [] for r in results)


def test_two_processes_on_an_older_db_take_exactly_one_pre_migrate_backup(tmp_path, monkeypatch):
    path = tmp_path / "jh.db"
    _make_db(path, monkeypatch, OLDER)
    results = _race(path)
    winners = [r for r in results if r["applied"]]
    losers = [r for r in results if not r["applied"]]
    assert len(winners) == 1 and len(losers) == 1
    assert winners[0]["applied"] == [v for v, _, _ in REAL if v > OLDER]
    assert len(winners[0]["backups"]) == 1
    assert losers[0]["backups"] == []  # the waiter found nothing pending: no backup
    files = sorted(p.name for p in backup.auto_dir(tmp_path).iterdir())
    assert len(files) == 1 and files[0].startswith(f"pre-migrate-{OLDER}-{NEWEST}-")
    assert _versions(path) == [v for v, _, _ in REAL]


def test_a_waiter_finds_nothing_pending_once_the_holder_has_migrated(tmp_path, monkeypatch):
    """The parent plays the other migrator: it holds the lock, migrates, then releases."""
    path = tmp_path / "jh.db"
    _make_db(path, monkeypatch, OLDER)
    lock = db.migrate_lock_path(path)
    fd = _hold(lock)
    child = _spawn(path)
    try:
        assert child.stdout.readline().strip() == "WAITING"
        conn = db.connect(path)
        # Holding the lock ourselves; apply directly as the "other process" would.
        assert db._apply_pending(conn) == [v for v, _, _ in REAL if v > OLDER]
        conn.close()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    out, err = child.communicate(timeout=120)
    assert child.returncode == 0, err
    assert json.loads(out.strip().splitlines()[-1]) == {"applied": [], "backups": []}
    assert not backup.auto_dir(tmp_path).exists()


def test_lock_wait_is_bounded_and_nothing_is_written(tmp_path, monkeypatch):
    path = tmp_path / "jh.db"
    _make_db(path, monkeypatch, OLDER)
    fd = _hold(db.migrate_lock_path(path))  # another open file description: conflicts
    waits: list[Path] = []
    conn = db.connect(path)
    try:
        with pytest.raises(db.MigrationLockTimeout, match="another jobhunter process"):
            db.migrate(conn, on_wait=waits.append, lock_timeout=0.3)
        assert waits == [db.migrate_lock_path(path)]
        assert max(db.applied_versions(conn)) == OLDER
    finally:
        conn.close()
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert not backup.auto_dir(tmp_path).exists()


def test_schema_guard_still_runs_before_the_lock(tmp_path, monkeypatch):
    """A newer database is refused at once, even while another process holds the lock."""
    path = tmp_path / "jh.db"
    _make_db(path, monkeypatch, NEWEST)
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO schema_version(version, name, applied_at) VALUES (?, 'future', 't')",
        (NEWEST + 1,),
    )
    fd = _hold(db.migrate_lock_path(path))
    try:
        with pytest.raises(db.SchemaTooNew):
            db.migrate(conn, lock_timeout=0.1)
    finally:
        conn.close()
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lock_path_is_resolved_so_symlinked_twins_share_it(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    twin = tmp_path / "twin"
    twin.symlink_to(real)
    assert db.migrate_lock_path(twin / "jh.db") == db.migrate_lock_path(real / "jh.db")
    assert db.migrate_lock_path(real / "jh.db").name == "jh.db.migrate.lock"


def test_memory_database_takes_no_lock(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("in-memory migrate must not lock")

    monkeypatch.setattr(db, "migrate_lock", boom)
    conn = db.connect(":memory:")
    assert db.migrate(conn) == [v for v, _, _ in REAL]


def test_enable_wal_retries_a_busy_switch_instead_of_failing_connect():
    """Two processes opening one fresh file race for WAL; SQLite can say "locked" at once."""

    class Conn:
        calls = 0

        def execute(self, sql):
            Conn.calls += 1
            if Conn.calls < 3:
                raise sqlite3.OperationalError("database is locked")

    db._enable_wal(Conn(), timeout_s=5)
    assert Conn.calls == 3


def test_enable_wal_does_not_swallow_other_errors():
    class Conn:
        def execute(self, sql):
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db._enable_wal(Conn(), timeout_s=5)


MIGRATOR = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from jobhunter.core import db

    path = Path(sys.argv[1])
    conn = db.connect(path)
    with db.migrate_lock(path):
        print("LOCKED", flush=True)
        sys.stdin.readline()  # the parent says when to go on
        db._apply_pending(conn)
    conn.close()
    """
)


@pytest.fixture
def migrator(tmp_path, monkeypatch):
    """Another process that holds the migrate lock on an older database until told to migrate."""
    path = tmp_path / "jobhunter.db"
    _make_db(path, monkeypatch, OLDER)
    proc = subprocess.Popen(
        [sys.executable, "-c", MIGRATOR, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "LOCKED"
    yield path, proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(10)


def _finish(proc: subprocess.Popen[str]) -> None:
    proc.stdin.write("go\n")
    proc.stdin.flush()
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err


def test_a_console_starting_during_another_migration_waits_then_serves(tmp_path, migrator):
    import logging
    import threading

    from fastapi.testclient import TestClient

    from jobhunter.config import Paths, Settings
    from jobhunter.console import app as console_app

    path, proc = migrator
    settings = Settings(paths=Paths(data_dir=tmp_path, db_path=path, profile_dir=tmp_path / "p"))
    waiting = threading.Event()

    class Seen(logging.Handler):
        def emit(self, record):
            if "waiting for another migration" in record.getMessage():
                waiting.set()

    handler = Seen()
    console_app.logger.addHandler(handler)
    built: list = []
    errors: list[BaseException] = []

    def start():
        try:
            built.append(console_app.create_app(settings))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    try:
        assert waiting.wait(30), errors  # blocked on the other process's migrate lock
        assert thread.is_alive()
        _finish(proc)
        thread.join(60)
    finally:
        console_app.logger.removeHandler(handler)
    assert not thread.is_alive() and not errors, errors
    console = built[0]
    with TestClient(console) as client:
        assert client.get("/healthz").status_code == 200
    assert _versions(path) == [v for v, _, _ in REAL]
    assert not backup.auto_dir(tmp_path).exists()  # the console found nothing to migrate


def test_restore_waits_for_another_migration_before_writing(tmp_path, monkeypatch, migrator):
    """db restore (holding run + console exclusive) takes the migrate lock innermost; with a
    migration in progress elsewhere it waits, bounded, and writes nothing on timeout."""
    from typer.testing import CliRunner

    from jobhunter import cli
    from jobhunter.ops import restore as rst

    path, proc = migrator
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{tmp_path}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr(rst, "busy_report", lambda db_path: [])
    raw = sqlite3.connect(path)
    saved = tmp_path / "saved.db"
    raw.execute(f"VACUUM INTO '{saved}'")
    raw.close()
    called: list = []
    monkeypatch.setattr(rst, "restore", lambda *a, **k: called.append(a))
    monkeypatch.setattr(cli, "RESTORE_MIGRATE_WAIT_S", 0.3)

    result = CliRunner().invoke(cli.app, ["db", "restore", str(saved)])
    assert result.exit_code == 1, result.output
    assert "waiting for another jobhunter process to finish migrating" in result.output
    assert "another jobhunter process" in result.output
    assert called == []  # nothing written under the other process's migration
    # The run and console locks were released on the way out.
    from jobhunter.core import runlock

    probe = runlock.ConsoleLock(path, exclusive=True)
    probe.acquire()
    probe.release()
    _finish(proc)
