"""The console presence lock (C6 review): a running console blocks `db restore` even when it
is invisible in /proc (another PID namespace), and a restore blocks a console from starting."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Paths, Settings
from jobhunter.console.app import create_app
from jobhunter.core import backup, db, runlock
from jobhunter.ops import restore as rst

runner = CliRunner()

HOLDER = (
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600); "
    "fcntl.flock(fd, fcntl.LOCK_SH); print('ready', flush=True); sys.stdin.read()"
)


def settings_for(tmp_path):
    return Settings(
        paths=Paths(
            data_dir=tmp_path, db_path=tmp_path / "jobhunter.db", profile_dir=tmp_path / "p"
        )
    )


def exclusive_free(db_path) -> bool:
    probe = runlock.ConsoleLock(db_path, exclusive=True)
    try:
        probe.acquire()
    except runlock.ConsoleBusy:
        return False
    probe.release()
    return True


@pytest.fixture
def shared_holder(tmp_path):
    """Another process holding the console lock shared, as a console elsewhere would."""
    db_path = tmp_path / "jobhunter.db"
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(runlock.console_lock_path(db_path))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    yield db_path, proc
    if proc.poll() is None:
        proc.stdin.close()
        proc.wait(10)


def test_a_lock_held_by_another_process_is_seen(shared_holder):
    db_path, proc = shared_holder
    with pytest.raises(runlock.ConsoleBusy, match=r"console is running .*console\.lock"):
        runlock.ConsoleLock(db_path, exclusive=True).acquire()
    proc.stdin.close()
    proc.wait(10)
    assert exclusive_free(db_path)  # the kernel dropped it when the holder exited


def test_create_app_holds_the_lock_until_shutdown(tmp_path):
    settings = settings_for(tmp_path)
    console = create_app(settings)
    assert not exclusive_free(settings.paths.db_path)
    with TestClient(console) as client:  # runs the lifespan, shutdown at exit
        assert client.get("/healthz").status_code == 200
        assert not exclusive_free(settings.paths.db_path)
    assert exclusive_free(settings.paths.db_path)


def test_a_console_does_not_start_during_a_restore(tmp_path):
    settings = settings_for(tmp_path)
    create_app(settings).state.console_lock.release()  # make the database
    restoring = runlock.ConsoleLock(settings.paths.db_path, exclusive=True)
    restoring.acquire()
    try:
        with pytest.raises(runlock.ConsoleBusy, match="restore is in progress"):
            create_app(settings)
    finally:
        restoring.release()


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{tmp_path}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    # /proc finds nothing, as from another PID namespace: only the lock can tell.
    monkeypatch.setattr(rst, "busy_report", lambda db_path: [])
    return tmp_path


def test_restore_refuses_while_a_console_elsewhere_holds_the_lock(env, shared_holder):
    db_path, proc = shared_holder
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute("CREATE TABLE scratch(n)")
    saved = backup.online_backup(conn, env / "saved.db")
    conn.execute("INSERT INTO scratch VALUES (1)")
    conn.close()

    refused = runner.invoke(app, ["db", "restore", str(saved)])
    assert refused.exit_code == 1
    assert "console.lock" in refused.output and "Nothing was changed" in refused.output
    assert not (env / "backups").exists()

    forced = runner.invoke(app, ["db", "restore", "--force", str(saved)])
    assert forced.exit_code == 0, forced.output
    assert "WARNING" in forced.output

    proc.stdin.close()
    proc.wait(10)
    assert runner.invoke(app, ["db", "restore", str(saved)]).exit_code == 0


def test_a_symlinked_database_gives_one_console_lock_and_one_run_lock(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    settings = Settings(
        paths=Paths(
            data_dir=linked_dir, db_path=linked_dir / "jobhunter.db", profile_dir=tmp_path / "p"
        )
    )
    console = create_app(settings)
    file_link = tmp_path / "file-link.db"
    file_link.symlink_to(real_dir / "jobhunter.db")
    for spelling in (real_dir / "jobhunter.db", file_link):
        assert not exclusive_free(spelling), spelling
    console.state.console_lock.release()
    assert exclusive_free(real_dir / "jobhunter.db")

    with runlock.RunLock(file_link, "run"), pytest.raises(runlock.LockHeld):
        runlock.RunLock(real_dir / "jobhunter.db", "collect").acquire(None)


def open_fds() -> int:
    import os

    return len(os.listdir("/proc/self/fd"))


def test_an_unusable_lock_file_lets_the_console_start_but_stops_a_restore(tmp_path, caplog):
    db_path = tmp_path / "jobhunter.db"
    runlock.console_lock_path(db_path).mkdir()  # cannot be opened as a file
    before = open_fds()
    shared = runlock.ConsoleLock(db_path)
    with caplog.at_level("WARNING"):
        shared.acquire()  # no exception: the console goes on
    assert not shared.held
    assert "running without it" in caplog.text
    with pytest.raises(runlock.ConsoleBusy, match="cannot be ruled out"):
        runlock.ConsoleLock(db_path, exclusive=True).acquire()
    assert open_fds() == before  # nothing leaked


def test_restore_refuses_when_it_cannot_take_the_console_lock(env):
    conn = db.connect(env / "jobhunter.db")
    db.migrate(conn)
    saved = backup.online_backup(conn, env / "saved.db")
    conn.close()
    runlock.console_lock_path(env / "jobhunter.db").mkdir()
    refused = runner.invoke(app, ["db", "restore", str(saved)])
    assert refused.exit_code == 1 and "cannot be ruled out" in refused.output
    assert runner.invoke(app, ["db", "restore", "--force", str(saved)]).exit_code == 0
