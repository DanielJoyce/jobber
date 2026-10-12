"""/models (specs/020 M4): the page, its routes and their gates, the field-only settings swap,
the digest, the queued re-score question, and the rules that nothing leaks and nothing reaches
the network. Synthetic data only; scorer and client factories fail the test."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunter import readiness
from jobhunter.config import Settings, load_settings, read_models_file
from jobhunter.console import models_routes as mr
from jobhunter.console.app import create_app
from jobhunter.core import db

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.168.1.20", 50000)
# Key-shaped, so config.shown_value refuses it: it must never reach a page.
SENTINEL = "sk-ant-SENTINEL7f3a9c"


@pytest.fixture(autouse=True)
def _fresh():
    readiness.clear_caches()
    yield
    readiness.clear_caches()


@pytest.fixture(autouse=True)
def _no_network_no_models(monkeypatch: pytest.MonkeyPatch):
    """A recording transport on every httpx client (an error-raising one would be swallowed
    by OpenRouterScorer.catalog()), and no Anthropic client or subprocess at all."""
    import subprocess

    import anthropic

    requests: list[httpx.Request] = []
    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        kwargs["transport"] = httpx.MockTransport(record)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)

    def no_client(*a, **k):
        pytest.fail("no Anthropic client may be built")

    monkeypatch.setattr(anthropic, "Anthropic", no_client)

    def no_popen(*a, **k):
        pytest.fail("no subprocess may run")

    monkeypatch.setattr(subprocess, "Popen", no_popen)
    return requests


def write_config(tmp_path: Path, body: str = "") -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{data / "jobhunter.db"}"\n'
        f'profile_dir = "{data / "profile"}"\ncache_dir = "{tmp_path / "cache"}"\n' + body
    )
    return cfg


def settings_from(cfg: Path) -> Settings:
    return load_settings(cfg)


def make_client(
    settings: Settings, *, peer=LOOPBACK, allow_remote: bool = False, mode: str = readiness.HOST
) -> TestClient:
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    conn.close()
    app = create_app(
        settings,
        lambda: db.connect(settings.paths.db_path),
        clock=lambda: NOW,
        allow_remote=allow_remote,
    )
    app.state.setup_mode = mode
    return TestClient(app, client=peer)


@pytest.fixture
def cfg(tmp_path):
    return write_config(tmp_path)


@pytest.fixture
def client(cfg):
    return make_client(settings_from(cfg))


def models_path(client: TestClient) -> Path:
    return Path(client.app.state.settings.paths.data_dir) / "models.toml"


def digest_of(text: str) -> str:
    m = re.search(r'id="models-digest" name="digest" value="([0-9a-f]*)"', text)
    assert m, "the page carries #models-digest"
    return m.group(1)


def selected(text: str, name: str) -> str:
    """The value of the selected option of the select named ``name``."""
    m = re.search(rf'<select id="models-{name}" name="{name}">(.*?)</select>', text, re.S)
    assert m, f"no select {name}"
    sel = re.findall(r'<option value="([^"]*)" selected>', m.group(1))
    assert len(sel) == 1, sel
    return unescape(sel[0])


def row_html(text: str, row: str) -> str:
    m = re.search(rf'<section class="mo-row[^"]*" id="models-row-{row}".*?</section>', text, re.S)
    assert m, f"no row {row}"
    return m.group(0)


def save(client: TestClient, row: str, page: str, **values) -> httpx.Response:
    """Post a row as the browser does: every field of the row with the shown values, the
    changed ones replaced by ``values``."""
    data: dict[str, object] = {"digest": digest_of(page)}
    html = row_html(page, row)
    for name, shown in re.findall(r'name="shown_([a-z_]+)" value="([^"]*)"', html):
        data[f"shown_{name}"] = unescape(shown)
        data[name] = unescape(shown)
    data.update(values)
    return client.post(f"/models/{row}", data=data, headers={"HX-Request": "true"})


# ─── The page ───────────────────────────────────────────────────────────────


def test_page_renders_every_row_with_the_effective_value(client, _no_network_no_models):
    r = client.get("/models")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    text = r.text
    assert '<a href="/models" aria-current="page">Models</a>' in text
    for row in mr.ROWS:
        assert f'id="models-row-{row}"' in text
    assert selected(text, "screen") == "anthropic:claude-haiku-5-5"
    assert selected(text, "deep") == "anthropic:claude-opus-5-5"
    assert selected(text, "draft_cli") == "opus"
    assert "per 1,000 jobs" in text
    assert _no_network_no_models == []


def test_nav_puts_models_after_prefs(client):
    text = client.get("/models").text
    assert text.index('href="/prefs"') < text.index('href="/models"') < text.index('href="/setup"')


def test_effective_value_not_offered_is_still_selected_with_its_reason(tmp_path):
    cfg = write_config(
        tmp_path,
        '[scoring]\nscreen_scorer = "anthropic:claude-opus-5-5"\n'
        '[apply]\ncli_model = "claude-sonnet-5-5"\n',
    )
    client = make_client(settings_from(cfg))
    text = client.get("/models").text
    assert selected(text, "screen") == "anthropic:claude-opus-5-5"
    screen = unescape(row_html(text, "screen"))
    assert "config.toml" in screen
    assert "not offered for screening new jobs" in screen
    # Its own price line, not the first option's.
    opus = re.search(r"About \$([0-9.]+) per 1,000 jobs", screen)
    haiku_client_text = make_client(settings_from(write_config(tmp_path / "h"))).get("/models")
    haiku = re.search(r"About \$([0-9.]+) per 1,000 jobs", unescape(haiku_client_text.text))
    assert opus and haiku and float(opus.group(1)) > float(haiku.group(1))


def test_openrouter_slug_with_no_cache_is_selected(tmp_path):
    cfg = write_config(tmp_path, '[scoring]\nscreen_scorer = "openrouter:acme/tiny-1"\n')
    client = make_client(settings_from(cfg))
    text = client.get("/models").text
    assert selected(text, "screen") == "openrouter:acme/tiny-1"
    assert "No known price" in unescape(row_html(text, "screen"))
