"""CLI and console behaviour of the schema guard (specs/018 Version skew, C12)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter import cli
from jobhunter.cli import app
from jobhunter.config import Paths, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

runner = CliRunner()
REAL = db._load_migrations()
NEWEST = REAL[-1][0]
OLDER = REAL[-3][0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A config whose data dir (marker, backups) and database are both under tmp_path."""
    data = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    return data


def make_db(path, monkeypatch, version):
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in REAL if m[0] <= version])
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: REAL)


def version_of(path) -> int:
    raw = sqlite3.connect(path)
    try:
        return raw.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        raw.close()


def add_fake_version(path, version):
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO schema_version VALUES (?, 'future', 't')", (version,))
    raw.commit()
    raw.close()


def test_host_install_keeps_migrating_automatically_after_a_backup(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, OLDER)
    result = runner.invoke(app, ["apply", "stats"])
    assert result.exit_code == 0, result.output
    assert version_of(path) == NEWEST
    [saved] = (env / "backups" / "auto").glob(f"pre-migrate-{OLDER}-{NEWEST}-*Z.db")
    assert version_of(saved) == OLDER
    assert "backed up the database to" in result.output


def test_restored_older_database_is_not_remigrated_in_a_container_deployment(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, OLDER)  # as if a pre-migrate backup had just been restored
    (env / "deployment.toml").write_text('mode = "container"\n')
    result = runner.invoke(app, ["apply", "stats"])
    assert result.exit_code == 1
    assert "run `jobhunter upgrade`" in result.output
    assert "Traceback" not in result.output
    assert version_of(path) == OLDER
    assert not (env / "backups").exists()


def test_newer_database_refused_with_a_clear_message(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, NEWEST)
    add_fake_version(path, NEWEST + 1)
    result = runner.invoke(app, ["backfill-salary"])
    assert result.exit_code == 1
    assert (
        f"error: database is newer than this jobhunter (code has {NEWEST}, "
        f"database has {NEWEST + 1})" in result.output
    )


def test_db_migrate_is_exempt_from_the_marker(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, OLDER)
    (env / "deployment.toml").write_text('mode = "container"\n')
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0, result.output
    assert f"from {OLDER} to {NEWEST}" in result.output
    assert version_of(path) == NEWEST
    assert len(list((env / "backups" / "auto").glob("pre-migrate-*.db"))) == 1
    again = runner.invoke(app, ["db", "migrate"])
    assert f"schema up to date at {NEWEST}" in again.output


def test_db_migrate_still_refuses_a_newer_database(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, NEWEST)
    add_fake_version(path, NEWEST + 1)
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 1
    assert "database is newer than this jobhunter" in result.output


def test_version_reports_without_migrating_or_refusing(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, OLDER)
    (env / "deployment.toml").write_text('mode = "container"\n')
    monkeypatch.setenv("JOBHUNTER_BUILD_COMMIT", "abc123")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert "commit:     abc123 (build)" in result.output
    assert f"schema:     {NEWEST}" in result.output
    assert f"database:   {OLDER} (behind" in result.output
    assert "deployment: container" in result.output
    assert version_of(path) == OLDER
    add_fake_version(path, NEWEST + 1)
    out = json.loads(runner.invoke(app, ["version", "--json"]).output)
    assert out["db_state"] == "newer" and out["unknown_migrations"] == [NEWEST + 1]
    assert out["code_schema"] == NEWEST and out["commit"] == "abc123"


def test_version_without_a_database_or_commit(env, monkeypatch):
    monkeypatch.delenv("JOBHUNTER_BUILD_COMMIT", raising=False)
    from jobhunter.ops import version as ver

    monkeypatch.setattr(ver, "build_commit", lambda env=None: (None, "unknown"))
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert "commit:     unknown" in result.output
    assert "database:   missing" in result.output
    assert "deployment: host" in result.output
    assert not (env / "jobhunter.db").exists()


def test_paths_is_exempt(env, monkeypatch):
    path = env / "jobhunter.db"
    make_db(path, monkeypatch, OLDER)
    (env / "deployment.toml").write_text('mode = "container"\n')
    assert runner.invoke(app, ["paths"]).exit_code == 0
    assert version_of(path) == OLDER


def test_exempt_list_matches_the_spec():
    assert {
        "version",
        "paths",
        "secrets status",
        "container preflight",
        "db backup",
        "db restore",
        "upgrade",
        "db migrate",
    } == cli.MIGRATION_EXEMPT_COMMANDS


# --- console ----------------------------------------------------------------------------


def _settings(tmp_path):
    return Settings(
        paths=Paths(
            data_dir=tmp_path, db_path=tmp_path / "jobhunter.db", profile_dir=tmp_path / "p"
        )
    )


def test_console_stops_serving_once_a_newer_jobhunter_migrates(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    path = settings.paths.db_path
    client = TestClient(create_app(settings, lambda: db.connect(path)))
    assert client.get("/healthz").status_code == 200
    add_fake_version(path, NEWEST + 1)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert "database is newer than this jobhunter" in r.text
    assert client.get("/").status_code == 503


def test_console_does_not_start_on_a_newer_database(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    make_db(settings.paths.db_path, monkeypatch, NEWEST)
    add_fake_version(settings.paths.db_path, NEWEST + 1)
    with pytest.raises(db.SchemaTooNew):
        create_app(settings)


def test_console_command_prints_the_refusal(env, monkeypatch):
    make_db(env / "jobhunter.db", monkeypatch, OLDER)
    (env / "deployment.toml").write_text('mode = "container"\n')
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("console started"))
    result = runner.invoke(app, ["console"])
    assert result.exit_code == 1
    assert "run `jobhunter upgrade`" in result.output


def test_console_backs_up_before_migrating_on_a_host_install(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    make_db(settings.paths.db_path, monkeypatch, OLDER)
    create_app(settings)
    assert version_of(settings.paths.db_path) == NEWEST
    assert len(list((tmp_path / "backups" / "auto").glob("pre-migrate-*.db"))) == 1
