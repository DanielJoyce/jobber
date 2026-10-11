"""CLI wiring of ``jobhunter upgrade`` and ``jobhunter ctr-script`` (spec 018 C15).

The real :class:`SubprocessRunner` is replaced with a fake, so no podman, systemctl or git
runs; HOME is the conftest's temporary directory.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.ops import ctr, proc
from jobhunter.ops.proc import Result

runner = CliRunner()


class Fake:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def which(self, name):
        return f"/usr/bin/{name}" if name == "podman" else None

    def run(self, argv, *, capture=True, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git" and "rev-parse" in argv:
            return Result(0, "abc123def456\n")
        if argv[0] == "git":
            return Result(0, "")
        if argv[1:3] == ["secret", "ls"]:
            return Result(0, "jobhunter_anthropic\njobhunter_gmail_token\nsomething_else\n")
        if argv[1] == "version":
            return Result(0, "5.8.4\n")
        raise AssertionError(f"unexpected command: {argv}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{data}"\ncache_dir = "{tmp_path / "cache"}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    fake = Fake()
    monkeypatch.setattr(proc, "SubprocessRunner", lambda: fake)
    return data, fake


def test_upgrade_on_a_host_only_install_says_it_is_not_needed(env):
    _data, fake = env
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "not needed" in result.output and "host-only install" in result.output
    assert fake.calls == []


def test_upgrade_refuses_in_container_mode(env, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    result = runner.invoke(app, ["upgrade", "--dry-run"])
    assert result.exit_code == 2
    assert "Run it on the host" in result.output


def test_upgrade_dry_run_in_a_container_deployment(env):
    data, fake = env
    data.mkdir()
    conn = db.connect(data / "jobhunter.db")
    db.migrate(conn)
    conn.close()
    (data / db.DEPLOYMENT_MARKER).write_text('mode = "container"\n')
    result = runner.invoke(app, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run: nothing is run" in result.output
    assert "BUILD_COMMIT=abc123def456" in result.output
    assert all(c[0] == "git" for c in fake.calls)


def test_upgrade_refusal_is_one_error_line(env):
    data, fake = env
    data.mkdir()
    (data / db.DEPLOYMENT_MARKER).write_text('mode = "container"\n')
    real_run = fake.run

    def dirty(argv, **kw):
        if list(argv)[3:4] == ["status"]:
            return Result(0, " M src/jobhunter/cli.py\n")
        return real_run(argv, **kw)

    fake.run = dirty
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 1
    assert "uncommitted changes" in result.output and "nothing was changed" in result.output


def test_ctr_script_prints_a_wrapper_with_existing_secrets_only(env):
    data, _fake = env
    result = runner.invoke(app, ["ctr-script"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("#!/bin/sh\n")
    assert "jobhunter_anthropic,type=mount" in result.output
    assert "jobhunter_gmail_token,type=mount" in result.output
    assert "jobhunter_openrouter" not in result.output
    assert f"{data}:/data:z" in result.output


def test_ctr_script_write_installs_it(env, tmp_path):
    dest = tmp_path / "bin" / "jobhunter-ctr"
    result = runner.invoke(app, ["ctr-script", "--write", "--path", str(dest)])
    assert result.exit_code == 0, result.output
    assert ctr.is_ours(dest)
    assert "secrets: jobhunter_anthropic, jobhunter_gmail_token" in result.output


def test_ctr_script_refuses_in_container_mode(env, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    assert runner.invoke(app, ["ctr-script"]).exit_code == 2
