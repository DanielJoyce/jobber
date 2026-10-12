"""Free live checks (specs/019 "Live checks", S6): booleans only, no model call, no leak.

Every service is faked (tests/live_fakes.py); the conftest guards refuse any real network,
keyring or `claude` launch."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError
from typer.testing import CliRunner

from jobhunter import live_checks as lc
from jobhunter import readiness
from jobhunter.apply import cli_runner, runner_state
from jobhunter.cli import app
from jobhunter.config import Settings, load_settings
from jobhunter.mail import auth
from live_fakes import (
    AUTH_WITH_IDENTITY,
    LEAK,
    FakeGmail,
    Recorder,
    anthropic_factory,
    deps,
    gmail_ok,
    http_factory,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"sk-ant-{LEAK}")
    monkeypatch.setenv("OPENROUTER_API_KEY", f"sk-or-{LEAK}")
    readiness.clear_caches()
    yield
    readiness.clear_caches()


def settings_for(tmp_path: Path, **sections: dict) -> Settings:
    data = tmp_path / "data"
    client_json = tmp_path / "google_client.json"
    client_json.write_text('{"installed": {"client_id": "x"}}', encoding="utf-8")
    overrides: dict = {
        "paths": {
            "data_dir": str(data),
            "db_path": str(data / "jobhunter.db"),
            "profile_dir": str(data / "profile"),
            "resume_path": str(data / "resume"),
            "cache_dir": str(tmp_path / "cache"),
        },
        "mail": {
            "alerts_address": f"{LEAK}+jobs@example.org",
            "client_secrets_path": str(client_json),
            "fallback_sender_domains": ["alerts.example.org"],
            "fallback_senders": ["jobs-noreply@board.example.org"],
        },
    }
    for name, values in sections.items():
        overrides.setdefault(name, {}).update(values)
    return load_settings(tmp_path / "no-config.toml", overrides=overrides)


def text(result: lc.LiveResult) -> str:
    return json.dumps(result.as_dict()) + repr(result)


def run(row: str, settings: Settings, d: lc.Deps, mode: str = readiness.HOST) -> lc.LiveResult:
    result = lc.run_row(row, settings, mode, now=NOW, deps=d)
    assert result is not None
    assert LEAK not in text(result)
    return result


# --- Anthropic ---------------------------------------------------------------------------------


def test_anthropic_lists_models_only(tmp_path):
    rec, s = Recorder(), settings_for(tmp_path)
    result = run("scorer.screen", s, deps(rec, s))
    assert result.kind == lc.ANTHROPIC and result.outcome == lc.OK
    assert [lc.fact_text(f) for f in result.facts] == ["accepted: yes"]
    assert rec.seen == ["api.anthropic.com /v1/models"]
    assert rec.model_calls() == []


def test_the_default_anthropic_client_does_not_retry(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    client = lc._anthropic_client()
    assert client.max_retries == 0  # one click, one request
    assert client.timeout == 15.0


def test_anthropic_refused_is_fixed_text(tmp_path):
    rec, s = Recorder(), settings_for(tmp_path)
    d = deps(rec, s, anthropic_client=anthropic_factory(rec, status=401))
    result = run("scorer.screen", s, d)
    assert result.outcome == lc.FAILED
    assert result.notes == ("the credential was refused",)


def test_anthropic_without_a_credential_sends_nothing(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    rec, s = Recorder(), settings_for(tmp_path)

    def boom():
        raise AssertionError("no client may be built without a credential")

    result = run("scorer.screen", s, deps(rec, s, anthropic_client=boom))
    assert result.outcome == lc.NOT_RUN and rec.seen == []


def test_anthropic_unexpected_error_shows_type_only(tmp_path):
    rec, s = Recorder(), settings_for(tmp_path)

    def broken():
        raise RuntimeError(f"profile {LEAK} unreadable")

    result = run("scorer.screen", s, deps(rec, s, anthropic_client=broken))
    assert result.outcome == lc.ERROR and result.notes == ("error (RuntimeError)",)


# --- OpenRouter and Jev ---------------------------------------------------------------------------


def test_openrouter_key_accepted_with_credit_and_no_label(tmp_path):
    rec = Recorder()
    s = settings_for(tmp_path, scoring={"screen_scorer": "openrouter:openai/gpt-oss-120b"})
    result = run("scorer.screen", s, deps(rec, s))
    assert result.kind == lc.OPENROUTER and result.outcome == lc.OK
    assert "credit remaining on this key: $12.50" in result.notes
    assert rec.seen == ["openrouter.ai /api/v1/key"]
    assert rec.model_calls() == []


def test_openrouter_unlimited_key_and_refused_key(tmp_path):
    s = settings_for(tmp_path, scoring={"screen_scorer": "jev:typesafe/jev-1.13"})
    rec = Recorder()
    result = run("scorer.screen", s, deps(rec, s, http_client=http_factory(rec, remaining=None)))
    assert result.kind == lc.OPENROUTER  # Jev uses the OpenRouter key
    assert "credit remaining: no limit set on this key" in result.notes
    rec = Recorder()
    refused = run("scorer.screen", s, deps(rec, s, http_client=http_factory(rec, key_status=401)))
    assert refused.outcome == lc.FAILED and refused.notes == ("the key was refused",)
    assert rec.model_calls() == []


@pytest.mark.parametrize(
    "base_url", ["https://openrouter.example.test/api/v1", "https://evil.test/openrouter.ai/v1"]
)
def test_openrouter_only_on_openrouter_ai(tmp_path, base_url):
    rec = Recorder()
    s = settings_for(
        tmp_path,
        scoring={"screen_scorer": "openrouter:x/y", "openrouter": {"base_url": base_url}},
    )
    result = run("scorer.screen", s, deps(rec, s))
    assert result.outcome == lc.NOT_RUN
    assert result.notes == (lc.NO_FREE_TEXT,)
    assert rec.seen == []


def test_openrouter_key_is_never_sent_over_plain_http(tmp_path):
    rec = Recorder()
    s = settings_for(
        tmp_path,
        scoring={
            "screen_scorer": "openrouter:x/y",
            "openrouter": {"base_url": "http://openrouter.ai/api/v1"},
        },
    )
    result = run("scorer.screen", s, deps(rec, s))
    assert result.outcome == lc.NOT_RUN and rec.seen == []


def test_openai_compat_has_no_free_check(tmp_path):
    rec = Recorder()
    s = settings_for(tmp_path, scoring={"screen_scorer": "openai-compat:llama"})
    result = run("scorer.screen", s, deps(rec, s))
    assert result.outcome == lc.NOT_RUN and result.notes == (lc.NO_FREE_TEXT,)
    assert rec.seen == []


# --- local model ------------------------------------------------------------------------------


def test_local_reachable_and_loaded_without_a_completion(tmp_path):
    rec = Recorder()
    s = settings_for(tmp_path, scoring={"screen_scorer": "local:qwen"})
    result = run("scorer.screen", s, deps(rec, s))
    assert [lc.fact_text(f) for f in result.facts] == ["reachable: yes", "model loaded: yes"]
    assert rec.seen == ["127.0.0.1 /v1/models"]
    assert rec.model_calls() == []


def test_local_health_fallback_and_non_loopback(tmp_path):
    rec = Recorder()
    s = settings_for(tmp_path, scoring={"screen_scorer": "local:qwen"})
    result = run("scorer.screen", s, deps(rec, s, http_client=http_factory(rec, models=[])))
    assert result.outcome == lc.FAILED  # /v1/models lists nothing, /health is not asked
    rec = Recorder()
    remote = settings_for(
        tmp_path,
        scoring={"screen_scorer": "local:qwen", "local": {"base_url": "http://10.0.0.5:8080"}},
    )
    assert run("scorer.screen", remote, deps(rec, remote)).outcome == lc.NOT_RUN
    assert rec.seen == []


# --- Gmail --------------------------------------------------------------------------------------


@pytest.mark.parametrize("row", ["mail.token", "mail.label"])
def test_gmail_booleans_and_fallback_count(tmp_path, row):
    rec = Recorder()
    s = settings_for(tmp_path)
    result = run(row, s, deps(rec, s))
    assert [lc.fact_text(f) for f in result.facts] == [
        "token accepted: yes",
        "label present: yes",
        "filter for your alerts address present: yes",
    ]
    assert "1 of 2 fallback sender filters present" in result.notes
    assert rec.seen == ["gmail.googleapis.com labels.list", "gmail.googleapis.com filters.list"]


def test_gmail_missing_filter_and_expired_token(tmp_path):
    rec = Recorder()
    s = settings_for(tmp_path)
    missing = run("mail.token", s, deps(rec, s, gmail=gmail_ok(rec, s, to_filter=False)))
    assert missing.outcome == lc.FAILED
    expired = run("mail.token", s, deps(rec, s, gmail=FakeGmail(rec, fail=RefreshError(LEAK))))
    assert expired.outcome == lc.FAILED
    assert expired.facts[0] == lc.Fact("token accepted", False)
    assert expired.notes == (auth.TOKEN_EXPIRED_MESSAGE,)


def test_gmail_missing_label_is_reported_never_created(tmp_path):
    rec = Recorder()
    s = settings_for(tmp_path)
    service = FakeGmail(rec, labels=[{"id": "L2", "name": "Other"}], filters=[])
    result = run("mail.label", s, deps(rec, s, gmail=service))
    assert result.facts[1] == lc.Fact("label present", False)
    assert result.outcome == lc.FAILED
    # reads only: a create-if-missing (like `mail setup`) would show up here
    assert rec.seen == ["gmail.googleapis.com labels.list", "gmail.googleapis.com filters.list"]


def test_gmail_without_a_token_sends_nothing(tmp_path):
    s = settings_for(tmp_path)

    def no_token(_settings):
        raise auth.MailAuthError("No Gmail token found. Run: jobhunter mail auth")

    result = lc.run_row(
        "mail.token", s, readiness.HOST, now=NOW, deps=lc.Deps(gmail_service=no_token)
    )
    assert result.outcome == lc.NOT_RUN and "no Gmail token found" in result.notes[0]


def test_gmail_in_the_console_container_points_at_jobhunter_ctr(tmp_path):
    s = settings_for(tmp_path)
    d = lc.Deps(gmail_service=lambda _s: pytest.fail("must not build a Gmail client"), environ={})
    result = lc.run_row("mail.token", s, readiness.CONTAINER, now=NOW, deps=d)
    assert result.outcome == lc.NOT_RUN and result.instead == "jobhunter-ctr mail status"


# --- drafting runner ---------------------------------------------------------------------------


def test_runner_reads_auth_status_uncached_and_drops_identity(tmp_path, fake_claude):
    fake_claude.set(auth=AUTH_WITH_IDENTITY)
    s = settings_for(tmp_path)
    for _ in range(2):  # uncached: every click asks again
        result = run("apply.runner", s, lc.Deps())
        assert [lc.fact_text(f) for f in result.facts] == [
            "logged in: yes",
            "claude.ai login (not an API key): yes",
        ]
    assert len(fake_claude.calls(auth=True)) == 2
    assert fake_claude.calls(auth=False) == []  # no drafting call, no model
    assert set() == cli_runner._AUTH_OK


def test_runner_api_key_login_is_a_status_not_a_turn_off(tmp_path, fake_claude):
    fake_claude.set(auth={**AUTH_WITH_IDENTITY, "authMethod": "api_key", "apiProvider": "x"})
    s = settings_for(tmp_path)
    result = run("apply.runner", s, lc.Deps())
    assert result.outcome == lc.FAILED
    assert result.facts[1] == lc.Fact("claude.ai login (not an API key)", False)
    assert not runner_state.load(Path(s.paths.data_dir)).off
    assert set() == cli_runner._AUTH_OK


def test_runner_without_claude_on_path(tmp_path):
    result = run("apply.runner", settings_for(tmp_path), lc.Deps())
    assert result.outcome == lc.NOT_RUN


# --- modes and plan -------------------------------------------------------------------------------


def test_host_managing_defers_secret_backed_checks(tmp_path):
    s = settings_for(tmp_path)
    planned = lc.plan(s, readiness.HOST_MANAGING)
    deferred = {p.kind: p for p in planned if p.instead}
    assert deferred[lc.ANTHROPIC].instead == lc.CTR_LIVE
    assert deferred[lc.GMAIL].instead == lc.CTR_LIVE
    runnable = [p.kind for p in planned if not p.instead]
    assert runnable == [lc.RUNNER]  # drafting runs on the host


def test_plan_shares_one_check_between_rows(tmp_path):
    s = settings_for(tmp_path)
    planned = {p.kind: p.rows for p in lc.plan(s, readiness.HOST)}
    assert planned[lc.ANTHROPIC] == ("scorer.screen", "scorer.deep")
    assert planned[lc.GMAIL] == ("mail.token", "mail.label")


# --- doctor --live -----------------------------------------------------------------------------


@pytest.fixture
def doctor_env(tmp_path, monkeypatch, fake_claude):
    """A configured host whose every live service is faked; returns the recorder."""
    from test_readiness import make_db, write_prefs, write_resume

    data = tmp_path / "data"
    client_json = tmp_path / "google_client.json"
    client_json.write_text("{}", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{data / "jobhunter.db"}"\n'
        f'profile_dir = "{data / "profile"}"\nresume_path = "{data / "resume"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n'
        f'[fetch]\nuser_agent_contact = "{LEAK}-contact@example.org"\n'
        f'[mail]\nalerts_address = "{LEAK}+jobs@example.org"\n'
        f'client_secrets_path = "{client_json}"\n'
        'fallback_sender_domains = ["alerts.example.org"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    s = load_settings(cfg)
    write_prefs(s)
    write_resume(s)
    make_db(s).close()
    fake_claude.set(auth=AUTH_WITH_IDENTITY)
    rec = Recorder()
    monkeypatch.setattr(lc, "_anthropic_client", anthropic_factory(rec))
    monkeypatch.setattr(lc, "_http_client", http_factory(rec))
    monkeypatch.setattr(lc, "_gmail_service", lambda settings: gmail_ok(rec, settings))
    return rec


def test_doctor_live_prints_contacts_first_then_results(doctor_env, caplog):
    caplog.set_level(logging.DEBUG)
    result = runner.invoke(app, ["doctor", "--live"])
    out = result.output
    assert result.exit_code == 0, out
    assert out.index("Will contact:") < out.index("Live checks\n")
    assert "api.anthropic.com" in out and "claude auth status" in out
    assert "accepted: yes" in out and "token accepted: yes" in out
    assert "claude.ai login (not an API key): yes" in out
    assert doctor_env.model_calls() == []
    assert LEAK not in out and LEAK not in caplog.text


def test_doctor_live_json_keeps_the_checks_and_adds_live(doctor_env):
    result = runner.invoke(app, ["doctor", "--live", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data["checks"], list) and data["live"]
    kinds = {r["kind"]: r for r in data["live"]}
    assert kinds["anthropic"]["facts"] == {"accepted": True}
    assert "Will contact:" in result.stderr
    assert LEAK not in result.output


def test_doctor_live_exits_1_when_the_screen_scorer_is_refused(doctor_env, monkeypatch):
    monkeypatch.setattr(lc, "_anthropic_client", anthropic_factory(doctor_env, status=401))
    result = runner.invoke(app, ["doctor", "--live"])
    assert result.exit_code == 1
    assert "FAILED" in result.output and LEAK not in result.output


def test_plain_doctor_contacts_nothing(doctor_env):
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    assert doctor_env.seen == []


# --- doctor --live preamble: gated, origins only -------------------------------------------


def preamble(settings: Settings, mode: str = readiness.HOST) -> str:
    from jobhunter.ops import doctor

    report = doctor.Report([], mode, settings=settings)
    return "\n".join(doctor.live_preamble(report, lc.plan(settings, mode)))


def test_preamble_prints_only_the_origin_of_a_local_url(tmp_path):
    s = settings_for(
        tmp_path,
        scoring={
            "screen_scorer": "local:qwen",
            "local": {"base_url": f"http://user:{LEAK}@127.0.0.1:8080/v1?key={LEAK}"},
        },
    )
    out = preamble(s)
    assert "http://127.0.0.1:8080: /v1/models and /health" in out
    assert LEAK not in out and "user" not in out and "/v1?" not in out


def test_preamble_skips_contacts_a_gate_would_skip(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    s = settings_for(
        tmp_path,
        scoring={
            "screen_scorer": "local:qwen",
            "deep_scorer": "anthropic:claude-opus-5-5",
            "local": {"base_url": f"http://user:{LEAK}@10.0.0.5:8080/v1"},
        },
        mail={"client_secrets_path": str(tmp_path / "absent.json")},
    )
    out = preamble(s)
    assert "10.0.0.5" not in out and LEAK not in out  # non-loopback local: never contacted
    assert "Anthropic" not in out  # no credential: the check sends nothing
    assert "Google" not in out  # no client JSON
    assert "Nothing to contact here." in out


def test_preamble_names_the_anthropic_base_url_host(tmp_path, monkeypatch):
    s = settings_for(tmp_path)
    assert "https://api.anthropic.com" in preamble(s)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"https://u:{LEAK}@proxy.example.test:8443/a/")
    out = preamble(s)
    assert "the Anthropic API (https://proxy.example.test:8443): list models" in out
    assert "api.anthropic.com" not in out and LEAK not in out


def test_group_limits_the_live_plan(doctor_env):
    from jobhunter.ops import doctor

    everything = doctor.gather()
    assert {k for p in doctor.live_plan(everything) for k in p.rows} >= {
        "mail.token",
        "scorer.screen",
    }
    email = doctor.gather(group="Email alerts")
    rows = {r for p in doctor.live_plan(email) for r in p.rows}
    assert rows and rows <= {c.id for c in email.checks}
    assert "scorer.screen" not in rows
    out = runner.invoke(app, ["doctor", "--live", "--group", "email-alerts"]).output
    assert "Anthropic API" not in out and "Google" in out


def test_summary_counts_a_failing_live_check(doctor_env, monkeypatch):
    monkeypatch.setattr(lc, "_anthropic_client", anthropic_factory(doctor_env, status=401))
    out = runner.invoke(app, ["doctor", "--live"])
    assert out.exit_code == 1
    last = out.output.strip().splitlines()[-1]
    assert "1 essential failing" in last and "all essentials ok" not in last
    plain = runner.invoke(app, ["doctor"]).output.strip().splitlines()[-1]
    assert "all essentials ok" in plain


def test_terminal_doctor_never_names_the_setup_button(doctor_env):
    out = runner.invoke(app, ["doctor", "-v"]).output
    assert "Test (free)" not in out


def test_host_managing_gmail_row_names_jobhunter_ctr():
    from jobhunter.ops import doctor

    assert "jobhunter-ctr doctor --live" in doctor.terminal_detail(
        "use Test (free), or run `jobhunter-ctr mail status`", readiness.HOST_MANAGING
    )
