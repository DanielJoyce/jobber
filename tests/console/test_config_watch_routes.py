"""Config watch in the console (specs/019 Addendum B, S12a): the notice in /activity,
/config/changes, Apply (partial, under the lock, loopback peer only), Dismiss, /setup row 1."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from jobhunter import config
from jobhunter import config_watch as cw
from jobhunter.config import load_settings
from jobhunter.console.app import create_app
from jobhunter.core import db

NOW = datetime(2026, 10, 11, 14, 5, tzinfo=UTC)
SENTINEL = "zqxSENTINELqxz"
LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.168.1.20", 50000)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Console:
    def __init__(self, tmp: Path, *, overrides: tuple[str, ...] = (), allow_remote: bool = False):
        self.tmp = tmp
        self.cfg = tmp / "config.toml"
        self.env_file = tmp / "dot.env"
        self.base = (
            f'[paths]\ndata_dir = "{tmp / "data"}"\ncache_dir = "{tmp / "cache"}"\n'
            f'profile_dir = "{tmp / "profile"}"\n'
        )
        self.cfg.write_text(self.base)
        (tmp / "profile").mkdir()
        self.running = load_settings(self.cfg)
        db_path = self.running.paths.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.connect(db_path)
        db.migrate(conn)
        conn.close()
        self.clock = Clock()
        self.app = create_app(
            self.running,
            lambda: db.connect(db_path),
            clock=lambda: NOW,
            allow_remote=allow_remote,
        )
        self.app.state.setup_mode = "host"
        self.watch = cw.ConfigWatcher(
            config_path=lambda: self.cfg,
            env_candidates=lambda: [self.env_file],
            clock=self.clock,
            container_mode=False,
            cli_overrides=overrides,
            start_env={self.env_file: {}},
        )
        self.app.state.config_watch = self.watch

    def client(self, peer: tuple[str, int] = LOOPBACK) -> TestClient:
        return TestClient(self.app, client=peer)

    def write(self, extra: str) -> None:
        self.cfg.write_text(self.base + extra)
        self.clock.t += 3

    @property
    def settings(self):
        return self.app.state.settings


@pytest.fixture
def console(tmp_path):
    return Console(tmp_path)


def apply(client: TestClient, console: Console, **kw: Any):
    key = console.watch.check(console.settings).key
    return client.post("/config/apply", data={"key": key}, **kw)


# --- every reloadable field changed --------------------------------------------------------------

LITERAL_ALT = {"apply.runner": "api", "apply.effort": "high"}


def changed_value(leaf: str, old: Any) -> Any:
    if leaf in LITERAL_ALT:
        return LITERAL_ALT[leaf]
    if isinstance(old, bool):
        return not old
    if isinstance(old, int):
        return old + 1
    if isinstance(old, float):
        return old + 1.25
    if isinstance(old, Path):
        return f"/tmp/jh-other/{leaf}"
    if isinstance(old, list):
        return ["b.example"]
    if isinstance(old, dict):
        return {"require_parameters": False}
    if leaf == "mail.alerts_address":
        return SENTINEL  # not the placeholder
    return f"{old}-b"


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(f'"{x}"' for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    return f'"{v}"'


def every_reloadable(running, **extra: str) -> str:
    """TOML changing every reloadable leaf; ``extra`` maps "section__key" to a TOML value."""
    tables: dict[str, list[str]] = {}
    for name, value in extra.items():
        section, key = name.split("__")
        tables.setdefault(section, []).append(f"{key} = {value}")
    for leaf in sorted(config.RELOADABLE):
        section, key = leaf.rsplit(".", 1)
        new = changed_value(leaf, config.field_value(running, leaf))
        tables.setdefault(section, []).append(f"{key} = {toml_value(new)}")
    return "\n".join(f"[{s}]\n" + "\n".join(lines) for s, lines in tables.items()) + "\n"


def test_apply_swaps_every_reloadable_field_and_pages_show_it(console):
    client = console.client()
    old = console.running
    assert '"cap_usd":10.0' in client.get("/api/dash/kpis").text.replace(" ", "")
    assert "Set your alerts address first" in client.get("/alerts").text
    console.write(every_reloadable(old, console__port="9999", fetch__global_concurrency="3"))
    bar = client.get("/activity").text
    assert "config.toml changed:" in bar and "Apply now" in bar
    assert "Restart needed" in bar  # console.port and global_concurrency
    r = apply(client, console, follow_redirects=False)
    assert r.status_code == 303
    new = console.settings
    assert new is not old
    for leaf in config.leaf_fields():
        want = changed_value(leaf, config.field_value(old, leaf))
        got = config.field_value(new, leaf)
        if leaf in config.RELOADABLE:
            assert str(got) == str(want) or got == want, leaf
        else:
            assert got == config.field_value(old, leaf), leaf
    # The pages read the swapped object: the dashboard cap (a closure before S12a), /costs'
    # packet cap, /alerts' placeholder warning.
    assert '"cap_usd":11.25' in client.get("/api/dash/kpis").text.replace(" ", "")
    assert "of $2.25 [apply] cap" in client.get("/costs").text
    assert "Set your alerts address first" not in client.get("/alerts").text
    # What is left: the two restart fields.
    console.clock.t += 3
    bar = client.get("/activity").text
    assert "Applied" in bar and "2 settings still need a restart" in bar
    assert "Apply now" not in bar


PREFS = """\
resume_path: resume.md
target_titles: [Systems Engineer]
hard:
  states_allowed: all
  remote_ok: true
