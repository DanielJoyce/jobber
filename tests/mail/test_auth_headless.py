"""Headless Gmail auth: opener chain, --manual paste flow, token file fallback. No network."""

from __future__ import annotations

import os
import stat
import subprocess
import webbrowser

import pytest
from keyring.errors import NoKeyringError
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Settings
from jobhunter.mail import auth

URL = "https://accounts.example.test/auth?x=1"
runner = CliRunner()


class FakeRun:
    def __init__(self, ok=()):
        self.ok = set(ok)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if argv[0] in self.ok else 1)


@pytest.fixture
def no_python_browser(monkeypatch):
    def boom():
        raise webbrowser.Error("none")

    monkeypatch.setattr(auth.webbrowser, "get", boom)


def all_tools(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda n: f"/usr/bin/{n}")


def test_chain_order_and_stop_at_first_success(monkeypatch, no_python_browser):
    all_tools(monkeypatch)
    run = FakeRun(ok={"distrobox-host-exec"})
    monkeypatch.setattr(auth.subprocess, "run", run)
    assert auth.open_url(URL) == "distrobox-host-exec"
    assert [c[0] for c in run.calls] == ["xdg-open", "flatpak-spawn", "distrobox-host-exec"]
    assert run.calls[1] == ["flatpak-spawn", "--host", "xdg-open", URL]


def test_first_opener_success_stops(monkeypatch, no_python_browser):
    all_tools(monkeypatch)
    run = FakeRun(ok={"xdg-open"})
    monkeypatch.setattr(auth.subprocess, "run", run)
    assert auth.open_url(URL) == "xdg-open"
    assert len(run.calls) == 1


def test_python_browser_first_but_not_text_browser(monkeypatch):
    class Real:
        name = "firefox"

        def open(self, url, new=0):
            return True

    monkeypatch.setattr(auth.webbrowser, "get", lambda: Real())
    assert auth.open_url(URL) == "python webbrowser"

    class Text(Real):
        name = "/usr/bin/lynx"

    monkeypatch.setattr(auth.webbrowser, "get", lambda: Text())
    all_tools(monkeypatch)
    monkeypatch.setattr(auth.subprocess, "run", FakeRun(ok={"xdg-open"}))
    assert auth.open_url(URL) == "xdg-open"


def test_portal_is_last_resort(monkeypatch, no_python_browser):
    all_tools(monkeypatch)
    run = FakeRun(ok={"gdbus"})
    monkeypatch.setattr(auth.subprocess, "run", run)
    assert auth.open_url(URL) == "desktop portal"
    assert run.calls[-1][-2:] == [URL, "{}"]
    assert "org.freedesktop.portal.OpenURI.OpenURI" in run.calls[-1]


def test_none_available_never_raises(monkeypatch, no_python_browser):
    monkeypatch.setattr(auth.shutil, "which", lambda n: None)
    assert auth.open_url(URL) is None


def test_subprocess_errors_are_swallowed(monkeypatch, no_python_browser):
    all_tools(monkeypatch)

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(auth.subprocess, "run", boom)
    assert auth.open_url(URL) is None


# --- local-server flow ------------------------------------------------------------------


class Creds:
    refresh_token = "rt"


class ServerFlow:
    last = None

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        cls.last = cls()
        return cls.last

    def run_local_server(self, **kw):
        self.kw = kw
        return Creds()


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    f = tmp_path / "c.json"
    f.write_text("{}")
    monkeypatch.setenv(auth.SECRETS_ENV_VAR, str(f))
    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", ServerFlow)


def test_server_flow_prints_url_message_and_registers_chain(secrets):
    assert auth.run_oauth_flow(Settings(), port=9000) == "rt"
    kw = ServerFlow.last.kw
    assert kw["open_browser"] is True and kw["port"] == 9000 and kw["host"] == "127.0.0.1"
    assert "{url}" in kw["authorization_prompt_message"]
    assert auth.OPEN_HINT in kw["authorization_prompt_message"]
    ctrl = webbrowser.get(kw["browser"])
    assert isinstance(ctrl, auth._ChainBrowser)


def test_no_browser_skips_openers(secrets, monkeypatch):
    called = []
    monkeypatch.setattr(auth, "open_url", lambda u: called.append(u))
    auth.run_oauth_flow(Settings(), open_browser=False)
    kw = ServerFlow.last.kw
    assert kw["open_browser"] is False and "browser" not in kw
    assert called == []
    assert auth.OPEN_HINT in kw["authorization_prompt_message"]


def test_chain_browser_uses_chain(monkeypatch):
    seen = []
    monkeypatch.setattr(auth, "open_url", lambda u: seen.append(u) or None)
    assert auth._ChainBrowser().open(URL) is False  # nothing worked; library ignores it
    assert seen == [URL]


# --- manual flow ------------------------------------------------------------------------


class ManualFlow:
    last = None

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        cls.last = cls()
        cls.last.fetched = None
        return cls.last

    def authorization_url(self, **kw):
        return "https://accounts.example.test/auth?state=S1", "S1"

    def fetch_token(self, authorization_response):
        self.fetched = authorization_response
        self.env = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
        self.credentials = Creds()


@pytest.fixture
def manual_secrets(tmp_path, monkeypatch):
    f = tmp_path / "c.json"
    f.write_text("{}")
    monkeypatch.setenv(auth.SECRETS_ENV_VAR, str(f))
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", ManualFlow)


