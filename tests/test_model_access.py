"""model_access (specs/020 "Credential detection"): each provider, each step of the subscription
order, and each mode. Local and free: no subprocess, no network, and the Claude login file is
only ever stat-ed, never opened. Fake values only."""

from __future__ import annotations

import builtins
import io
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobhunter import model_access as ma
from jobhunter import readiness
from jobhunter.apply import cli_runner, runner_state
from jobhunter.config import Apply, Paths, Scoring, Settings
from jobhunter.ops import preflight

NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
FAKE = "sk-ant-fake-0123456789"

# Every variable that changes an answer here, cleared from the real environment by
# using explicit ``environ`` dicts, never os.environ.
HOST, CONTAINER, MANAGING = readiness.HOST, readiness.CONTAINER, readiness.HOST_MANAGING


class Forbidden(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """Any subprocess, and any spawn of claude, fails the test."""

    def refuse(*args, **kwargs):
        raise Forbidden("model_access started a subprocess")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(cli_runner, "_spawn", refuse)


@pytest.fixture(autouse=True)
def login_never_opened(monkeypatch):
    """Opening the Claude login file for reading fails the test: it may only be stat-ed. (Tests
    create it with ``touch``, a write-only ``os.open``.)"""

    def guard(real):
        def wrapped(file, *args, **kwargs):
            if ma.CREDENTIALS_FILE in str(file):
                raise Forbidden(f"opened the Claude login file {file}")
            return real(file, *args, **kwargs)

        return wrapped

    real_os_open = os.open

    def os_open(path, flags, *args, **kwargs):
        reading = flags & os.O_ACCMODE != os.O_WRONLY
        if reading and ma.CREDENTIALS_FILE in str(path):
            raise Forbidden(f"opened the Claude login file {path}")
        return real_os_open(path, flags, *args, **kwargs)

    real_path_open = Path.open

    def path_open(self, *args, **kwargs):
        if self.name == ma.CREDENTIALS_FILE:
            raise Forbidden(f"opened the Claude login file {self}")
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(Path, "open", path_open)


def test_the_guard_catches_a_read(home):
    path = login_file(home)
    with pytest.raises(Forbidden):
        path.read_text()
    with pytest.raises(Forbidden):
        os.open(path, os.O_RDONLY)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    return Settings(paths=Paths(data_dir=data, db_path=data / "jobhunter.db"))


def with_runner(settings: Settings, runner: str) -> Settings:
    return settings.model_copy(update={"apply": Apply(runner=runner)})


def with_scorer(settings: Settings, spec: str) -> Settings:
    return settings.model_copy(update={"scoring": Scoring(screen_scorer=spec)})


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE llm_spend (day TEXT, model TEXT, tier TEXT, calls INTEGER, "
        "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)"
    )
    yield c
    c.close()


def spend(conn, day: str, tier: str = "packet-cli") -> None:
    conn.execute("INSERT INTO llm_spend VALUES (?, 'opus', ?, 1, 10, 10, 0)", (day, tier))


def login_file(home: Path, env: dict | None = None) -> Path:
    path = ma.claude_login_file(env or {}, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # empty: only its existence matters, and it is never read
    return path


def access(use, settings, *, mode=HOST, env=None, home=None, conn=None):
    return ma.access_for(
        use, None, settings, mode=mode, now=NOW, conn=conn, environ=env or {}, home=home
    )


def sub(settings, *, env=None, home=None, conn=None, platform="linux"):
    return ma.subscription_access(
        settings, now=NOW, conn=conn, environ=env or {}, home=home, platform=platform
    )


# --- scoring providers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "status"),
    [
        ({}, ma.MISSING),
        ({"ANTHROPIC_API_KEY": FAKE}, ma.OK),
        ({"ANTHROPIC_AUTH_TOKEN": "fake-bearer"}, ma.OK),
        ({"ANTHROPIC_PROFILE": "work"}, ma.LIKELY),
    ],
)
def test_anthropic_screening_each_source(settings, home, env, status):
    a = access(ma.SCREEN, settings, env=env, home=home)
    assert (a.provider, a.status, a.uses) == (ma.ANTHROPIC, status, (ma.SCREEN,))
    for value in env.values():
        assert value not in a.detail


def test_anthropic_sdk_profile_file_is_likely(settings, home):
    (home / ".config" / "anthropic").mkdir(parents=True)
    (home / ".config" / "anthropic" / "active_config").write_text("x", encoding="utf-8")
    a = access(ma.DEEP, settings, home=home)
    assert a.status == ma.LIKELY and "not checked" in a.detail


@pytest.mark.parametrize("spec", ["openrouter:x/y", "jev:typesafe/jev-1.13", "decisions:x"])
def test_openrouter_family(settings, home, spec):
    s = with_scorer(settings, spec)
    assert access(ma.SCREEN, s, home=home).status == ma.MISSING
    a = access(ma.SCREEN, s, env={"OPENROUTER_API_KEY": "sk-or-fake"}, home=home)
    assert (a.provider, a.status) == (ma.OPENROUTER, ma.OK)
    assert "sk-or-fake" not in a.detail


def test_openai_compat(settings, home):
    s = with_scorer(settings, "openai-compat:m")
    a = access(ma.SCREEN, s, home=home)
    assert (a.provider, a.status) == (ma.OPENAI_COMPAT, ma.MISSING)


def test_local_is_unknown_never_missing(settings, home):
    a = access(ma.SCREEN, with_scorer(settings, "local:qwen"), home=home)
    assert (a.provider, a.status) == (ma.LOCAL, ma.UNKNOWN)
    assert ma.problems([ma.SCREEN], with_scorer(settings, "local:qwen"), mode=HOST, now=NOW,
                       environ={}, home=home) == []  # fmt: skip


# --- the subscription order ------------------------------------------------------------------


def test_step1_not_installed_is_missing(settings, home):
    login_file(home)
    a = sub(settings, env={"CLAUDE_CODE_OAUTH_TOKEN": "fake"}, home=home)
    assert (a.provider, a.status) == (ma.SUBSCRIPTION, ma.MISSING)
    assert "not installed" in a.detail


def test_step2_runner_off_is_missing_with_reason(settings, home, fake_claude):
    login_file(home)
    runner_state.turn_off(settings.paths.data_dir, "not a claude.ai subscription login")
    a = sub(settings, home=home)
    assert a.status == ma.MISSING and "not a claude.ai subscription login" in a.detail


def test_step3_no_login_is_missing(settings, home, fake_claude):
    a = sub(settings, home=home)
    assert a.status == ma.MISSING and "no login was found" in a.detail


def test_step3_no_login_on_macos_is_unknown(settings, home, fake_claude):
    a = sub(settings, home=home, platform="darwin")
    assert a.status == ma.UNKNOWN


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CLAUDE_CONFIG_DIR": "SET"},
        {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "SET", "CLAUDE_CONFIG_DIR": "/nowhere"},
        {"CLAUDE_SECURESTORAGE_CONFIG_DIR": ""},
    ],
)
def test_step5_login_file_is_likely_and_only_stat_ed(settings, home, tmp_path, fake_claude, env):
    env = {k: (str(tmp_path / "cfg") if v == "SET" else v) for k, v in env.items()}
    path = login_file(home, env)
    a = sub(settings, env=env, home=home)
    assert a.status == ma.LIKELY and "not checked" in a.detail
    path.unlink()
    assert sub(settings, env=env, home=home).status == ma.MISSING


