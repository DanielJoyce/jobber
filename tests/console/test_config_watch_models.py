"""Config watch on the models.toml layers (specs/020 "Config watch", ticket M8): an overridden
config.toml edit is "no effect: set on /models", Details shows each model key layer by layer, an
outside models.toml change has one flow (the watch's Apply; /models links to it), a models.toml
problem is a validation error that keeps the running settings, and a blocked use stays blocked
after Apply or Load. Apply, Load and the /models writes serialize on ``settings_lock``."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_models_page import (  # noqa: F401  (autouse fixtures, helpers)
    _fresh,
    _no_network_no_models,
    save,
)

from jobhunter import config
from jobhunter import config_watch as cw
from jobhunter.config import load_settings
from jobhunter.console.app import create_app
from jobhunter.core import db

NOW = datetime(2026, 10, 11, 14, 5, tzinfo=UTC)
LOOPBACK = ("127.0.0.1", 50000)
SENTINEL = "zqxSENTINELqxz"
HAIKU = "anthropic:claude-haiku-5-5"
SONNET = "anthropic:claude-sonnet-5-5"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Console:
    """A console started with ``config.toml`` and ``models.toml``, watching both."""

    def __init__(self, tmp: Path, *, config_extra: str = "", models: str = "", watch=True):
        self.tmp = tmp
        self.data = tmp / "data"
        self.data.mkdir()
        (tmp / "profile").mkdir()
        self.cfg = tmp / "config.toml"
        self.base = (
            f'[paths]\ndata_dir = "{self.data}"\ncache_dir = "{tmp / "cache"}"\n'
            f'profile_dir = "{tmp / "profile"}"\n'
        )
        self.cfg.write_text(self.base + config_extra)
        if models:
            self.models.write_text(models)
        running = load_settings(self.cfg)
        db_path = running.paths.db_path
        conn = db.connect(db_path)
        db.migrate(conn)
        conn.close()
        self.clock = Clock()
        self.app = create_app(running, lambda: db.connect(db_path), clock=lambda: NOW)
        self.app.state.setup_mode = "host"
        self.watch = None
        if watch:
            self.watch = cw.ConfigWatcher(
                config_path=lambda: self.cfg, env_candidates=lambda: [], clock=self.clock,
                container_mode=False, start_env={},
            )  # fmt: skip
            self.app.state.config_watch = self.watch

    @property
    def models(self) -> Path:
        return self.data / "models.toml"

    @property
    def settings(self) -> config.Settings:
        return self.app.state.settings

    def client(self) -> TestClient:
        return TestClient(self.app, client=LOOPBACK)

    def edit_config(self, extra: str) -> None:
        self.cfg.write_text(self.base + extra)
        self.clock.t += 3

    def edit_models(self, text: str) -> None:
        self.models.write_text(text)
        self.clock.t += 3

    def state(self) -> cw.State:
        assert self.watch is not None
        return self.watch.check(self.settings, force=True)


def apply(client: TestClient, console: Console):
    return client.post("/config/apply", data={"key": console.state().key}, follow_redirects=False)


def layer(page: str, field: str) -> list[str]:
    """The cells of the layer table's row for ``field``."""
    import re

    table = re.search(r'<table class="pg-table" id="cfg-layers">(.*?)</table>', page, re.S)
    assert table, "Details has the layer table"
    for row in re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S):
        if f"<code>{field}</code>" in row:
            cells = re.findall(r"<td>(.*?)</td>", row, re.S)
            return [unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
    raise AssertionError(f"no layer row for {field}")


# ─── 1. An overridden config.toml edit ───────────────────────────────────────


def test_config_edit_to_a_key_models_sets_is_no_effect_with_a_link(tmp_path):
    c = Console(tmp_path, models='[scoring]\nscreen_scorer = "local:qwen3"\n')
    client = c.client()
    assert not c.state().pending
    c.edit_config(f'[scoring]\nscreen_scorer = "{SONNET}"\n')
    bar = client.get("/activity").text
    assert "config.toml changed: 1 setting (no effect: set on /models)." in bar
    assert 'href="/models#models-row-screen"' in bar
    assert "Apply now" not in bar and "Restart needed" not in bar
    page = client.get("/config/changes").text
    assert "no effect: set on" in page and 'href="/models#models-row-screen"' in page
    assert "Apply now" not in page
    (change,) = c.state().fields
    assert change.cls == cw.NO_EFFECT and change.set_on == cw.SET_ON_MODELS
    assert change.source == "config.toml" and change.new == repr(SONNET)
    # Details, layer by layer: default, config.toml, models.toml, running, from the files now.
    assert layer(page, "scoring.screen_scorer")[1:] == [
        HAIKU, SONNET, "local:qwen3", "local:qwen3", "local:qwen3",
    ]  # fmt: skip
    assert c.settings.scoring.screen_scorer == "local:qwen3"


def test_a_blocked_key_overrides_a_config_edit_too(tmp_path):
    """A spending key that models.toml blocks stays blocked: the config.toml value under it is
    not offered as a way round the block."""
    c = Console(tmp_path, models='[scoring]\nscreen_scorer = "nonsense:x"\n')
    assert "screen_scorer" in c.settings.scoring.scorer_problems()
    c.edit_config(f'[scoring]\nscreen_scorer = "{SONNET}"\n')
    state = c.state()
    (change,) = state.fields
    assert change.cls == cw.NO_EFFECT and change.set_on == cw.SET_ON_MODELS
    assert not state.can_apply and not state.models_errors  # the console started blocked
    page = c.client().get("/config/changes").text
    assert layer(page, "scoring.screen_scorer")[3:] == [
        "invalid (blocks this use)", "blocked", "blocked",
    ]  # fmt: skip


def test_mixed_change_applies_the_rest_and_keeps_the_models_value(tmp_path):
    c = Console(tmp_path, models='[apply]\neffort = "low"\n')
    client = c.client()
    c.edit_config('[apply]\neffort = "high"\n[scoring]\nweekly_cap_usd = 12.0\n')
    bar = client.get("/activity").text
    assert "config.toml changed: 2 settings; 1 with no effect (set on /models)." in bar
    assert 'href="/models#models-row-drafting"' in bar
    assert apply(client, c).status_code == 303
    assert c.settings.scoring.weekly_cap_usd == 12.0
    assert c.settings.apply.effort == "low"
    # The config.toml layer is now the running one: nothing is left to show.
    assert not c.state().pending
    # Reset on /models now gives config.toml's new value, not the one from start.
    page = client.get("/models").text
    r = client.post("/models/drafting/reset", data={"digest": _digest(page)},
                    headers={"HX-Request": "true"})  # fmt: skip
    assert r.status_code == 200, r.text
    assert c.settings.apply.effort == "high"


def _digest(page: str) -> str:
    import re

    m = re.search(r'id="models-digest" name="digest" value="([0-9a-f]*)"', page)
    assert m
    return m.group(1)


def test_key_shaped_values_are_never_rendered(tmp_path, caplog):
    c = Console(tmp_path, models='[scoring]\nscreen_scorer = "local:qwen3"\n')
    client = c.client()
    caplog.set_level(logging.DEBUG)
    c.edit_config(f'[scoring]\nscreen_scorer = "openrouter:sk-or-{SENTINEL}"\n')
    c.edit_models(
        f'[scoring]\nscreen_scorer = "local:qwen3"\n[apply]\napi_model = "sk-ant-{SENTINEL}"\n'
    )
    bar = client.get("/activity").text
    page = client.get("/config/changes").text
    models_page = client.get("/models").text
    for text in (bar, page, models_page, caplog.text):
        assert SENTINEL not in text
    row = layer(page, "scoring.screen_scorer")
    assert row[2] == "(not shown)"


# ─── 2. Layer by layer on Details ────────────────────────────────────────────


def test_details_lists_each_model_layer_for_an_outside_models_change(tmp_path):
    c = Console(tmp_path, config_extra=f'[scoring]\ndeep_scorer = "{SONNET}"\n')
    client = c.client()
    c.edit_models('[scoring]\ndeep_scorer = "anthropic:claude-opus-5-5"\n[apply]\nrunner = "api"\n')
    page = client.get("/config/changes").text
    assert layer(page, "scoring.deep_scorer")[1:] == [
        "anthropic:claude-opus-5-5", SONNET, "anthropic:claude-opus-5-5", SONNET,
        "anthropic:claude-opus-5-5",
    ]  # fmt: skip
    assert layer(page, "apply.runner")[1:] == ["cli", "not set", "api", "cli", "api"]
    # A model key nothing changed is not listed.
    assert "apply.effort" not in page


def test_layer_rows_never_show_an_invalid_models_value(tmp_path):
    c = Console(tmp_path)
    c.edit_models(f'[apply]\napi_model = "{SENTINEL} with spaces"\n')
    layers = config.load_layers(c.cfg)
    (row,) = cw.layer_rows(c.settings, layers, ["apply.api_model"])
    assert row.models_toml == "invalid (ignored)"
    assert SENTINEL not in repr(row)


# ─── 3. One flow for an outside models.toml change ───────────────────────────


def test_outside_models_change_is_applied_by_the_watch_and_models_links_to_it(tmp_path):
    c = Console(tmp_path)
    client = c.client()
    c.edit_models(f'[scoring]\ndeep_scorer = "{SONNET}"\n')
    bar = client.get("/activity").text
    assert "models.toml changed: 1 setting." in bar and "Apply now" in bar
    page = client.get("/models").text
    assert "changed outside the console" in page
    assert 'href="/config/changes"' in page and "press Apply now there" in page
    assert 'action="/models/load"' not in page  # no second button for the same change
    # A Load posted anyway (an old tab) writes nothing and sends you to the one flow.
    r = client.post("/models/load", data={"page_digest": _digest(page)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/config/changes?flash=models"
    assert c.settings.scoring.deep_scorer == "anthropic:claude-opus-5-5"
    assert "applied here" in client.get("/config/changes?flash=models").text
    assert apply(client, c).status_code == 303
    assert c.settings.scoring.deep_scorer == SONNET
    assert c.settings.models_layers.source("scoring", "deep_scorer") == "models.toml"
    assert "changed outside the console" not in client.get("/models").text
    assert not c.state().pending


def test_without_the_watch_models_keeps_its_load(tmp_path):
    c = Console(tmp_path, watch=False)
    client = c.client()
    c.edit_models(f'[scoring]\ndeep_scorer = "{SONNET}"\n')
    page = client.get("/models").text
    assert 'action="/models/load"' in page and 'href="/config/changes"' not in page


# ─── 4. models.toml problems, fail closed ────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ['[scoring]\nscreen_scorer = "nonsense:x"\n', "this is [not toml\n"],
    ids=["invalid-key", "broken-file"],
)
def test_a_new_models_problem_is_an_error_and_the_running_settings_stay(tmp_path, text):
    c = Console(tmp_path, models='[scoring]\nscreen_scorer = "local:qwen3"\n')
    client = c.client()
    c.edit_config("[scoring]\nweekly_cap_usd = 12.0\n")
    c.edit_models(text)
    state = c.state()
    assert state.models_errors and not state.can_apply
    bar = client.get("/activity").text
    assert "models.toml has errors; the console keeps the settings from" in bar
    assert "Apply now" not in bar
    assert 'id="cfg-models-errors"' in client.get("/config/changes").text
    r = client.post("/config/apply", data={"key": state.key}, follow_redirects=False)
    assert r.headers["location"] == "/config/changes?flash=nothing"
    assert c.settings.scoring.screen_scorer == "local:qwen3"
    assert c.settings.scoring.weekly_cap_usd == 10.0
    # The nightly run reads the file itself and is blocked: fail closed there.
    assert "screen_scorer" in load_settings(c.cfg).scoring.scorer_problems()


def test_a_blocked_use_stays_blocked_after_apply(tmp_path):
    c = Console(tmp_path, models='[scoring]\nscreen_scorer = "nonsense:x"\n')
    client = c.client()
    assert "screen_scorer" in c.settings.scoring.scorer_problems()
    c.edit_config(f'[scoring]\nweekly_cap_usd = 12.0\nscreen_scorer = "{SONNET}"\n')
    assert apply(client, c).status_code == 303
    assert c.settings.scoring.weekly_cap_usd == 12.0
    assert c.settings.scoring.screen_scorer == config.BLOCKED_SPEC
    assert "screen_scorer" in c.settings.scoring.scorer_problems()
    # Apply again after an unrelated models.toml edit: still blocked.
    c.edit_models('[scoring]\nscreen_scorer = "nonsense:x"\n[apply]\neffort = "low"\n')
    assert apply(client, c).status_code == 303
    assert c.settings.apply.effort == "low"
    assert "screen_scorer" in c.settings.scoring.scorer_problems()


def test_load_of_a_blocked_file_blocks_the_running_use(tmp_path):
    c = Console(tmp_path, watch=False)
    client = c.client()
    c.edit_models('[scoring]\nscreen_scorer = "nonsense:x"\n')
    page = client.get("/models").text
    r = client.post("/models/load", data={"page_digest": _digest(page)}, follow_redirects=False)
    assert r.headers["location"] == "/models?done=loaded"
    assert "screen_scorer" in c.settings.scoring.scorer_problems()
    assert c.settings.scoring.screen_scorer == config.BLOCKED_SPEC


# ─── Racing under settings_lock ──────────────────────────────────────────────


class CountingLock:
    """``settings_lock`` that counts the requests waiting for it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting = 0
        self.entered = 0
        self._count = threading.Lock()

    def __enter__(self):
        with self._count:
            self.waiting += 1
        self.lock.acquire()
        with self._count:
            self.entered += 1
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False

    # The /activity poll's try-lock: not counted as a waiter.
    def acquire(self, blocking: bool = True) -> bool:
        return self.lock.acquire(blocking)

    def release(self) -> None:
        self.lock.release()


def _race(c: Console, *posts) -> list:
    """Hold the lock, start every post in its own thread, wait until each waits on the lock,
    then let them through one at a time. Daemon threads, and the lock is always released, so
    a broken lock fails the test instead of hanging it."""
    lock = CountingLock()
    c.app.state.settings_lock = lock
    results: list = [None] * len(posts)

    def run(i, post):
        results[i] = post(TestClient(c.app, client=LOOPBACK))

    threads = [threading.Thread(target=run, args=(i, p), daemon=True) for i, p in enumerate(posts)]
    lock.lock.acquire()
    try:
        for t in threads:
            t.start()
        deadline = time.monotonic() + 10
        while lock.waiting < len(posts) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lock.waiting == len(posts), "every request takes settings_lock"
    finally:
        lock.lock.release()
    for t in threads:
        t.join(10)
    return results


def test_apply_and_load_race_without_a_double_apply(tmp_path):
    """While Apply holds the lock, a Load posted from /models neither waits for nor repeats
    it: Load defers to the watch, and the running settings are assigned once."""
    c = Console(tmp_path)
    client = c.client()
    c.edit_models(f'[scoring]\ndeep_scorer = "{SONNET}"\n')
    c.edit_config("[scoring]\nweekly_cap_usd = 12.0\n")
    key = c.state().key
    digest = _digest(client.get("/models").text)
    lock = CountingLock()
    c.app.state.settings_lock = lock
    results: dict[str, object] = {}

    def post_apply():
        results["apply"] = c.client().post(
            "/config/apply", data={"key": key}, follow_redirects=False
        )

    def post_load():
        results["load"] = c.client().post(
            "/models/load", data={"page_digest": digest}, follow_redirects=False
        )

    ta = threading.Thread(target=post_apply, daemon=True)
    tl = threading.Thread(target=post_load, daemon=True)
    lock.lock.acquire()
    try:
        ta.start()
        deadline = time.monotonic() + 10
        while lock.waiting < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        tl.start()
        tl.join(10)
        assert "load" in results, "Load waited on settings_lock behind Apply"
        assert results["load"].headers["location"] == "/config/changes?flash=models"
        assert c.settings.scoring.deep_scorer == "anthropic:claude-opus-5-5"
    finally:
        lock.lock.release()
    ta.join(10)
    assert results["apply"].status_code == 303
    assert lock.entered == 1  # one read-modify-assign of the running settings
    assert c.settings.scoring.deep_scorer == SONNET
    assert c.settings.scoring.weekly_cap_usd == 12.0
    assert not c.state().pending


def test_apply_and_a_models_save_race_and_keep_both_changes(tmp_path):
    """Save and Apply each read, modify and assign the running settings. Under the shared lock
    neither loses the other's change."""
    c = Console(tmp_path)
    client = c.client()
    c.edit_models('[apply]\neffort = "low"\n')  # outside: the watch's to apply
    c.edit_config("[scoring]\nweekly_cap_usd = 12.0\n")
    key = c.state().key
    page = client.get("/models").text

    results = _race(
        c,
        lambda cl: cl.post("/config/apply", data={"key": key}, follow_redirects=False),
        lambda cl: save(cl, "deep", page, deep=SONNET),
    )
    assert results[0].status_code == 303
    assert results[0].headers["location"] == "/config/changes", "Apply was not refused"
    assert results[1].status_code == 200 and "Saved at" in results[1].text, results[1].text
    s = c.settings
    assert s.scoring.deep_scorer == SONNET  # the Save
    assert s.scoring.weekly_cap_usd == 12.0  # Apply: config.toml
    assert s.apply.effort == "low"  # Apply: the outside models.toml change
    assert not c.state().pending, c.state().fields


def test_a_models_save_does_not_move_a_pending_apply_or_a_dismissal(tmp_path):
    """The console's own write is already running: it neither makes Apply answer "changed
    again" nor brings back a dismissed notice (the self-write baseline)."""
    c = Console(tmp_path)
    client = c.client()
    c.edit_models('[apply]\neffort = "low"\n')
    key = c.state().key
    c.watch.dismiss(key)
    page = client.get("/models").text
    assert "Saved at" in save(client, "deep", page, deep=SONNET).text
    c.clock.t += 3
    state = c.state()
    assert state.key == key and c.watch.is_dismissed(state)
    r = client.post("/config/apply", data={"key": key}, follow_redirects=False)
    assert r.headers["location"] == "/config/changes"
    assert c.settings.apply.effort == "low" and c.settings.scoring.deep_scorer == SONNET


# ─── Review round: the poll during a save, wording, unreadable files, mutation survivors ────


def test_the_poll_skips_the_check_while_settings_lock_is_held(tmp_path):
    """A save holds the lock between writing models.toml and assigning the running settings;
    a poll in that window must not report the console's own write as an outside change."""
    c = Console(tmp_path)
    client = c.client()
    assert "changed" not in client.get("/activity").text
    lock = CountingLock()
    c.app.state.settings_lock = lock
    c.edit_models(f'[scoring]\ndeep_scorer = "{SONNET}"\n')  # as a save mid-way
    with lock:
        assert "models.toml changed" not in client.get("/activity").text
    c.clock.t += 3
    assert "models.toml changed: 1 setting." in client.get("/activity").text


def test_models_banner_does_not_offer_apply_when_apply_is_blocked(tmp_path):
    c = Console(tmp_path)
    client = c.client()
    c.edit_models("this is [not toml\n")
    page = client.get("/models").text
    assert "changed outside the console" in page
    assert "lists errors in the files" in page and "press Apply now" not in page


def test_a_models_file_that_appears_unreadable_is_seen(tmp_path):
    c = Console(tmp_path)
    assert not c.state().pending
    c.models.mkdir()  # exists, but cannot be read as a file
    c.clock.t += 3
    state = c.state()
    assert state.models_errors and state.pending and not state.can_apply


def test_an_error_only_models_change_is_pending(tmp_path):
    """An invalid [apply] key falls back, so the merged settings do not change: the error
    alone must still raise the notice."""
    c = Console(tmp_path)
    client = c.client()
    c.edit_models('[apply]\nrunner = "bogus"\n')
    state = c.state()
    assert not state.fields and state.models_errors and state.pending
    assert "models.toml has errors" in client.get("/activity").text


def test_an_unknown_models_key_does_not_block_apply(tmp_path):
    c = Console(tmp_path)
    c.edit_models(f'[scoring]\ndeep_scorer = "{SONNET}"\nfuture_key = 1\n[future]\nx = 2\n')
    state = c.state()
    assert not state.models_errors and state.can_apply
    assert apply(c.client(), c).status_code == 303
    assert c.settings.scoring.deep_scorer == SONNET


def test_model_rows_match_the_models_page_anchors():
    from jobhunter import model_choices as mc
    from jobhunter.console import models_routes as mr

    expected = {".".join(mc.NAMES[n]): row for n, row in mr.ROW_OF.items()}
    assert expected == cw.MODEL_ROWS
