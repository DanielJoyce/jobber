"""`jobhunter db relativize-paths` (specs/018 C14): dry run by default, --apply backs up first."""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db

runner = CliRunner()
ISO = "2026-10-10T12:00:00+00:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Data dir configured through a symlinked twin (``home`` -> ``var/home``); the rows hold
    the realpath form, as ``.resolve()`` wrote them. Returns (configured, real, outside)."""
    (tmp_path / "var" / "home" / "you").mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "var" / "home")
    configured = tmp_path / "home" / "you" / "data"
    real = tmp_path / "var" / "home" / "you" / "data"
    (real / "packets" / "1").mkdir(parents=True)
    (real / "packets" / "1" / "r.pdf").write_bytes(b"resume")
    outside = tmp_path / "Documents"
    outside.mkdir()
    (outside / "cl.pdf").write_bytes(b"letter")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{configured}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    conn = db.connect(configured / "jobhunter.db")
    db.migrate(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO application (id, job_group_id, status, cover_letter_path, created_at, "
        "updated_at) VALUES (1, 1, 'applied', ?, ?, ?)",
        (str(outside / "cl.pdf"), ISO, ISO),
    )
    conn.execute(
        "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (1, 'resume', ?, ?)",
        (str(real / "packets" / "1" / "r.pdf"), ISO),
    )
    conn.close()
    return configured, real, outside


def rows(real):
    raw = sqlite3.connect(real / "jobhunter.db")
    try:
        return (
            raw.execute("SELECT path FROM attachment").fetchall(),
            raw.execute("SELECT cover_letter_path FROM application").fetchall(),
        )
    finally:
        raw.close()


def test_dry_run_lists_changes_and_writes_nothing(env):
    _, real, outside = env
    before = rows(real)
    result = runner.invoke(app, ["db", "relativize-paths"])
    assert result.exit_code == 0, result.output
    assert f"{real / 'packets/1/r.pdf'} -> packets/1/r.pdf" in result.output
    assert f"copy {outside / 'cl.pdf'} -> attachments/cl.pdf" in result.output
    assert "2 path(s) to fix: 1 under the data dir, 1 to copy" in result.output
    assert "dry run: nothing written" in result.output
    assert rows(real) == before
    assert not (real / "backups").exists() and not (real / "attachments").exists()


def test_apply_backs_up_first_then_rewrites(env):
    _, real, _ = env
    before = rows(real)
    result = runner.invoke(app, ["db", "relativize-paths", "--apply"])
    assert result.exit_code == 0, result.output
    assert rows(real) == ([("packets/1/r.pdf",)], [("attachments/cl.pdf",)])
    assert (real / "attachments" / "cl.pdf").read_bytes() == b"letter"
    [saved] = (real / "backups").glob("jobhunter-*-pre-relativize-paths.db")
    raw = sqlite3.connect(saved)
    assert (
        raw.execute("SELECT path FROM attachment").fetchall(),
        raw.execute("SELECT cover_letter_path FROM application").fetchall(),
    ) == before
    raw.close()
    assert (
        "rewrote 2 row(s), removed 0 duplicate(s), copied 1 file(s); 0 path(s) still"
        in result.output
    )
    # nothing left: a second run makes no backup
    again = runner.invoke(app, ["db", "relativize-paths", "--apply"])
    assert again.exit_code == 0 and "nothing to change" in again.output
    assert len(list((real / "backups").glob("jobhunter-*.db"))) == 1


def test_apply_exits_1_while_an_unresolvable_path_remains(env):
    _, real, outside = env
    (outside / "cl.pdf").unlink()
    result = runner.invoke(app, ["db", "relativize-paths", "--apply"])
    assert result.exit_code == 1, result.output
    assert "cannot resolve" in result.output and "1 path(s) still to fix" in result.output
    assert rows(real)[0] == [("packets/1/r.pdf",)]


def test_refused_in_container_mode(env, monkeypatch):
    _, real, _ = env
    before = rows(real)
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    result = runner.invoke(app, ["db", "relativize-paths", "--apply"])
    assert result.exit_code == 2
    assert "`jobhunter db relativize-paths` does not run in container mode" in result.output
    assert rows(real) == before