def test_login_dir_follows_the_cli(home, tmp_path):
    assert ma.claude_login_file({}, home) == home / ".claude" / ".credentials.json"
    assert ma.claude_login_dir({"CLAUDE_CONFIG_DIR": "/c"}, home) == Path("/c")
    env = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/s", "CLAUDE_CONFIG_DIR": "/c"}
    assert ma.claude_login_dir(env, home) == Path("/s")
    env = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "", "CLAUDE_CONFIG_DIR": "/c"}
    assert ma.claude_login_dir(env, home) == home / ".claude"


def test_step5_oauth_token_env_is_likely(settings, home, fake_claude):
    a = sub(settings, env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth"}, home=home)
    assert a.status == ma.LIKELY and "fake-oauth" not in a.detail


def test_step4_auth_ok_with_login_is_ok(settings, home, fake_claude):
    login_file(home)
    cli_runner._AUTH_OK.add(cli_runner.find_binary())
    assert sub(settings, home=home).status == ma.OK


def test_step4_auth_ok_without_login_is_missing(settings, home, fake_claude):
    cli_runner._AUTH_OK.add(cli_runner.find_binary())
    assert sub(settings, home=home).status == ma.MISSING


def test_step4_recent_cli_draft_with_login_is_ok(settings, home, fake_claude, conn):
    login_file(home)
    spend(conn, "2026-09-20")
    a = sub(settings, home=home, conn=conn)
    assert a.status == ma.OK and "2026-09-20" in a.detail


def test_step4_recent_cli_draft_without_login_is_missing(settings, home, fake_claude, conn):
    spend(conn, "2026-10-10")
    assert sub(settings, home=home, conn=conn).status == ma.MISSING


def test_old_or_paid_rows_do_not_count(settings, home, fake_claude, conn):
    login_file(home)
    spend(conn, "2026-09-01")  # 40 days ago
    spend(conn, "2026-10-10", tier="packet")  # an API draft, not the subscription
    assert sub(settings, home=home, conn=conn).status == ma.LIKELY


def test_the_subscription_never_runs_claude(settings, home, fake_claude, conn):
    login_file(home)
    for env in ({}, {"CLAUDE_CODE_OAUTH_TOKEN": "x"}):
        sub(settings, env=env, home=home, conn=conn)
    assert fake_claude.calls(auth=None) == []


# --- modes -------------------------------------------------------------------------------------


@pytest.mark.parametrize("use", [ma.DRAFT, ma.FACTCHECK])
@pytest.mark.parametrize("runner", ["cli", "api"])
def test_container_drafting_is_unknown_and_never_a_banner(settings, home, use, runner):
    s = with_runner(settings, runner)
    a = access(use, s, mode=CONTAINER, env={}, home=home)
    assert a.status == ma.UNKNOWN and "on the host" in a.detail
    assert ma.problems([use], s, mode=CONTAINER, now=NOW, environ={}, home=home) == []


def test_container_screening_uses_the_container_environment(settings, home):
    a = access(ma.SCREEN, settings, mode=CONTAINER, env={"ANTHROPIC_API_KEY": FAKE}, home=home)
    assert a.status == ma.OK
    assert access(ma.SCREEN, settings, mode=CONTAINER, env={}, home=home).status == ma.MISSING


def test_host_managing_screening_follows_the_dropin(settings, home):
    env = {"ANTHROPIC_API_KEY": FAKE}  # the host shell's key does not reach the containers
    assert access(ma.SCREEN, settings, mode=MANAGING, env=env, home=home).status == ma.MISSING
    drop = preflight.dropin_path(
        preflight.default_unit_dir(home), preflight.RUN, "jobhunter_anthropic"
    )
    drop.parent.mkdir(parents=True)
    drop.write_text("[Container]\n", encoding="utf-8")
    assert access(ma.SCREEN, settings, mode=MANAGING, env={}, home=home).status == ma.OK


def test_host_managing_drafting_is_judged_from_the_host(settings, home, fake_claude):
    api = with_runner(settings, "api")
    env = {"ANTHROPIC_AUTH_TOKEN": "fake"}
    assert access(ma.DRAFT, api, mode=MANAGING, env=env, home=home).status == ma.OK
    assert access(ma.DRAFT, api, mode=MANAGING, env={}, home=home).status == ma.MISSING
    cli = with_runner(settings, "cli")
    assert access(ma.DRAFT, cli, mode=MANAGING, home=home).status == ma.MISSING  # no login
    login_file(home)
    assert access(ma.FACTCHECK, cli, mode=MANAGING, home=home).status == ma.LIKELY


def test_host_api_drafting_accepts_every_sdk_source(settings, home):
    api = with_runner(settings, "api")
    for env in ({"ANTHROPIC_API_KEY": FAKE}, {"ANTHROPIC_AUTH_TOKEN": "t"}):
        assert access(ma.DRAFT, api, env=env, home=home).status == ma.OK
    assert access(ma.DRAFT, api, env={"ANTHROPIC_PROFILE": "p"}, home=home).status == ma.LIKELY


# --- problems() ------------------------------------------------------------------------------


def test_problems_merge_uses_that_fail_alike(settings, home):
    found = ma.problems(ma.USES, with_runner(settings, "api"), mode=HOST, now=NOW, environ={},
                        home=home)  # fmt: skip
    assert len(found) == 1
    (only,) = found
    assert only.provider == ma.ANTHROPIC and only.uses == ma.USES
    assert ma.summary(only).endswith(
        "Screening, the deep pass, drafting and the fact check cannot run."
    )


def test_problems_skip_likely_and_ok(settings, home, fake_claude):
    login_file(home)
    env = {"ANTHROPIC_PROFILE": "p"}
    assert ma.problems(ma.USES, settings, mode=HOST, now=NOW, environ=env, home=home) == []


def test_problems_name_the_missing_subscription(settings, home):
    found = ma.problems(ma.DRAFTING_USES, settings, mode=HOST, now=NOW,
                        environ={"ANTHROPIC_API_KEY": FAKE}, home=home)  # fmt: skip
    assert [(p.provider, p.uses) for p in found] == [(ma.SUBSCRIPTION, ma.DRAFTING_USES)]
    assert "not installed" in ma.summary(found[0])


def test_unknown_use_or_mode_raises(settings):
    with pytest.raises(ValueError):
        ma.access_for("nope", None, settings, mode=HOST, now=NOW, environ={})
    with pytest.raises(ValueError):
        ma.access_for(ma.SCREEN, None, settings, mode="nope", now=NOW, environ={})


def test_explicit_choice_overrides_settings(settings, home):
    choice = ma.Choice("jev:typesafe/jev-1.13")
    a = ma.access_for(ma.SCREEN, choice, settings, mode=HOST, now=NOW,
                      environ={"OPENROUTER_API_KEY": "sk-or-x"}, home=home)  # fmt: skip
    assert (a.provider, a.status) == (ma.OPENROUTER, ma.OK)