def run_manual(pasted, port=auth.DEFAULT_PORT):
    out = []
    token = auth.run_manual_flow(Settings(), port=port, echo=out.append, ask=lambda p: pasted)
    return token, out


def test_manual_success(manual_secrets):
    pasted = "http://127.0.0.1:8765/?state=S1&code=abc&scope=x"
    token, out = run_manual(pasted)
    flow = ManualFlow.last
    assert token == "rt" and flow.fetched == pasted
    assert flow.redirect_uri == "http://127.0.0.1:8765/"
    assert flow.env == "1"
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ  # scoped to the exchange
    assert "https://accounts.example.test/auth?state=S1" in out[0] and auth.OPEN_HINT in out[0]


def test_manual_custom_port(manual_secrets):
    run_manual("http://127.0.0.1:9999/?state=S1&code=abc", port=9999)
    assert ManualFlow.last.redirect_uri == "http://127.0.0.1:9999/"


def test_manual_state_mismatch(manual_secrets):
    with pytest.raises(auth.MailAuthError, match="state mismatch"):
        run_manual("http://127.0.0.1:8765/?state=OTHER&code=abc")
    assert ManualFlow.last.fetched is None


def test_manual_missing_code(manual_secrets):
    with pytest.raises(auth.MailAuthError, match="no authorization code"):
        run_manual("http://127.0.0.1:8765/?state=S1")


def test_manual_garbage_and_error(manual_secrets):
    with pytest.raises(auth.MailAuthError, match="does not look like"):
        run_manual("abc123")
    with pytest.raises(auth.MailAuthError, match="access_denied"):
        run_manual("http://127.0.0.1:8765/?error=access_denied&state=S1")


def test_manual_exchange_failure_restores_env(manual_secrets, monkeypatch):
    def bad(self, authorization_response):
        raise ValueError("invalid_grant")

    monkeypatch.setattr(ManualFlow, "fetch_token", bad)
    with pytest.raises(auth.MailAuthError, match="rejected"):
        run_manual("http://127.0.0.1:8765/?state=S1&code=abc")
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


# --- token storage ----------------------------------------------------------------------


@pytest.fixture
def tokfile(tmp_path, monkeypatch):
    p = tmp_path / "home" / ".config" / "jobhunter" / "gmail_token.json"
    monkeypatch.setattr(auth, "token_file_path", lambda: p)
    return p


def failing_keyring(monkeypatch):
    def fail(*a):
        raise NoKeyringError("no backend")

    monkeypatch.setattr(auth.keyring, "set_password", fail)
    monkeypatch.setattr(auth.keyring, "get_password", fail)


def test_store_falls_back_to_file_0600(monkeypatch, tokfile):
    failing_keyring(monkeypatch)
    out = []
    assert auth.store_token("secret-rt", echo=out.append) == "file"
    assert stat.S_IMODE(tokfile.stat().st_mode) == 0o600
    assert stat.S_IMODE(tokfile.parent.stat().st_mode) == 0o700
    assert len(out) == 1 and "no OS keyring" in out[0] and str(tokfile) in out[0]
    assert "secret-rt" not in out[0]
    assert auth.load_token() == "secret-rt"


def test_null_backend_uses_file(monkeypatch, tokfile):
    from keyring.backends import null

    monkeypatch.setattr(auth.keyring, "get_keyring", lambda: null.Keyring())
    assert auth.store_token("t", echo=lambda m: None) == "file"


def test_load_prefers_keyring_then_file(monkeypatch, tokfile):
    tokfile.parent.mkdir(parents=True)
    tokfile.write_text('{"refresh_token": "from-file"}')
    monkeypatch.setattr(auth.keyring, "get_password", lambda s, k: "from-keyring")
    assert auth.load_token() == "from-keyring"
    monkeypatch.setattr(auth.keyring, "get_password", lambda s, k: None)
    assert auth.load_token() == "from-file"


def test_store_prefers_keyring(monkeypatch, tokfile):
    saved = {}
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, k, v: saved.update({k: v}))
    assert auth.store_token("t") == "keyring"
    assert saved == {"gmail": "t"} and not tokfile.exists()


def test_status_reports_location_without_token(monkeypatch, tokfile, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    failing_keyring(monkeypatch)
    tokfile.parent.mkdir(parents=True)
    tokfile.write_text('{"refresh_token": "TOPSECRET"}')
    assert auth.token_location() == f"file {tokfile}"
    from jobhunter.mail import setup

    monkeypatch.setattr(auth, "build_service", lambda s: object())
    monkeypatch.setattr(
        setup,
        "mailbox_status",
        lambda svc, s: {"label": "L", "label_present": True, "filters": {}, "messages": None},
    )
    result = runner.invoke(app, ["mail", "status"])
    assert result.exit_code == 0, result.output
    assert f"token: file {tokfile}" in result.output
    assert "TOPSECRET" not in result.output


def test_cli_auth_flags_passed(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    got = {}

    def fake(settings, **kw):
        got.update(kw)
        return "file " + "x"

    monkeypatch.setattr(auth, "authenticate", fake)
    result = runner.invoke(app, ["mail", "auth", "--manual", "--port", "9000", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert got == {"manual": True, "port": 9000, "open_browser": False}
