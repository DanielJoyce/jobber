"""Container mode (specs/018 C1): JOBHUNTER_CONTAINER=1 changes defaults and refusals only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter import container
from jobhunter.cli import app
from jobhunter.config import ENV_FILE_VAR, env_files, load_settings
from jobhunter.console.app import check_host, cross_site_reason
from jobhunter.mail import auth
from jobhunter.xdg import cache_home, config_home, data_home

runner = CliRunner()


@pytest.fixture
def ctr(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")


def test_not_container_unless_explicit(monkeypatch):
    # /run/.containerenv exists in toolbox; only the variable counts
    assert not container.is_container()
    for value in ("", "0", "true", "yes"):
        monkeypatch.setenv("JOBHUNTER_CONTAINER", value)
        assert not container.is_container()
        assert str(data_home()) != "/data"


def test_defaults_are_container_paths(ctr, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/somewhere")  # XDG is not consulted in a container
    s = load_settings()
    assert (config_home(), data_home(), cache_home()) == (
        Path("/config"),
        Path("/data"),
        Path("/cache"),
    )
    assert str(s.paths.data_dir) == "/data"
    assert str(s.paths.db_path) == "/data/jobhunter.db"
    assert str(s.paths.profile_dir) == "/data/profile"
    assert str(s.paths.resume_path) == "/data/resume"
    assert str(s.paths.cache_dir) == "/cache"
    assert set(s.paths.sources.values()) == {"container default"}
    assert str(s.mail.client_secrets_path) == "/run/secrets/google_client_secret.json"


def test_explicit_vars_and_config_file_still_win(ctr, monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ncache_dir = "{tmp_path}/c"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setenv("JOBHUNTER_DATA_DIR", str(tmp_path / "d"))
    s = load_settings()
    assert s.paths.data_dir == tmp_path / "d"
    assert s.paths.db_path == tmp_path / "d/jobhunter.db"
    assert s.paths.cache_dir == tmp_path / "c"
    assert s.paths.sources["cache_dir"] == "config file"
    assert s.paths.sources["data_dir"] == "env JOBHUNTER_DATA_DIR"


def test_paths_reports_mode_and_source(ctr):
    out = runner.invoke(app, ["paths"]).output
    assert out.splitlines()[0] == "mode: container"
    assert "[container default]" in out and "/data/jobhunter.db" in out
    data = json.loads(runner.invoke(app, ["paths", "--json"]).output)
    assert data["mode"] == "container"
    assert data["db_path"]["source"] == "container default"


def test_paths_on_host_has_no_container_line():
    assert "mode: container" not in runner.invoke(app, ["paths"]).output


def test_legacy_data_guard_is_skipped(ctr, tmp_path, monkeypatch):
    from jobhunter import legacy_data

    old = tmp_path / "data"
    old.mkdir()
    (old / "jobhunter.db").write_bytes(b"x")
    monkeypatch.setattr(legacy_data, "candidate_roots", lambda: [tmp_path])
    assert legacy_data.check(load_settings()).kind == legacy_data.OK
    monkeypatch.delenv("JOBHUNTER_CONTAINER")
    assert legacy_data.check(load_settings()).blocked


def test_env_files_only_from_explicit_file(ctr, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("A=1\n")
    monkeypatch.chdir(tmp_path)
    assert env_files() == []
    explicit = tmp_path / "e.env"
    monkeypatch.setenv(ENV_FILE_VAR, str(explicit))
    assert env_files() == [explicit]
    monkeypatch.delenv("JOBHUNTER_CONTAINER")
    assert len(env_files()) == 3


@pytest.mark.parametrize(
    "args",
    [
        ["migrate-paths"],
        ["migrate-paths", "--apply"],
        ["schedule", "install"],
        ["schedule", "uninstall"],
        ["schedule", "status"],
        ["mail", "auth"],
        ["mail", "auth", "--manual"],
        ["apply", "draft", "1"],
    ],
)
def test_host_only_commands_refuse_with_host_command(ctr, args):
    r = runner.invoke(app, args)
    assert r.exit_code == 2
    assert "container mode" in r.output and "Run it on the host: jobhunter " in r.output


def test_mail_auth_pointer_names_podman_secret(ctr):
    assert "--podman-secret jobhunter_gmail_token" in runner.invoke(app, ["mail", "auth"]).output


def test_gmail_token_only_from_token_file_var(ctr, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(auth.keyring, "get_password", lambda *a: called.append(a) or "kr")
    assert auth.load_token() is None  # no variable: nothing read, keyring not tried
    assert called == []
    tok = tmp_path / "tok"
    tok.write_text(json.dumps({"refresh_token": "r-1"}))
    monkeypatch.setenv("JOBHUNTER_GMAIL_TOKEN_FILE", str(tok))
    assert auth.load_token() == "r-1"
    assert called == []
    with pytest.raises(auth.MailAuthError, match="host"):
        auth.store_token("x")


def test_console_bind_rules(ctr, monkeypatch):
    check_host("127.0.0.1")
    with pytest.raises(ValueError):
        check_host("0.0.0.0")
    assert not container.published_loopback_only()
    monkeypatch.setenv("JOBHUNTER_PUBLISHED_LOOPBACK_ONLY", "1")
    assert container.published_loopback_only()


def test_published_loopback_only_ignored_on_host(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_PUBLISHED_LOOPBACK_ONLY", "1")
    assert not container.published_loopback_only()


def test_console_command_binds_all_interfaces_only_with_the_interlock(ctr, monkeypatch, tmp_path):
    import uvicorn

    from jobhunter.console import app as console_app

    monkeypatch.setenv("JOBHUNTER_DATA_DIR", str(tmp_path))
    ran: dict = {}
    seen: dict = {}
    real = console_app.create_app
    monkeypatch.setattr(uvicorn, "run", lambda app_, host, port: ran.update(host=host))
    monkeypatch.setattr(console_app, "create_app", lambda *a, **k: seen.update(k) or real(*a, **k))
    r = runner.invoke(app, ["console", "--host", "0.0.0.0"])
    assert r.exit_code == 2 and not ran
    monkeypatch.setenv("JOBHUNTER_PUBLISHED_LOOPBACK_ONLY", "1")
    r = runner.invoke(app, ["console", "--host", "0.0.0.0"])
    assert r.exit_code == 0 and ran["host"] == "0.0.0.0"
    assert seen["allow_remote"] is False  # the Host check stays on


def test_host_check_still_refuses_foreign_host_in_container_mode(ctr):
    assert "loopback" in (cross_site_reason("GET", {"host": "evil.example:8808"}) or "")
    assert cross_site_reason("GET", {"host": "127.0.0.1:8808"}) is None
    assert cross_site_reason("GET", {"host": "localhost:8808"}) is None  # HealthCmd style
