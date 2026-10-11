"""specs/018 C7: `mail auth --podman-secret` against a fake podman on PATH (no real podman)."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.mail import auth

TOKEN = "1//SECRET-refresh-token-value"
runner = CliRunner()

FAKE = """#!{python}
import json, os, sys
rec = os.environ["FAKE_PODMAN_LOG"]
argv = sys.argv[1:]
stdin = "" if argv[:2] == ["secret", "ls"] else sys.stdin.read()
with open(rec, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "stdin": stdin, "env": dict(os.environ)}}) + "\\n")
if argv[:2] == ["secret", "ls"]:
    if os.environ.get("FAKE_PODMAN_LS_FAIL"):
        sys.exit(125)
    print(os.environ.get("FAKE_PODMAN_SECRETS", ""))
    sys.exit(0)
if os.environ.get("FAKE_PODMAN_CREATE_FAIL"):
    print("Error: boom", file=sys.stderr)
    sys.exit(125)
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "podman"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_PODMAN_LOG", str(log))
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(auth, "require_client_secrets", lambda s: tmp_path / "c.json")
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    consent = []

    def flow(settings, **kw):
        consent.append(kw)
        return TOKEN

    monkeypatch.setattr(auth, "run_oauth_flow", flow)

    def calls():
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines()]

    return type("F", (), {"calls": staticmethod(calls), "consent": consent, "tmp": tmp_path})


def _local_file(fake):
    return fake.tmp / "xdg" / "jobhunter" / "gmail_token.json"


def test_token_only_on_stdin_new_secret(fake):
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "jobhunter_gmail_token"])
    assert r.exit_code == 0, r.output
    creates = [c for c in fake.calls() if c["argv"][:2] == ["secret", "create"]]
    assert len(creates) == 1
    assert creates[0]["argv"] == ["secret", "create", "--replace", "jobhunter_gmail_token", "-"]
    assert creates[0]["stdin"] == TOKEN
    for c in fake.calls():
        assert TOKEN not in json.dumps(c["argv"])
        assert TOKEN not in json.dumps(c["env"])
    assert TOKEN not in r.output
    assert "created podman secret" in r.output and "preflight" in r.output
    assert "daemon-reload" in r.output and "restart" in r.output
    assert not _local_file(fake).exists()


def test_replaced_secret_says_nothing_else_to_do(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PODMAN_SECRETS", "other\njobhunter_gmail_token")
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "jobhunter_gmail_token"])
    assert r.exit_code == 0, r.output
    assert "replaced podman secret" in r.output and "Nothing else" in r.output
    assert "preflight" not in r.output and TOKEN not in r.output


def test_podman_missing_refused_before_consent(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "s"])
    assert r.exit_code == 1
    assert "podman is not reachable" in r.output
    assert fake.consent == []
    assert not _local_file(fake).exists()


def test_podman_ls_failing_refused_before_consent(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PODMAN_LS_FAIL", "1")
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "s"])
    assert r.exit_code == 1 and fake.consent == []


def test_host_escape_chain_used(fake, monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    (bindir / "podman").rename(bindir / "real-podman")
    spawn = bindir / "flatpak-spawn"
    spawn.write_text(
        f'#!/bin/sh\n[ "$1" = --host ] || exit 2\nshift\n'
        f'[ "$1" = podman ] || exit 3\nshift\nexec {bindir}/real-podman "$@"\n'
    )
    spawn.chmod(0o755)
    # `podman` is no longer on PATH, so the chain must fall through to flatpak-spawn.
    # The fake needs sh and python by absolute path; PATH only holds bindir.
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "s"])
    assert r.exit_code == 0, r.output
    creates = [c for c in fake.calls() if c["argv"][:2] == ["secret", "create"]]
    assert creates and creates[0]["stdin"] == TOKEN


def test_write_failure_keeps_token_locally(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PODMAN_CREATE_FAIL", "1")
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "s"])
    assert r.exit_code == 1
    assert "could not write podman secret" in r.output and "consent is not lost" in r.output
    f = _local_file(fake)
    assert json.loads(f.read_text()) == {"refresh_token": TOKEN}
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert TOKEN not in r.output
    for c in fake.calls():
        assert TOKEN not in json.dumps(c["argv"]) + json.dumps(c["env"])


def test_keep_local_also_stores(fake):
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "s", "--keep-local"])
    assert r.exit_code == 0, r.output
    assert json.loads(_local_file(fake).read_text())["refresh_token"] == TOKEN


def test_keep_local_without_secret_rejected(fake):
    r = runner.invoke(app, ["mail", "auth", "--keep-local"])
    assert r.exit_code == 2 and fake.consent == []


@pytest.mark.parametrize("name", ["-x", "a b", "", "a/b", "--replace"])
def test_bad_secret_name_refused_before_anything(fake, name):
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", name])
    assert r.exit_code != 0 and fake.consent == [] and fake.calls() == []


def test_no_token_in_logs(fake, caplog):
    caplog.set_level("DEBUG")
    runner.invoke(app, ["mail", "auth", "--podman-secret", "s"])
    assert TOKEN not in caplog.text


def test_container_mode_still_refused(fake, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_CONTAINER", "1")
    r = runner.invoke(app, ["mail", "auth", "--podman-secret", "s"])
    assert r.exit_code == 2 and fake.consent == []
    assert fake.calls() == []
