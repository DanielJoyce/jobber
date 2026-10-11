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
