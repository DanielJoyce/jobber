"""/models (specs/020 M4): the page, its routes and their gates, the field-only settings swap,
the digest, the queued re-score question, and the rules that nothing leaks and nothing reaches
the network. Synthetic data only; scorer and client factories fail the test."""

from __future__ import annotations

import json
import re
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

        kwargs.setdefault("transport", httpx.MockTransport(record))
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


# ─── Save ───────────────────────────────────────────────────────────────────


def test_save_writes_only_changed_fields_and_swaps_only_them(tmp_path, cfg):
    client = make_client(settings_from(cfg))
    before = client.app.state.settings
    page = client.get("/models").text
    # A config.toml edit after the console started: it waits for its own Apply.
    cfg.write_text(cfg.read_text() + '[apply]\napi_model = "claude-sonnet-5-5"\n')
    r = save(client, "drafting", page, effort="high")
    assert r.status_code == 200, r.text
    assert "Saved at" in r.text
    _exists, _digest, data, _p = read_models_file(models_path(client))
    assert data == {"apply": {"effort": "high"}}  # never cli_model, runner or api_model
    after = client.app.state.settings
    assert after is not before
    assert after.apply.effort == "high"
    assert after.apply.api_model == before.apply.api_model == "claude-opus-5-5"
    assert after.scoring == before.scoring and after.paths == before.paths
    assert after.models_layers.source("apply", "effort") == "models.toml"
    row = client.app.state.conn_factory().execute("SELECT * FROM model_change").fetchall()
    assert len(row) == 1


def test_stale_digest_gets_409_and_writes_nothing(client):
    page = client.get("/models").text
    path = models_path(client)
    path.write_text('[apply]\neffort = "low"\n')  # another writer
    r = save(client, "drafting", page, effort="high")
    assert r.status_code == 409
    assert "changed elsewhere" in r.text
    assert path.read_text() == '[apply]\neffort = "low"\n'
    assert client.app.state.settings.apply.effort == "medium"


def test_two_saves_from_one_page_view_succeed_with_the_swapped_digest(client):
    page = client.get("/models").text
    first = save(client, "drafting", page, effort="high")
    assert first.status_code == 200
    new_digest = digest_of(first.text)
    assert 'id="models-digest" name="digest" value="' + new_digest + '" hx-swap-oob="true"' in (
        first.text
    )
    # The second row's form is still the old page's, but #models-digest was swapped.
    second_page = page.replace(digest_of(page), new_digest)
    second = save(client, "deep", second_page, deep="anthropic:claude-sonnet-5-5")
    assert second.status_code == 200, second.text
    _e, _d, data, _p = read_models_file(models_path(client))
    assert data == {
        "apply": {"effort": "high"},
        "scoring": {"deep_scorer": "anthropic:claude-sonnet-5-5"},
    }
    s = client.app.state.settings
    assert (s.apply.effort, s.scoring.deep_scorer) == ("high", "anthropic:claude-sonnet-5-5")


def test_unchanged_save_writes_nothing(client):
    page = client.get("/models").text
    r = save(client, "screen", page)
    assert r.status_code == 200 and "No change" in r.text
    assert not models_path(client).exists()


def test_invalid_value_is_refused_and_not_written(client):
    page = client.get("/models").text
    r = save(client, "screen", page, screen_other="anthropic:claude-opus-5-5")
    assert r.status_code == 422  # Opus 5.5 can never screen: no "save anyway"
    text = unescape(r.text)
    assert "Not saved" in text
    assert not models_path(client).exists()
    assert client.app.state.settings.scoring.screen_scorer == "anthropic:claude-haiku-5-5"


