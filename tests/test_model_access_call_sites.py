"""The three places that once checked only ANTHROPIC_API_KEY (specs/020 "Every model use"): the
re-score panel's scorer list, the packet page's runner status and the drafting API pre-check.
Each now goes through credential_for, so every source the SDK accepts counts. Fake values only;
nothing here calls the SDK, the network or claude."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobhunter.apply import generator
from jobhunter.config import Apply, Paths, Scoring, Settings
from jobhunter.console import packet_docs
from jobhunter.console.rescore_routes import ANTHROPIC_HAIKU, available_scorers
from jobhunter.core import db

NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)

SOURCES = [
    pytest.param({"ANTHROPIC_API_KEY": "sk-ant-fake"}, id="api-key"),
    pytest.param({"ANTHROPIC_AUTH_TOKEN": "fake-bearer"}, id="auth-token"),
    pytest.param({"ANTHROPIC_PROFILE": "work"}, id="profile"),
    pytest.param({"ANTHROPIC_CONFIG_DIR": "/nonexistent"}, id="config-dir"),
    pytest.param(
        {
            "ANTHROPIC_FEDERATION_RULE_ID": "rule",
            "ANTHROPIC_ORGANIZATION_ID": "org",
            "ANTHROPIC_IDENTITY_TOKEN": "jwt",
        },
        id="workload-identity",
    ),
]


def profile_file(home: Path) -> None:
    path = Path(home) / ".config" / "anthropic" / "configs" / "default.json"
    path.parent.mkdir(parents=True)
    path.touch()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    return Settings(
        paths=Paths(data_dir=data, db_path=data / "jobhunter.db"),
        scoring=Scoring(screen_scorer="openrouter:test/model"),
        apply=Apply(runner="api", daily_cap_usd=5.0),
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


# --- the re-score panel's scorer list --------------------------------------------------------


@pytest.mark.parametrize("env", SOURCES)
def test_rescore_list_offers_anthropic_for_each_source(settings, env):
    specs = [s for s, _ in available_scorers(settings.scoring, env)]
    assert ANTHROPIC_HAIKU in specs


def test_rescore_list_offers_anthropic_for_a_profile_file(settings):
    profile_file(Path.home())  # HOME is a per-test temp dir (tests/conftest.py)
    assert ANTHROPIC_HAIKU in [s for s, _ in available_scorers(settings.scoring, {})]


def test_rescore_list_without_any_source(settings):
    specs = [s for s, _ in available_scorers(settings.scoring, {"ANTHROPIC_API_KEY": "  "})]
    assert ANTHROPIC_HAIKU not in specs and specs == ["openrouter:test/model"]


def test_rescore_list_labels_never_carry_values(settings):
    env = {"ANTHROPIC_AUTH_TOKEN": "fake-bearer", "OPENROUTER_API_KEY": "sk-or-fake"}
    text = repr(available_scorers(settings.scoring, env))
    assert "fake-bearer" not in text and "sk-or-fake" not in text
    assert "jev:" in text


# --- the packet page's runner status ----------------------------------------------------------


@pytest.mark.parametrize("env", SOURCES)
def test_runner_status_api_ok_for_each_source(conn, settings, env):
    status = packet_docs.runner_status(conn, settings, NOW, environ=env)
    assert status.api_ok, status.api_reason


def test_runner_status_api_ok_for_a_profile_file(conn, settings):
    profile_file(Path.home())
    assert packet_docs.runner_status(conn, settings, NOW, environ={}).api_ok


def test_runner_status_without_any_source(conn, settings):
    status = packet_docs.runner_status(conn, settings, NOW, environ={})
    assert not status.api_ok
    assert status.api_reason == generator.NO_API_CREDENTIAL
    assert "ANTHROPIC_AUTH_TOKEN" in status.api_reason


# --- the drafting API pre-check --------------------------------------------------------------


def preflight(conn, settings, env):
    generator._preflight(
        conn, settings, "api", now=NOW, estimate_usd=0.01, confirm_paid=False, environ=env
    )


@pytest.mark.parametrize("env", SOURCES)
def test_preflight_passes_for_each_source(conn, settings, env):
    preflight(conn, settings, env)  # no ApplyRefused


def test_preflight_passes_for_a_profile_file(conn, settings):
    profile_file(Path.home())
    preflight(conn, settings, {})


def test_preflight_refuses_without_any_source(conn, settings):
    with pytest.raises(generator.ApplyRefused, match="ANTHROPIC_AUTH_TOKEN"):
        preflight(conn, settings, {})


def test_preflight_still_checks_the_cap_after_the_credential(conn, settings):
    capped = settings.model_copy(update={"apply": Apply(runner="api", daily_cap_usd=0.0)})
    with pytest.raises(generator.ApplyRefused, match="daily cap"):
        preflight(conn, capped, {"ANTHROPIC_AUTH_TOKEN": "fake-bearer"})
