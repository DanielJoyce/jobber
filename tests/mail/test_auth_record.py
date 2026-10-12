"""specs/019 S5: gmail-auth.json (issued_at + storage kind, never the token) and the
RefreshError mapping. No network, no real keyring, no real Gmail."""

from __future__ import annotations

import importlib.util
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

from jobhunter import readiness
from jobhunter.mail import auth, sync
from jobhunter.sources.adapters.mailalerts import MailAlertsAdapter

_spec = importlib.util.spec_from_file_location(
    "readiness_helpers", Path(__file__).resolve().parent.parent / "test_readiness.py"
)
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)
NOW, add_source, evaluate = helpers.NOW, helpers.add_source, helpers.evaluate
good_settings, make_db = helpers.good_settings, helpers.make_db

TOKEN = "1//SENTINEL-refresh-token-9d2c"
OLD = "2026-10-08T09:30:00+00:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUNTER_CONTAINER", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(auth, "run_oauth_flow", lambda s, **kw: TOKEN)
    monkeypatch.setattr(auth, "require_client_secrets", lambda s: tmp_path / "c.json")
    readiness.clear_caches()
    return good_settings(tmp_path)


def record(settings):
    path = auth.auth_record_path(settings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return json.loads(path.read_text(encoding="utf-8"))


def fake_podman(monkeypatch, secrets=()):
    written = []
    monkeypatch.setattr(auth, "find_podman", lambda: (["podman"], list(secrets)))
    monkeypatch.setattr(auth, "write_podman_secret", lambda prefix, name, tok: written.append(name))
    return written


def test_keyring_variant_records_keyring(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: True)
    monkeypatch.setattr(auth.keyring, "set_password", lambda *a: None)
    assert auth.authenticate(env) == "keyring"
    rec = record(env)
    assert rec["storage"] == "keyring"
    assert abs(datetime.fromisoformat(rec["issued_at"]) - datetime.now(UTC)) < timedelta(minutes=1)
    assert set(rec) == {"issued_at", "storage"}


def test_file_variant_records_file_without_the_token(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    assert auth.authenticate(env) == "file"
    assert record(env)["storage"] == "file"
    assert TOKEN not in auth.auth_record_path(env).read_text(encoding="utf-8")
    assert list(auth.auth_record_path(env).parent.glob(".gmail-auth-*")) == []


def test_podman_secret_variant_records_the_secret(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    written = fake_podman(monkeypatch)
    assert auth.authenticate_to_podman_secret(env, "jobhunter_gmail_token") == "new"
    assert written == ["jobhunter_gmail_token"]
    rec = record(env)
    assert rec["storage"] == "podman secret jobhunter_gmail_token"
    assert rec["issued_at"]
    assert TOKEN not in auth.auth_record_path(env).read_text(encoding="utf-8")


def test_from_local_keeps_the_original_date(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    auth.write_auth_record(env, "file", OLD)
    auth._write_token_file(TOKEN)
    fake_podman(monkeypatch)
    assert auth.authenticate_to_podman_secret(env, "s1", from_local=True) == "new"
    assert record(env) == {"issued_at": OLD, "storage": "podman secret s1"}


def test_from_local_without_a_record_is_unknown_never_now(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    auth._write_token_file(TOKEN)
    fake_podman(monkeypatch)
    auth.authenticate_to_podman_secret(env, "s1", from_local=True)
    assert record(env) == {"issued_at": None, "storage": "podman secret s1"}


def test_podman_failure_after_consent_records_the_local_storage(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    monkeypatch.setattr(auth, "find_podman", lambda: (["podman"], []))

    def boom(*a):
        raise auth.MailAuthError("podman secret create exited 125")

    monkeypatch.setattr(auth, "write_podman_secret", boom)
    assert auth.authenticate_to_podman_secret(env, "s1", echo=lambda m: None) == "local"
    assert record(env)["storage"] == "file"


def test_rewrite_replaces_the_record(env):
    auth.write_auth_record(env, "file", OLD)
    auth.write_auth_record(env, "keyring", "2026-10-10T00:00:00+00:00")
    assert record(env) == {"issued_at": "2026-10-10T00:00:00+00:00", "storage": "keyring"}


# --- RefreshError mapping -----------------------------------------------------------------


def _search(monkeypatch, env, conn):
    def boom(*a, **kw):
        raise RefreshError("invalid_grant: Token has been expired or revoked. " + TOKEN)

    monkeypatch.setattr(sync, "plan_batch", boom)
    adapter = MailAlertsAdapter(service=object(), rows=[], clock=lambda: NOW)
    ctx = SimpleNamespace(conn=conn, settings=env)
    return list(adapter.search(SimpleNamespace(key="us-mailalerts"), None, NOW, ctx))


def test_refresh_error_becomes_fixed_wording(env, monkeypatch):
    with pytest.raises(auth.GmailTokenExpired) as err:
        _search(monkeypatch, env, make_db(env))
    assert str(err.value) == "Gmail token expired or revoked"
    assert TOKEN not in str(err.value)
    assert isinstance(err.value, auth.MailAuthError)


def test_readiness_row_end_to_end_from_record_and_runner_note(env, monkeypatch, tmp_path):
    """Record as `mail auth` writes it, then the failure note as the runner stores it."""
    from jobhunter.pipeline.runner import classify

    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    auth.write_auth_record(env, "file", (NOW - timedelta(days=2)).isoformat())
    token = tmp_path / "xdg" / "jobhunter" / "gmail_token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({"refresh_token": TOKEN}))
    conn = make_db(env)
    row = evaluate(env)["mail.token"]
    assert (row.status, row.source) == ("ok", "file")
    assert "re-auth due about" in row.detail

    with pytest.raises(auth.GmailTokenExpired) as err:
        _search(monkeypatch, env, conn)
    assert classify(err.value) == "broken"
    add_source(conn, "us-mailalerts", "broken", f"{type(err.value).__name__}: {err.value}")
    conn.close()
    readiness.clear_caches()
    row = evaluate(env)["mail.token"]
    assert row.status == "broken"
    assert row.detail == "Gmail token expired or revoked"
    assert TOKEN not in json.dumps(row.as_dict())


def test_readiness_reads_the_podman_record_in_container_mode(env, monkeypatch):
    monkeypatch.setattr(auth, "_keyring_usable", lambda: False)
    auth.write_auth_record(env, "file", (NOW - timedelta(days=8)).isoformat())
    auth._write_token_file(TOKEN)
    fake_podman(monkeypatch)
    auth.authenticate_to_podman_secret(env, "s1", from_local=True)
    row = evaluate(env, mode=readiness.HOST_MANAGING)["mail.token"]
    assert row.source == "podman secret s1"