def test_save_anyway_for_an_uncached_openrouter_slug(client):
    page = client.get("/models").text
    r = save(client, "screen", page, screen_other="openrouter:acme/unlisted-1")
    assert r.status_code == 200
    text = unescape(r.text)
    assert "Save anyway" in text and "not in the cached OpenRouter list" in text
    assert not models_path(client).exists()
    assert selected(r.text, "screen") == "openrouter:acme/unlisted-1"  # the candidate stays
    slug = "openrouter:acme/unlisted-1"
    r = save(client, "screen", page, screen_other=slug, force_for_screen=slug)
    assert "Saved at" in r.text
    assert client.app.state.settings.scoring.screen_scorer == "openrouter:acme/unlisted-1"


def test_preview_prices_the_candidate_not_the_effective_value(client):
    r = client.post(
        "/models/deep/preview",
        data={"deep": "anthropic:claude-sonnet-5-5", "shown_deep": "anthropic:claude-opus-5-5"},
    )
    sonnet = float(re.search(r"About \$([0-9.]+) a job", r.text).group(1))
    r = client.post("/models/deep/preview", data={"deep": "anthropic:claude-opus-5-5"})
    opus = float(re.search(r"About \$([0-9.]+) a job", r.text).group(1))
    assert sonnet < opus
    assert not models_path(client).exists()


def test_concurrent_saves_serialize_under_the_settings_lock(client):
    page = client.get("/models").text
    lock = client.app.state.settings_lock
    results: list[int] = []
    lock.acquire()
    try:
        t = threading.Thread(
            target=lambda: results.append(save(client, "drafting", page, effort="low").status_code)
        )
        t.start()
        time.sleep(0.3)
        assert not models_path(client).exists(), "the save waited for the lock"
        assert results == []
    finally:
        lock.release()
    t.join(5)
    assert results == [200]
    assert client.app.state.settings.apply.effort == "low"


def test_models_file_is_private(client):
    page = client.get("/models").text
    save(client, "drafting", page, effort="low")
    assert models_path(client).stat().st_mode & 0o777 == 0o600


# ─── Reset, revert, load, reset file ────────────────────────────────────────


def test_reset_removes_the_row_and_config_toml_applies(tmp_path):
    cfg = write_config(tmp_path, '[apply]\neffort = "low"\n')
    client = make_client(settings_from(cfg))
    page = client.get("/models").text
    save(client, "drafting", page, effort="high")
    page = client.get("/models").text
    assert "config.toml says low, /models overrides it" in unescape(page)
    r = client.post(
        "/models/drafting/reset", data={"digest": digest_of(page)}, headers={"HX-Request": "1"}
    )
    assert r.status_code == 200 and "Reset at" in r.text
    _e, _d, data, _p = read_models_file(models_path(client))
    assert data == {}
    assert client.app.state.settings.apply.effort == "low"


def test_revert_writes_the_old_value_as_a_new_change(client):
    page = client.get("/models").text
    save(client, "drafting", page, effort="high")
    conn = client.app.state.conn_factory()
    (cid,) = conn.execute("SELECT id FROM model_change").fetchone()
    page = client.get("/models").text
    assert "apply.effort: (not set) → high" in unescape(page)
    r = client.post(
        f"/models/history/{cid}/revert",
        data={"digest": digest_of(page)},
        headers={"HX-Request": "1"},
    )
    assert r.status_code == 200, r.text
    assert "Reverted at" in r.text
    rows = conn.execute("SELECT field, old_value, new_value, source FROM model_change").fetchall()
    assert tuple(rows[-1]) == ("apply.effort", "high", None, "revert")
    assert client.app.state.settings.apply.effort == "medium"