narrative:
  want: Synthetic systems work.
"""


def test_rescore_panel_sees_the_applied_scorer(console):
    pdir = console.tmp / "profile"
    (pdir / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (pdir / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    client = console.client()
    assert 'value="anthropic:claude-haiku-5-5" selected' in client.get("/prefs/rescore/panel").text
    console.write('[scoring]\nscreen_scorer = "anthropic:claude-opus-5-5"\n')
    apply(client, console)
    assert console.settings.scoring.screen_scorer == "anthropic:claude-opus-5-5"
    panel = client.get("/prefs/rescore/panel").text
    assert 'value="anthropic:claude-opus-5-5" selected' in panel


def test_port_set_on_the_command_line_has_no_effect(tmp_path):
    console = Console(tmp_path, overrides=("console.port",))
    client = console.client()
    console.write("[console]\nport = 9999\n")
    bar = client.get("/activity").text
    assert "no effect: set on the command line" in bar
    assert "Restart needed" not in bar and "Apply now" not in bar
    page = client.get("/config/changes").text
    assert "no effect: set on the command line" in page


def test_apply_after_another_edit_applies_nothing_stale(console):
    client = console.client()
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    key = console.watch.check(console.settings).key
    console.write("[scoring]\nweekly_cap_usd = 30.0\n")
    r = client.post("/config/apply", data={"key": key}, follow_redirects=False)
    assert r.headers["location"] == "/config/changes?flash=moved"
    assert console.settings.scoring.weekly_cap_usd == 10.0
    assert "config.toml changed again" in client.get(r.headers["location"]).text


def test_htmx_apply_returns_the_fresh_bar(console):
    client = console.client()
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    r = apply(client, console, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="activity"' in r.text
    assert "Applied 1 setting at" in r.text
    assert console.settings.scoring.weekly_cap_usd == 20.0


# --- who may press -----------------------------------------------------------------------------


@pytest.mark.parametrize("allow_remote", [False, True])
def test_non_loopback_peer_cannot_apply_and_sees_no_button(tmp_path, allow_remote):
    console = Console(tmp_path, allow_remote=allow_remote)
    lan = console.client(LAN)
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    bar = lan.get("/activity").text
    assert "config.toml changed" in bar and "Apply now" not in bar
    assert "Apply from the machine running the console" in bar
    assert apply(lan, console).status_code == 403
    assert console.settings.scoring.weekly_cap_usd == 10.0
    # Dismiss needs only the middleware.
    assert lan.post("/config/dismiss", data={"key": "x"}, follow_redirects=False).status_code == 303


def test_cross_site_apply_is_refused_by_the_middleware(console):
    client = console.client()
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    r = apply(client, console, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert console.settings.scoring.weekly_cap_usd == 10.0


# --- dismiss, invalid file, /setup -------------------------------------------------------------


def test_dismiss_hides_until_the_file_changes_again(console):
    client = console.client()
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    key = console.watch.check(console.settings).key
    client.post("/config/dismiss", data={"key": key})
    assert "config.toml changed" not in client.get("/activity").text
    console.write("[scoring]\nweekly_cap_usd = 21.0\n")
    assert "config.toml changed" in client.get("/activity").text


def test_invalid_file_keeps_old_settings_and_breaks_row_1(console):
    client = console.client()
    console.write(f'[scoring]\ndaily_cap_usd = "{SENTINEL}"\n')
    bar = client.get("/activity").text
    assert "config.toml has errors; the console keeps the settings from" in bar
    assert "scoring.daily_cap_usd: Input should be a valid number" in bar
    assert "Apply now" not in bar
    assert console.settings.scoring.daily_cap_usd == 2.0
    setup = client.get("/setup?all=1").text
    assert "Settings file" in setup and "config.toml has 1 error" in setup
    for page in (bar, setup, client.get("/config/changes").text):
        assert SENTINEL not in page


def test_toml_error_shows_line_and_column(console):
    client = console.client()
    console.write(f"[fetch]\nuser_agent_contact = {SENTINEL}\n")
    bar = client.get("/activity").text
    assert "not valid TOML" in bar and "line " in bar and SENTINEL not in bar


def test_setup_note_says_applied_after_apply(console):
    client = console.client()
    assert "Read when the console started" in client.get("/setup?all=1").text
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    apply(client, console)
    page = client.get("/setup?all=1").text
    assert "(applied)" in page  # rows read from config.toml (or its defaults)
    # Secret and environment rows are only read at start: Apply never re-reads them.
    assert "Read when the console started" in page


def test_no_watcher_means_no_notice(tmp_path):
    console = Console(tmp_path)
    console.app.state.config_watch = None
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    client = console.client()
    assert "config.toml" not in client.get("/activity").text
    assert apply(client, console).status_code == 409


# --- no value leaks ----------------------------------------------------------------------------


def test_sentinels_never_reach_activity_changes_or_logs(console, caplog):
    caplog.set_level(logging.DEBUG)
    client = console.client()
    console.write(
        f'[fetch]\nuser_agent_contact = "{SENTINEL}"\n'
        f'[mail]\nalerts_address = "{SENTINEL}"\n'
        f'[capture]\ndisabled_hosts = ["{SENTINEL}.example"]\n'
        f'[scoring.openrouter]\nreferer = "{SENTINEL}"\napi_key_env = "sk-or-{SENTINEL}"\n'
    )
    console.env_file.write_text(
        f"ANTHROPIC_API_KEY={SENTINEL}\nSENTINEL_{SENTINEL.upper()}=1\nsk-ant-{SENTINEL}\n"
    )
    bar = client.get("/activity").text
    details = client.get("/config/changes").text
    apply(client, console)
    console.clock.t += 3
    bar2 = client.get("/activity").text
    details2 = client.get("/config/changes").text
    for text in (bar, details, bar2, details2, caplog.text):
        assert SENTINEL not in text
        assert SENTINEL.upper() not in text
    # Names that pass the rules are shown; the rest are counted.
    assert "ANTHROPIC_API_KEY" in details
    assert "1 other key (names not shown)" in details
    assert "1 line that is not NAME=value" in details
    assert "changed (1 entries, was 0)" in details
    assert "changed (not shown)" in details  # the api_key_env


def test_every_settings_annotation_is_handled_by_the_swap_test():
    # changed_value must produce a different value for every reloadable leaf.
    running = config.Settings()
    for leaf in config.RELOADABLE:
        old = config.field_value(running, leaf)
        assert changed_value(leaf, old) != old, leaf
    assert set(LITERAL_ALT) <= config.RELOADABLE
    assert get_args(config.Apply.model_fields["runner"].annotation)


def test_apply_with_a_pending_env_edit_keeps_the_running_scorer_sections(console):
    client = console.client()
    console.write(
        "[scoring]\nweekly_cap_usd = 20.0\n"
        "[scoring.openai_compat]\nmax_concurrency = 5\n"
        '[scoring.openrouter]\nbase_url = "https://provider-b.example/v1"\n'
    )
    console.env_file.write_text("OPENAI_COMPAT_API_KEY=placeholder\n")
    page = client.get("/config/changes").text
    assert "held with its API key" in page
    apply(client, console)
    assert console.settings.scoring.weekly_cap_usd == 20.0
    assert console.settings.scoring.openai_compat.max_concurrency == 2
    assert console.settings.scoring.openrouter.base_url == "https://openrouter.ai/api/v1"


def test_non_loopback_peer_gets_the_command_for_a_reload_only_change(console):
    lan = console.client(LAN)
    console.write("[scoring]\nweekly_cap_usd = 20.0\n")
    bar = lan.get("/activity").text
    assert "Or restart to pick it up: <code>Ctrl-C and jobhunter console</code>" in bar
    assert "Or restart to pick it up" in lan.get("/config/changes").text
    # A loopback browser gets the button and no command.
    assert "Or restart" not in console.client().get("/activity").text


def test_deleted_config_offers_no_apply(console):
    client = console.client()
    console.cfg.unlink()
    console.clock.t += 3
    bar = client.get("/activity").text
    assert "config.toml is missing" in bar and "Apply now" not in bar
    r = apply(client, console, follow_redirects=False)
    assert r.headers["location"].endswith("flash=nothing")
    assert console.settings.scoring.daily_cap_usd == 2.0


def test_only_rows_backed_by_reloadable_settings_say_applied():
    from jobhunter import readiness as rd
    from jobhunter.console import setup_routes

    assert set(rd.CHECK_IDS) >= setup_routes.APPLIED_ROWS
    for restart_only in ("console.exposure", "secrets.valid", "data.permissions", "scorer.screen"):
        assert restart_only not in setup_routes.APPLIED_ROWS
