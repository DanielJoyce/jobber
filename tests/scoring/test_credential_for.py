"""credential_for (specs/019 "Scorer credentials"): every Anthropic SDK source alone, the
other providers, and that nothing is read, only stat-ed. Fake values only."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.config import Scoring
from jobhunter.scoring.scorers import (
    MISSING,
    PRESENT,
    SDK_NOTE,
    UNKNOWN,
    credential_for,
)

ANTHROPIC = "anthropic:claude-haiku-5-5"
FAKE = "sk-ant-fake-0123456789"


def ask(spec: str, env: dict[str, str], home: Path, scoring: Scoring | None = None):
    return credential_for(spec, scoring or Scoring(), environ=env, home=home)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def test_nothing_set_is_missing(home):
    cred = ask(ANTHROPIC, {}, home)
    assert cred.status == MISSING
    assert cred.names == ("ANTHROPIC_API_KEY",)


@pytest.mark.parametrize(
    ("env", "names"),
    [
        ({"ANTHROPIC_API_KEY": FAKE}, ("ANTHROPIC_API_KEY",)),
        ({"ANTHROPIC_AUTH_TOKEN": "fake-bearer"}, ("ANTHROPIC_AUTH_TOKEN",)),
        ({"ANTHROPIC_PROFILE": "work"}, ("ANTHROPIC_PROFILE",)),
        ({"ANTHROPIC_CONFIG_DIR": "/nonexistent/dir"}, ("ANTHROPIC_CONFIG_DIR",)),
    ],
)
def test_each_environment_source_alone_is_present(home, env, names):
    cred = ask(ANTHROPIC, env, home)
    assert cred.status == PRESENT
    assert cred.names == names


def test_workload_identity_literal_token(home):
    env = {
        "ANTHROPIC_FEDERATION_RULE_ID": "rule",
        "ANTHROPIC_ORGANIZATION_ID": "org",
        "ANTHROPIC_IDENTITY_TOKEN": "jwt",
    }
    cred = ask(ANTHROPIC, env, home)
    assert cred.status == PRESENT
    assert cred.note == SDK_NOTE


def test_workload_identity_token_file(home):
    env = {
        "ANTHROPIC_FEDERATION_RULE_ID": "rule",
        "ANTHROPIC_ORGANIZATION_ID": "org",
        "ANTHROPIC_IDENTITY_TOKEN_FILE": "/run/secrets/jwt",
    }
    assert ask(ANTHROPIC, env, home).status == PRESENT


@pytest.mark.parametrize(
    "env",
    [
        {"ANTHROPIC_IDENTITY_TOKEN": "jwt"},  # no federation ids
        {"ANTHROPIC_FEDERATION_RULE_ID": "rule", "ANTHROPIC_IDENTITY_TOKEN": "jwt"},  # no org
        {"ANTHROPIC_FEDERATION_RULE_ID": "rule", "ANTHROPIC_ORGANIZATION_ID": "org"},  # no token
    ],
)
def test_incomplete_workload_identity_is_missing(home, env):
    assert ask(ANTHROPIC, env, home).status == MISSING


@pytest.mark.parametrize("relative", ["active_config", "configs/default.json"])
def test_profile_files_alone_are_present_and_only_stat_ed(home, relative, monkeypatch):
    target = home / ".config" / "anthropic" / relative
    target.parent.mkdir(parents=True)
    target.write_text("SENTINEL-NOT-READ", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise AssertionError("credential_for read a file; it may only stat")

    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    cred = ask(ANTHROPIC, {}, home)
    assert cred.status == PRESENT
    assert cred.note == SDK_NOTE
    assert cred.names == (relative,)


def test_profile_file_in_a_config_dir_override(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "configs").mkdir(parents=True)
    (elsewhere / "configs" / "default.json").write_text("{}", encoding="utf-8")
    # the variable itself already counts as an explicit profile selection
    cred = ask(ANTHROPIC, {"ANTHROPIC_CONFIG_DIR": str(elsewhere)}, home)
    assert cred.status == PRESENT
    assert cred.names == ("ANTHROPIC_CONFIG_DIR",)


def test_api_key_notes_a_wrong_shape_without_echoing_it(home):
    assert ask(ANTHROPIC, {"ANTHROPIC_API_KEY": FAKE}, home).note == ""
    odd = ask(ANTHROPIC, {"ANTHROPIC_API_KEY": "not-an-anthropic-key"}, home)
    assert odd.status == PRESENT
    assert "doesn't look like" in odd.note
    assert "not-an-anthropic-key" not in repr(odd)


def test_blank_values_do_not_count(home):
    env = {"ANTHROPIC_API_KEY": "  ", "ANTHROPIC_AUTH_TOKEN": ""}
    assert ask(ANTHROPIC, env, home).status == MISSING


def test_bare_name_is_anthropic(home):
    assert ask("claude-haiku-5-5", {"ANTHROPIC_API_KEY": FAKE}, home).status == PRESENT
    assert ask("claude-haiku-5-5", {}, home).status == MISSING


@pytest.mark.parametrize("provider", ["openrouter", "jev", "decisions"])
def test_openrouter_family_uses_the_configured_variable(home, provider):
    spec = f"{provider}:x/y"
    assert ask(spec, {}, home).status == MISSING
    cred = ask(spec, {"OPENROUTER_API_KEY": "sk-or-fake"}, home)
    assert cred.status == PRESENT
    assert cred.names == ("OPENROUTER_API_KEY",)
    renamed = Scoring.model_validate({"openrouter": {"api_key_env": "MY_ROUTER_KEY"}})
    assert ask(spec, {"OPENROUTER_API_KEY": "sk-or-fake"}, home, renamed).status == MISSING
    assert ask(spec, {"MY_ROUTER_KEY": "sk-or-fake"}, home, renamed).status == PRESENT


def test_openrouter_wrong_prefix_is_a_note(home):
    cred = ask("openrouter:x/y", {"OPENROUTER_API_KEY": "weird"}, home)
    assert cred.status == PRESENT
    assert "doesn't look like" in cred.note


def test_openai_compat(home):
    spec = "openai-compat:m"
    assert ask(spec, {}, home).status == MISSING
    assert ask(spec, {"OPENAI_COMPAT_API_KEY": "k"}, home).status == PRESENT
    keyless = Scoring.model_validate({"openai_compat": {"api_key_env": ""}})
    assert ask(spec, {}, home, keyless).status == PRESENT


def test_local_is_unknown_never_missing(home):
    cred = ask("local:qwen", {}, home)
    assert cred.status == UNKNOWN
    assert cred.names == ("LLAMA_API_KEY",)