def test_load_takes_an_outside_change(client):
    page = client.get("/models").text
    assert "changed outside the console" not in page
    models_path(client).write_text('[scoring]\ndeep_scorer = "anthropic:claude-sonnet-5-5"\n')
    page = client.get("/models").text
    assert "changed outside the console" in page
    # The running settings are untouched until Load.
    assert client.app.state.settings.scoring.deep_scorer == "anthropic:claude-opus-5-5"
    r = client.post("/models/load", data={"page_digest": digest_of(page)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/models?done=loaded"
    assert client.app.state.settings.scoring.deep_scorer == "anthropic:claude-sonnet-5-5"
    assert "changed outside the console" not in client.get("/models").text


def test_reset_file_replaces_a_broken_file(client):
    path = models_path(client)
    path.write_text("this is [not toml\n")
    page = client.get("/models").text
    assert "not valid TOML" in page and "Reset file" in page
    r = client.post(
        "/models/reset-file", data={"page_digest": digest_of(page)}, follow_redirects=False
    )
    assert r.headers["location"] == "/models?done=reset-file"
    assert read_models_file(path)[2] == {}
    assert list(path.parent.glob("models.toml.broken-*"))


def test_reset_file_refuses_a_valid_file(client):
    page = client.get("/models").text
    save(client, "drafting", page, effort="low")
    page = client.get("/models").text
    r = client.post(
        "/models/reset-file", data={"page_digest": digest_of(page)}, follow_redirects=False
    )
    assert r.headers["location"] == "/models?done=not-broken"
    assert read_models_file(models_path(client))[2] == {"apply": {"effort": "low"}}


# ─── Queued re-scores ───────────────────────────────────────────────────────


def queue_rescore(client: TestClient) -> None:
    conn = client.app.state.conn_factory()
    with conn:
        conn.execute(
            "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
            "cost_per_job_usd, estimated_usd) VALUES ('2026-10-10T01:21:00Z', 'recent', 'v', "
            "3, 0.002, 0.006)"
        )


def test_screen_change_asks_about_queued_rescores_and_keep_pins_them(client):
    queue_rescore(client)
    page = client.get("/models").text
    preview = client.post(
        "/models/screen/preview",
        data={"screen": "jev:typesafe/jev-1.13", "shown_screen": "anthropic:claude-haiku-5-5"},
    )
    assert "queued re-score" in preview.text
    assert "keep queued re-scores on anthropic:claude-haiku-5-5" in preview.text
    r = save(client, "screen", page, screen="jev:typesafe/jev-1.13")
    assert "choose what queued re-scores use" in r.text
    assert not models_path(client).exists()
    r = save(client, "screen", page, screen="jev:typesafe/jev-1.13", queued="keep")
    assert r.status_code == 200 and "Saved at" in r.text
    conn = client.app.state.conn_factory()
    assert conn.execute("SELECT scorer FROM rescore_request").fetchone()[0] == (
        "anthropic:claude-haiku-5-5"
    )
    assert client.app.state.settings.scoring.screen_scorer == "jev:typesafe/jev-1.13"


def hidden(text: str, name: str) -> str | None:
    m = re.search(rf'name="{name}" value="([^"]*)"', text)
    return unescape(m.group(1)) if m else None


def test_save_anyway_approves_only_the_value_it_was_shown(client):
    """Review blocker: an approval carried through the queued question must not apply to a
    value edited afterwards (a dearer slug would save with no prompt)."""
    queue_rescore(client)
    page = client.get("/models").text
    first = "openrouter:acme/unlisted-1"
    r = save(client, "screen", page, screen_other=first)
    assert "Save anyway" in r.text
    assert hidden(r.text, "force_for_screen") == first
    # Save anyway: the approval matches, so the queued question comes next, carrying it.
    r = save(client, "screen", page, screen_other=first, force_for_screen=first)
    assert "choose what queued re-scores use" in r.text
    assert hidden(r.text, "force_for_screen") == first
    # The value is edited before the queued button is clicked: the approval does not move.
    other = "openrouter:acme/unlisted-2"
    r = save(client, "screen", page, screen_other=other, force_for_screen=first, queued="new")
    assert "Save anyway" in r.text and "Saved at" not in r.text
    assert not models_path(client).exists()
    assert hidden(r.text, "force_for_screen") == other
    r = save(client, "screen", page, screen_other=first, force_for_screen=first, queued="new")
    assert "Saved at" in r.text
    assert client.app.state.settings.scoring.screen_scorer == first


def test_a_bare_force_flag_approves_nothing(client):
    page = client.get("/models").text
    r = save(client, "screen", page, screen_other="openrouter:acme/unlisted-1", force="1")
    assert "Save anyway" in r.text
    assert not models_path(client).exists()


def test_409_keeps_the_old_digest_so_a_second_click_is_refused_too(client):
    page = client.get("/models").text
    old = digest_of(page)
    models_path(client).write_text('[apply]\neffort = "low"\n')  # another writer
    r = save(client, "drafting", page, effort="high")
    assert r.status_code == 409
    assert 'id="models-digest" name="digest"' not in r.text  # no new digest handed out
    assert hidden(r.text, "page_digest") == old
    r = save(client, "drafting", page, effort="high")
    assert r.status_code == 409
    assert models_path(client).read_text() == '[apply]\neffort = "low"\n'


def test_a_no_change_save_after_a_409_hands_out_no_fresh_digest(client):
    """Only a write moves the page's digest: a no-op save (or any refusal) after another
    writer's change must not refresh it, or the next real save would overwrite that change."""
    page = client.get("/models").text
    old = digest_of(page)
    models_path(client).write_text('[apply]\neffort = "low"\n')  # another writer
    assert save(client, "drafting", page, effort="high").status_code == 409
    r = save(client, "screen", page)  # nothing changed on this row
    assert r.status_code == 200 and "No change" in r.text
    assert 'id="models-digest" name="digest"' not in r.text
    assert hidden(r.text, "page_digest") == old
    r = save(client, "deep", page, screen_other="", deep="anthropic:claude-sonnet-5-5")
    assert r.status_code == 409
    r = client.post("/models/screen", data={"digest": old, "screen": f"openrouter:{SENTINEL}"})
    assert r.status_code == 422 and digest_of(r.text) == old  # no-JS page keeps it too
    assert models_path(client).read_text() == '[apply]\neffort = "low"\n'


def test_no_js_409_does_not_claim_the_page_was_reloaded(client):
    page = client.get("/models").text
    models_path(client).write_text('[apply]\neffort = "low"\n')
    data = {"digest": digest_of(page), "effort": "high", "shown_effort": "medium"}
    r = client.post("/models/drafting", data=data)
    assert r.status_code == 409
    text = unescape(r.text)
    assert "reloaded" not in text and "Reload the page" in text


def test_a_models_save_is_not_an_outside_change_to_the_config_watch(cfg):
    from jobhunter import config_watch as cw

    client = make_client(settings_from(cfg))
    watch = cw.ConfigWatcher(
        config_path=lambda: cfg, env_candidates=lambda: [], container_mode=False, start_env={}
    )
    client.app.state.config_watch = watch
    assert not watch.check(client.app.state.settings, force=True).pending
    page = client.get("/models").text
    assert "Saved at" in save(client, "deep", page, deep="anthropic:claude-sonnet-5-5").text
    state = watch.check(client.app.state.settings, force=True)
    assert not state.pending, state.fields
    # An edit made outside the console still shows.
    models_path(client).write_text('[apply]\neffort = "low"\n')
    assert watch.check(client.app.state.settings, force=True).pending


def test_malformed_config_value_is_shown_with_its_problem_not_as_blocked(tmp_path):
    cfg = write_config(tmp_path, '[scoring]\nscreen_scorer = "openroute:jev:tiny"\n')
    client = make_client(settings_from(cfg))
    text = client.get("/models").text
    assert selected(text, "screen") == "openroute:jev:tiny"
    screen = unescape(row_html(text, "screen"))
    assert "Blocked: the value in models.toml is invalid" not in screen
    assert "config.toml" in screen and 'class="mo-blocked"' in screen


def test_key_shaped_free_text_is_never_echoed_or_saved(client):
    page = client.get("/models").text
    r = client.post("/models/screen/preview", data={"screen_other": f"openrouter:{SENTINEL}"})
    assert SENTINEL not in r.text and "not a usable model name" in r.text
    r = save(client, "screen", page, screen_other=f"openrouter:{SENTINEL}")
    assert r.status_code == 422 and SENTINEL not in r.text
    assert not models_path(client).exists()


def test_keep_pins_queued_rescores_to_the_on_disk_value(client):
    """While a Load is pending, tonight's drain plans with models.toml on disk, so "keep"
    must pin that value, not this console's running one."""
    queue_rescore(client)
    models_path(client).write_text('[scoring]\nscreen_scorer = "jev:typesafe/jev-1.13"\n')
    page = client.get("/models").text
    assert "changed outside the console" in page
    r = save(client, "screen", page, screen_other="local:qwen3-4b", queued="keep")
    assert "Saved at" in r.text, r.text
    conn = client.app.state.conn_factory()
    assert conn.execute("SELECT scorer FROM rescore_request").fetchone()[0] == (
        "jev:typesafe/jev-1.13"
    )


def test_keep_pins_nothing_when_the_write_is_refused(client):
    queue_rescore(client)
    page = client.get("/models").text
    models_path(client).write_text('[apply]\neffort = "low"\n')
    r = save(client, "screen", page, screen="jev:typesafe/jev-1.13", queued="keep")
    assert r.status_code == 409
    conn = client.app.state.conn_factory()
    assert conn.execute("SELECT scorer FROM rescore_request").fetchone()[0] is None


def test_reset_file_is_recorded_in_history(client):
    path = models_path(client)
    path.write_text("this is [not toml\n")
    page = client.get("/models").text
    client.post("/models/reset-file", data={"page_digest": digest_of(page)})
    conn = client.app.state.conn_factory()
    rows = conn.execute("SELECT field, old_value, new_value, source FROM model_change").fetchall()
    assert ("scoring.screen_scorer", None, "anthropic:claude-haiku-5-5", "reset") in [
        tuple(r) for r in rows
    ]


def test_queued_new_leaves_the_requests_unpinned(client):
    queue_rescore(client)
    page = client.get("/models").text
    r = save(client, "screen", page, screen="jev:typesafe/jev-1.13", queued="new")
    assert "Saved at" in r.text
    conn = client.app.state.conn_factory()
    assert conn.execute("SELECT scorer FROM rescore_request").fetchone()[0] is None


# ─── Gates ──────────────────────────────────────────────────────────────────

WRITES = [
    ("/models/screen", {"screen": "jev:typesafe/jev-1.13"}),
    ("/models/screen/reset", {}),
    ("/models/history/1/revert", {}),
    ("/models/reset-file", {}),
    ("/models/load", {}),
    ("/models/openrouter/refresh", {}),
    ("/models/local/check", {}),
]


@pytest.mark.parametrize(("url", "data"), WRITES)
def test_every_write_needs_a_loopback_peer(cfg, url, data):
    client = make_client(settings_from(cfg), peer=LAN)
    page = client.get("/models").text
    r = client.post(url, data={"digest": digest_of(page), **data}, follow_redirects=False)
    assert r.status_code == 403
    assert not models_path(client).exists()


@pytest.mark.parametrize("url", ["/models/openrouter/refresh", "/models/local/check"])
def test_network_routes_are_refused_under_allow_remote(cfg, url, monkeypatch):
    from jobhunter.scoring import catalog

    monkeypatch.setattr(catalog, "refresh_openrouter_cache", lambda *a, **k: pytest.fail("no"))
    client = make_client(settings_from(cfg), allow_remote=True)
    assert client.post(url, follow_redirects=False).status_code == 403


def test_get_routes_pass_the_middleware_only(cfg):
    client = make_client(settings_from(cfg), peer=LAN)
    assert client.get("/models").status_code == 200
    assert client.post("/models/screen/preview", data={"screen": "x"}).status_code == 200


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [(readiness.HOST, "jobhunter"), (readiness.CONTAINER, "jobhunter-ctr")],
)
def test_non_loopback_peer_sees_read_only_page_with_commands(cfg, mode, prefix):
    client = make_client(settings_from(cfg), peer=LAN, mode=mode)
    text = unescape(client.get("/models").text)
    assert "Read only" in text
    assert 'class="mo-save"' not in text and "Revert</button>" not in text
    assert "Check server (free)" not in text and "Refresh list" not in text
    assert f"{prefix} models set screen anthropic:claude-haiku-5-5" in text
    assert f"{prefix} models reset drafting" in text


def test_cross_site_write_is_refused_by_the_middleware(client):
    page = client.get("/models").text
    r = client.post(
        "/models/drafting",
        data={"digest": digest_of(page), "effort": "low", "shown_effort": "medium"},
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert not models_path(client).exists()


# ─── No leaks, no network ───────────────────────────────────────────────────


def test_an_invalid_value_never_reaches_the_page(tmp_path):
    cfg = write_config(tmp_path, f'[apply]\napi_model = "{SENTINEL}"\n')
    client = make_client(settings_from(cfg))
    models_path(client).write_text(
        f'[scoring]\nscreen_scorer = "{SENTINEL}"\n[apply]\ncli_model = "{SENTINEL}"\n'
    )
    client.app.state.settings = load_settings(cfg)  # as a console started with this file
    page = client.get("/models").text
    assert SENTINEL not in page
    assert "Blocked" in page  # the screening row says it is blocked, without the value
    for row in mr.ROWS:
        params = {"shown_screen": "", "screen": "anthropic:claude-haiku-5-5"}
        assert SENTINEL not in client.post(f"/models/{row}/preview", data=params).text
    r = save(client, "drafting", page, effort="low")
    assert r.status_code == 200 and SENTINEL not in r.text
    r = save(client, "screen", client.get("/models").text, screen="anthropic:claude-haiku-5-5")
    assert SENTINEL not in r.text
    assert client.app.state.settings.scoring.screen_scorer == "anthropic:claude-haiku-5-5"


def test_no_network_on_render_preview_save_reset_revert(client, _no_network_no_models, tmp_path):
    # A cache over a day old: a stray OpenRouterScorer.catalog() would try to fetch.
    cache = Path(client.app.state.settings.scoring.openrouter.catalog_cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fetched_at": 1.0, "prices": {"acme/tiny-1": [0.01, 0.02]}}))
    page = client.get("/models").text
    for row, params in [
        ("screen", {"screen": "openrouter:acme/tiny-1"}),
        ("screen", {"screen": "jev:typesafe/jev-1.13"}),
        ("deep", {"deep": "anthropic:claude-sonnet-5-5"}),
        ("drafting", {"runner": "api", "draft_api": "claude-sonnet-5-5"}),
        ("check", {"entailment_check": "true", "check_api": "claude-sonnet-5-5"}),
    ]:
        assert client.post(f"/models/{row}/preview", data=params).status_code == 200
    tiny = "openrouter:acme/tiny-1"
    save(client, "screen", page, screen=tiny, force_for_screen=tiny)
    page = client.get("/models").text
    client.post("/models/screen/reset", data={"digest": digest_of(page)})
    page = client.get("/models").text
    conn = client.app.state.conn_factory()
    (cid,) = conn.execute("SELECT max(id) FROM model_change").fetchone()
    client.post(f"/models/history/{cid}/revert", data={"digest": digest_of(page)})
    assert _no_network_no_models == []
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0


def test_local_check_shows_fixed_text_and_escaped_capped_ids(client, caplog):
    secret_detail = "SENTINEL-detail-http://user:pw@127.0.0.1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            ids = [{"id": f"<b>m{i}</b>" + "x" * 300} for i in range(60)]
            return httpx.Response(200, json={"data": ids})
        return httpx.Response(404)

    client.app.state.local_check_client = lambda: httpx.Client(
        transport=httpx.MockTransport(handler)
    )
    r = client.post("/models/local/check", headers={"HX-Request": "1"})
    assert r.status_code == 200
    assert "reachable" in r.text and "<b>" not in r.text and "&lt;b&gt;m0" in r.text
    assert r.text.count('name="local_id"') == 50
    assert "x" * 201 not in r.text

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(secret_detail)

    client.app.state.local_check_client = lambda: httpx.Client(transport=httpx.MockTransport(down))
    r = client.post("/models/local/check", headers={"HX-Request": "1"})
    assert "not reachable" in r.text and "SENTINEL" not in r.text
    assert "SENTINEL" not in caplog.text


def test_local_check_unauthorized_asks_for_a_key(client):
    client.app.state.local_check_client = lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    r = client.post("/models/local/check", headers={"HX-Request": "1"})
    assert "asks for a key" in r.text


def test_openrouter_refresh_runs_only_on_click(client, monkeypatch):
    from jobhunter.scoring import catalog

    calls = []
    monkeypatch.setattr(catalog, "refresh_openrouter_cache", lambda cfg: calls.append(cfg))
    client.get("/models")
    assert calls == []
    r = client.post("/models/openrouter/refresh", follow_redirects=False)
    assert r.headers["location"] == "/models?done=refresh-failed" and len(calls) == 1


# ─── Banner and access ──────────────────────────────────────────────────────


def test_banner_names_a_missing_anthropic_credential(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    client.app.state.setup_environ = {"OPENROUTER_API_KEY": "placeholder"}
    client.app.state.setup_home = Path(client.app.state.settings.paths.data_dir)
    text = unescape(client.get("/models").text)
    assert "No Anthropic API credential found" in text
    assert "Screening and the deep pass cannot run" in text
    assert '<a href="/help/scorers">How to fix it</a>' in text


# ─── 020 M5b: the price line says what the cap holds ────────────────────────


def test_screen_price_line_shows_the_hold_when_it_is_above_the_estimate(client):
    row = unescape(row_html(client.get("/models").text, "screen"))
    # Haiku 5.5 at about $0.26 per 1,000; the cap holds the $0.002 floor.
    assert "About $0.26 per 1,000 jobs (price table)." in row
    assert "While a run is in flight the spend cap holds $0.002 a job ($2.00 per 1,000)" in row
    assert "never passes" not in row  # no overclaim (review B1)


def test_screen_price_line_for_a_new_model_shows_its_cold_hold(tmp_path):
    cfg = write_config(tmp_path, '[scoring]\nscreen_scorer = "anthropic:claude-sonnet-5-5"\n')
    row = unescape(row_html(make_client(settings_from(cfg)).get("/models").text, "screen"))
    assert "About $5.07 per 1,000 jobs (price table)." in row
    # No history: its first batch is held with the cached prefix as cache writes (B1).
    held = re.search(r"the spend cap holds \$([0-9.]+) a job", row)
    assert held and float(held.group(1)) > 0.00507
    assert "not offered" not in row  # offered for screening now


def test_screen_price_line_has_no_hold_when_it_equals_the_estimate(tmp_path):
    cfg = write_config(tmp_path, '[scoring]\nscreen_scorer = "anthropic:claude-sonnet-5-5"\n')
    client = make_client(settings_from(cfg))
    conn = db.connect(client.app.state.settings.paths.db_path)
    with conn:
        for gid in (1, 2):
            conn.execute(
                "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
                "VALUES (?, NULL, 'exact_hash', 'x')",
                (gid,),
            )
            conn.execute(
                "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, "
                "scoring_version, verdict, overall, dimensions, evidence, cost_usd, created_at) "
                "VALUES (?, 'screen', 'anthropic:claude-sonnet-5-5', 'p', 'v', 'weak', 10, "
                "'{}', '[]', 0.03, 'x')",
                (gid,),
            )
    conn.close()
    row = unescape(row_html(client.get("/models").text, "screen"))
    # Measured above the no-cache-reuse floor (about $0.012), so that is what the cap holds.
    assert "About $30.00 per 1,000 jobs (measured over 2 jobs)." in row
    assert "the spend cap holds" not in row  # measured, and that is what the cap holds
