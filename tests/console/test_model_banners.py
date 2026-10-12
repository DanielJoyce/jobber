# ruff: noqa: F811
"""No-model-access banners (specs/020 M7): show per state, hide per state, no leaks, no network.
Synthetic data only."""

from __future__ import annotations

import re
import subprocess

import httpx
import pytest
from test_packet_pages import (  # noqa: F401
    TEXT,
    _fresh_shared_state,
    client,
    conn,
    db_path,
    make_client,
    new_packet,
    pages,
    pdir,
)

from jobhunter import model_access
from jobhunter.apply import cli_runner
from jobhunter.config import BLOCKED_SPEC

SENTINEL = "SENTINEL-9c41e7d2b0"
BANNER = 'data-model-banner="'


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch, tmp_path):
    """Nothing here may start a process or send an HTTP request (the conftest guard also stops
    sockets). The user's own keys never leak in: the environment is explicit per test."""

    def boom(*a, **k):
        raise AssertionError("a banner render touched the network or a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli_runner, "find_binary", lambda: None)


def env(c, tmp_path, mode="host", **variables):
    c.app.state.setup_mode = mode
    c.app.state.setup_environ = dict(variables)
    c.app.state.setup_home = tmp_path / "home"


def banners(html: str) -> list[str]:
    return re.findall(r'<div class="setup-banner model-banner".*?</div>\s*</div>', html, re.S)


def test_rescore_panel_shows_for_a_missing_key_and_hides_when_set(client, tmp_path):
    # The default scorer in the fixtures is openrouter:...; select it explicitly.
    spec = client.app.state.settings.scoring.screen_scorer
    env(client, tmp_path)
    html = client.get("/prefs/rescore/panel").text
    got = banners(html)
    assert len(got) == 1
    assert "No OpenRouter key found" in got[0] and "Screening cannot run" in got[0]
    assert 'href="/help/openrouter#get-an-account-credits-and-a-key"' in got[0]
    assert "jobhunter models show" in got[0]  # /models is not served yet: the command instead
    assert "data-model-banner-hide" in got[0]
    assert spec.startswith("openrouter:")
    env(client, tmp_path, OPENROUTER_API_KEY=SENTINEL)
    html = client.get("/prefs/rescore/panel").text
    assert not banners(html)
    assert SENTINEL not in html


def test_prefs_page_carries_the_rescore_banner(client, tmp_path):
    env(client, tmp_path)
    assert len(banners(client.get("/prefs").text)) == 1


def test_unknown_and_likely_never_banner(client, tmp_path):
    """A local server cannot be judged from here: unknown, so no banner."""
    from jobhunter.config import Scoring

    client.app.state.settings.scoring = Scoring(screen_scorer="local:some-model")
    env(client, tmp_path)
    assert (
        model_access.problems(
            ["screen"],
            client.app.state.settings,
            mode="host",
            now=__import__("datetime").datetime.now(),
        )
        == []
    )
    assert not banners(client.get("/captured").text)
    assert not banners(client.get("/prefs/rescore/panel").text)


def test_captured_page_names_screening(client, tmp_path):
    from jobhunter.config import Scoring

    client.app.state.settings.scoring = Scoring(screen_scorer="anthropic:claude-haiku-5-5")
    env(client, tmp_path)
    html = client.get("/captured").text
    got = banners(html)
    assert len(got) == 1 and "No Anthropic API credential found" in got[0]
    assert 'href="/help/scorers#get-an-anthropic-api-key"' in got[0]
    env(client, tmp_path, ANTHROPIC_API_KEY=SENTINEL)
    html = client.get("/captured").text
    assert not banners(html) and SENTINEL not in html


def test_packet_page_names_drafting_and_the_fact_check(client, tmp_path):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    env(client, tmp_path)
    html = client.get(f"/packet/{pid}").text
    got = [b for b in banners(html) if "claude command is not installed" in b]
    assert len(got) == 1
    assert "Drafting and the fact check cannot run" in got[0]
    assert any("No OpenRouter key found" in b for b in banners(html))  # Score now panel
    assert 'href="/help/drafting#the-cli-runner-log-in-to-claude-code"' in got[0]


def test_packet_page_in_a_container_is_unknown_so_no_drafting_banner(client, tmp_path):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    env(client, tmp_path, mode="container")
    html = client.get(f"/packet/{pid}").text
    assert not [b for b in banners(html) if "Drafting" in b]


def test_blocked_use_gets_its_own_line(client, tmp_path):
    from jobhunter.config import Scoring

    client.app.state.settings.scoring = Scoring(screen_scorer=BLOCKED_SPEC)
    env(client, tmp_path, OPENROUTER_API_KEY=SENTINEL)
    html = client.get("/captured").text
    got = banners(html)
    assert len(got) == 1
    assert "models.toml holds an invalid value" in got[0] and "blocked" in got[0]
    assert "/help/troubleshooting#a-model-use-is-blocked-by-the-saved-choice" in got[0]
    assert SENTINEL not in html


def test_models_link_only_when_the_page_exists(client, tmp_path):
    from jobhunter.config import Scoring

    client.app.state.settings.scoring = Scoring(screen_scorer="anthropic:claude-haiku-5-5")
    env(client, tmp_path)
    assert 'href="/models"' not in client.get("/captured").text
    client.app.add_api_route("/models", lambda: "x", methods=["GET"])
    assert 'href="/models"' in client.get("/captured").text


def test_a_crashing_check_hides_the_banner_not_the_page(client, tmp_path, monkeypatch):
    env(client, tmp_path)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(model_access, "problems", broken)
    r = client.get("/captured")
    assert r.status_code == 200 and not banners(r.text)


def test_hide_is_wired_and_styles_are_dark_and_phone_safe(client):
    js = client.get("/static/app.js").text
    assert "data-model-banner-hide" in js and "sessionStorage" in js
    css = client.get("/static/app.css").text
    assert ".model-banner[hidden]" in css
    # Colors come from theme tokens (dark mode follows them); the row wraps at phone width.
    block = css[css.index(".setup-banner {") :].split("\n", 1)[0]
    assert "var(--surface-1)" in block and "flex-wrap: wrap" in block
    assert "overflow-wrap: anywhere" in block
