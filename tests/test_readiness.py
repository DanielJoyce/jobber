"""Readiness checks (specs/019, ticket S1): one table-driven case per inventory row, the mode
differences, that nothing leaks a value, and that nothing touches the network, a subprocess or
the keyring. Fake values only."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from jobhunter import readiness, secrets
from jobhunter.config import Settings, load_settings
from jobhunter.core import db, dbpaths
from jobhunter.ops import preflight
from jobhunter.readiness import (
    BROKEN,
    CONTAINER,
    HOST,
    HOST_MANAGING,
    MISSING,
    OK,
    OPTIONAL_STATUS,
    UNKNOWN,
    Check,
    essentials_ok,
    run_checks,
)

SENTINEL = "SENTINEL-7f3c9a1e5b"
FAKE_KEY = "sk-ant-fake-0123456789abcdef"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
EXAMPLE_PREFS = Path(__file__).resolve().parent.parent / "examples" / "preferences.example.yaml"


@pytest.fixture(autouse=True)
def _fresh_caches():
    readiness.clear_caches()
    yield
    readiness.clear_caches()


@pytest.fixture
def home() -> Path:
    return Path(os.environ["HOME"])  # the autouse isolation fixture points HOME at a tmp dir


def make_settings(tmp_path: Path, **sections: dict) -> Settings:
    data = tmp_path / "data"
    overrides: dict = {
        "paths": {
            "data_dir": str(data),
            "db_path": str(data / "jobhunter.db"),
            "profile_dir": str(data / "profile"),
            "resume_path": str(data / "resume"),
        }
    }
    for name, values in sections.items():
        overrides.setdefault(name, {}).update(values)
    return load_settings(tmp_path / "no-config.toml", overrides=overrides)


def good_settings(tmp_path: Path, **sections: dict) -> Settings:
    sections.setdefault("fetch", {})["user_agent_contact"] = "jobs@example.org"
    return make_settings(tmp_path, **sections)


def write_prefs(settings: Settings, text: str | None = None) -> Path:
    profile = Path(settings.paths.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    target = profile / "preferences.yaml"
    if text is None:
        shutil.copy(EXAMPLE_PREFS, target)
    else:
        target.write_text(text, encoding="utf-8")
    return target


def write_resume(settings: Settings, name: str = "resume.md", text: str = "# Synthetic\n") -> Path:
    folder = Path(settings.paths.resume_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def make_db(settings: Settings) -> sqlite3.Connection:
    Path(settings.paths.data_dir).mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    return conn


def add_source(conn, key: str, status: str = "ok", note: str | None = None, last_ok=None):
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy, status, status_note)"
        " VALUES (?, 'C', ?, ?, 'api', 'https://example.test', 'enabled', ?, ?)",
        (key, key, key, status, note),
    )
    if last_ok:
        conn.execute(
            "INSERT INTO source_state (source_key, last_ok_at) VALUES (?, ?)",
            (key, last_ok.isoformat()),
        )
    conn.commit()


def evaluate(
    settings: Settings,
    env: dict[str, str] | None = None,
    *,
    mode: str = HOST,
    home: Path | None = None,
    env_files: list[Path] | None = None,
    **kwargs,
) -> dict[str, Check]:
    """Resolve secrets the way the CLI does, run every check, key the rows by id."""
    environ = dict(env or {})
    names = secrets.known_names(settings)
    states = secrets.resolve(names, set(environ), {}, lambda line: None, environ=environ)
    report = secrets.Report(states, env_files or [])
    checks = run_checks(
        settings,
        secrets_report=report,
        now=NOW,
        mode=mode,
        registry_rows=[],
        environ=environ,
        home=home if home is not None else Path(os.environ["HOME"]),
        **kwargs,
    )
    return {c.id: c for c in checks}


# --- shape ---------------------------------------------------------------------------------


def test_every_check_id_is_built_once_from_the_fixed_list(tmp_path):
    rows = evaluate(make_settings(tmp_path))
    assert set(rows) == set(readiness.CHECK_IDS)
    ids = [c.id for c in run_checks(
        make_settings(tmp_path), secrets_report=None, now=NOW, mode=HOST, registry_rows=[]
    )]  # fmt: skip
    assert len(ids) == len(set(ids))
    for check in rows.values():
        assert check.group in readiness.GROUPS
        assert check.level in {"essential", "recommended", "optional", "info"}
        assert check.status in {"ok", "missing", "broken", "optional", "unknown"}


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError):
        run_checks(make_settings(tmp_path), secrets_report=None, now=NOW, mode="vm")


def test_fresh_install_fails_the_essentials_it_should(tmp_path):
    rows = evaluate(make_settings(tmp_path))
    assert rows["fetch.contact"].status == BROKEN
    assert rows["profile.preferences"].status == MISSING
    assert rows["profile.resume"].status == MISSING
    assert rows["scorer.screen"].status == MISSING
    assert rows["db.schema"].status == OK  # no database yet is fine
    assert not essentials_ok(rows.values())


def test_complete_setup_passes_the_essentials(tmp_path):
    settings = good_settings(tmp_path)
    write_prefs(settings)
    write_resume(settings)
    make_db(settings).close()
    rows = evaluate(settings, {"ANTHROPIC_API_KEY": FAKE_KEY})
    failing = [c.id for c in rows.values() if c.level == "essential" and c.status != OK]
    assert failing == []
    assert essentials_ok(rows.values())


def test_unknown_never_counts_as_missing():
    unknown = Check("x", "Basics", "essential", "unknown", False, "t", "d", "u")
    broken = Check("x", "Basics", "essential", "broken", False, "t", "d", "u")
    assert essentials_ok([unknown])
    assert not essentials_ok([broken])


def test_cheap_only_skips_subscriptions_and_backups(tmp_path):
    settings = good_settings(tmp_path)
    full = evaluate(settings)
    cheap = evaluate(settings, cheap_only=True)
    assert set(full) - set(cheap) == {"mail.subscriptions", "backups.auto"}


# --- one case per inventory row ---------------------------------------------------------------


def test_contact_placeholder_is_broken_and_set_is_ok(tmp_path):
    assert evaluate(make_settings(tmp_path))["fetch.contact"].status == BROKEN
    settings = make_settings(tmp_path, fetch={"user_agent_contact": "<you>@example.com"})
    assert evaluate(settings)["fetch.contact"].status == BROKEN
    blank = make_settings(tmp_path, fetch={"user_agent_contact": "  "})
    assert evaluate(blank)["fetch.contact"].status == BROKEN
    assert evaluate(good_settings(tmp_path))["fetch.contact"].status == OK
    row = evaluate(make_settings(tmp_path))["fetch.contact"]
    assert row.level == "essential"
    assert "restart" in " ".join(row.fix.steps)


def test_preferences_missing_broken_yaml_and_broken_fields(tmp_path):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["profile.preferences"].status == MISSING
    write_prefs(settings, "target_titles: [unclosed\n  - : :\n")
    broken = evaluate(settings)["profile.preferences"]
    assert broken.status == BROKEN
    assert broken.level == "essential"
    write_prefs(settings, "hard:\n  remote_ok: banana-split\n")
    fields = evaluate(settings)["profile.preferences"]
    assert fields.status == BROKEN
    assert "hard.remote_ok" in fields.detail
    assert "banana-split" not in fields.detail  # field names, never the value
    write_prefs(settings)
    assert evaluate(settings)["profile.preferences"].status == OK


def test_resume_states(tmp_path):
    settings = good_settings(tmp_path)
    write_prefs(settings)
    assert evaluate(settings)["profile.resume"].status == MISSING
    first = write_resume(settings, "a.md")
    assert evaluate(settings)["profile.resume"].status == OK
    write_resume(settings, "b.md")
    two = evaluate(settings)["profile.resume"]
    assert two.status == BROKEN
    assert "a.md" not in two.detail  # file names can be a person's name
    (Path(settings.paths.resume_path) / "b.md").unlink()
    first.write_text("   \n", encoding="utf-8")
    empty = evaluate(settings)["profile.resume"]
    assert empty.status == BROKEN
    assert "empty" in empty.detail


def test_resume_cache_notices_a_new_file(tmp_path):
    settings = good_settings(tmp_path)
    write_prefs(settings)
    write_resume(settings, "a.md")
    assert evaluate(settings)["profile.resume"].status == OK
    write_resume(settings, "b.md")  # the folder's mtime changes
    assert evaluate(settings)["profile.resume"].status == BROKEN


def test_resume_console_and_run_resolutions_differ(tmp_path):
    settings = good_settings(tmp_path)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("# Other\n", encoding="utf-8")
    write_prefs(settings, f"resume_path: {elsewhere}\ntarget_titles: [Engineer]\n")
    write_resume(settings)
    row = evaluate(settings)["profile.resume"]
    assert row.status == BROKEN
    assert "read different resumes" in row.detail
    assert "elsewhere.md" in row.detail
    assert "resume.md" in row.detail  # both paths are shown


def test_resume_with_matching_yaml_value_is_ok(tmp_path):
    settings = good_settings(tmp_path)
    resume = write_resume(settings)
    write_prefs(settings, f"resume_path: {resume}\ntarget_titles: [Engineer]\n")
    assert evaluate(settings)["profile.resume"].status == OK


@pytest.mark.parametrize("mode", [CONTAINER, HOST_MANAGING])
def test_absolute_yaml_resume_path_is_broken_in_container_modes(tmp_path, mode):
    settings = good_settings(tmp_path)
    resume = write_resume(settings)
    write_prefs(settings, f"resume_path: {resume}\ntarget_titles: [Engineer]\n")
    row = evaluate(settings, mode=mode)["profile.resume"]
    assert row.status == BROKEN
    assert "absolute or outside" in row.detail
    assert "upload the resume again" in row.fix.text
    assert evaluate(settings, mode=HOST)["profile.resume"].status == OK
    write_prefs(settings, "resume_path: ../resume/resume.md\ntarget_titles: [Engineer]\n")
    assert evaluate(settings, mode=mode)["profile.resume"].status == OK


def test_profile_completeness(tmp_path):
    settings = good_settings(tmp_path)
    write_prefs(settings, "target_titles: []\n")
    thin = evaluate(settings)["profile.completeness"]
    assert thin.status == MISSING
    assert "target_titles" in thin.detail
    assert "salary_floor" in thin.detail
    write_prefs(
        settings,
        "target_titles: [Engineer]\nhard:\n  salary_floor: {amount: 1, period: year}\n"
        "current_focus: {doing: building things, done_with: [x]}\n",
    )
    assert evaluate(settings)["profile.completeness"].status == OK


@pytest.mark.parametrize(
    ("env", "expect"),
    [
        ({}, MISSING),
        ({"ANTHROPIC_API_KEY": FAKE_KEY}, OK),
        ({"ANTHROPIC_AUTH_TOKEN": "fake-bearer"}, OK),
        ({"ANTHROPIC_PROFILE": "work"}, OK),
        ({"ANTHROPIC_API_KEY": f"{SENTINEL} with space"}, BROKEN),
    ],
)
def test_screen_scorer_default_anthropic(tmp_path, env, expect):
    row = evaluate(good_settings(tmp_path), env)["scorer.screen"]
    assert row.status == expect
    assert row.level == "essential"
    assert row.reads_at_start
    if expect != OK:
        assert row.fix is not None
        assert row.fix.alt is not None  # "or pick another scorer"
    assert SENTINEL not in json.dumps(row.as_dict())


def test_screen_scorer_other_providers(tmp_path):
    settings = good_settings(tmp_path, scoring={"screen_scorer": "openrouter:x/y"})
    row = evaluate(settings)["scorer.screen"]
    assert row.status == MISSING
    assert "OPENROUTER_API_KEY" in row.detail
    assert evaluate(settings, {"OPENROUTER_API_KEY": "sk-or-fake"})["scorer.screen"].status == OK
    local = good_settings(tmp_path, scoring={"screen_scorer": "local:qwen"})
    assert evaluate(local)["scorer.screen"].status == UNKNOWN


def test_malformed_scorer_spec_is_broken(tmp_path):
    settings = good_settings(tmp_path, scoring={"screen_scorer": "no-such-provider:model"})
    assert evaluate(settings)["scorer.screen"].status == BROKEN


def test_default_scorer_is_labelled_default_and_explicit_is_config(tmp_path):
    assert evaluate(good_settings(tmp_path))["scorer.screen"].source == "default"
    explicit = good_settings(tmp_path, scoring={"screen_scorer": "anthropic:claude-haiku-5-5"})
    assert evaluate(explicit)["scorer.screen"].source == "config.toml"


def test_caps(tmp_path):
    row = evaluate(good_settings(tmp_path))["scorer.caps"]
    assert row.status == OK
    assert "$2.00/day" in row.detail
    zero = good_settings(tmp_path, scoring={"daily_cap_usd": 0})
    assert evaluate(zero)["scorer.caps"].status == BROKEN
    weekly = good_settings(tmp_path, scoring={"weekly_cap_usd": 0})
    assert evaluate(weekly)["scorer.caps"].status == BROKEN


def test_caps_show_todays_spend(tmp_path):
    settings = good_settings(tmp_path)
    conn = make_db(settings)
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) "
        "VALUES (?, 'm', 'screen', 1, 0.41)",
        (NOW.date().isoformat(),),
    )
    conn.execute(  # packet spend is not scoring spend
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) VALUES (?, 'm', 'packet', 1, 9)",
        (NOW.date().isoformat(),),
    )
    conn.commit()
    conn.close()
    assert "$0.41 spent today" in evaluate(settings)["scorer.caps"].detail


def test_deep_pass_level_follows_the_screen_scorer(tmp_path):
    anthropic = evaluate(good_settings(tmp_path))["scorer.deep"]
    assert anthropic.level == "recommended"
    assert anthropic.status == MISSING
    other = good_settings(tmp_path, scoring={"screen_scorer": "local:qwen"})
    row = evaluate(other)["scorer.deep"]
    assert row.level == "optional"
    assert row.status == OPTIONAL_STATUS


def test_usajobs_by_mode(tmp_path, home):
    settings = good_settings(tmp_path)
    both = {"USAJOBS_API_KEY": "k-fake", "USAJOBS_EMAIL": "x@example.org"}
    assert evaluate(settings)["source.usajobs"].status == MISSING
    assert evaluate(settings, {"USAJOBS_API_KEY": "k-fake"})["source.usajobs"].status == MISSING
    assert evaluate(settings, both)["source.usajobs"].status == OK
    bad = evaluate(settings, {**both, "USAJOBS_API_KEY": "has space"})["source.usajobs"]
    assert bad.status == BROKEN
    # the console container never sees these; unset there is unknown, not missing
    assert evaluate(settings, mode=CONTAINER)["source.usajobs"].status == UNKNOWN
    assert evaluate(settings, both, mode=CONTAINER)["source.usajobs"].status == OK


def test_usajobs_names_come_from_registry_rows(tmp_path):
    class Row:
        key = "us-usajobs"
        config: ClassVar[dict] = {
            "auth": {"key_env": "MY_USAJOBS_KEY", "email_env": "MY_USAJOBS_MAIL"}
        }

    settings = good_settings(tmp_path)
    environ = {"MY_USAJOBS_KEY": "k", "MY_USAJOBS_MAIL": "m"}
    states = secrets.resolve(
        secrets.known_names(settings), set(environ), {}, print, environ=environ
    )
    checks = run_checks(
        settings, secrets_report=secrets.Report(states, []), now=NOW, mode=HOST,
        registry_rows=[Row()], environ=environ,
    )  # fmt: skip
    assert {c.id: c for c in checks}["source.usajobs"].status == OK


def test_default_registry_names_are_parsed_once(tmp_path, monkeypatch):
    calls = []

    def fake(rows):
        calls.append(rows)
        return ("USAJOBS_API_KEY", "USAJOBS_EMAIL")

    monkeypatch.setattr(preflight, "usajobs_auth", fake)
    settings = good_settings(tmp_path)
    for _ in range(3):
        run_checks(settings, secrets_report=None, now=NOW, mode=HOST)
    assert calls == [None]


def test_host_managing_reads_podman_dropins_not_the_shell(tmp_path, home):
    settings = good_settings(tmp_path)
    Path(settings.paths.data_dir).mkdir(parents=True)
    (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n", encoding="utf-8")
    shell_key = {"ANTHROPIC_API_KEY": FAKE_KEY}
    row = evaluate(settings, shell_key, mode=HOST_MANAGING)["scorer.screen"]
    assert row.status == MISSING  # the shell's key is not what the containers use
    assert "podman secret create" in " ".join(row.fix.steps)
    path = preflight.dropin_path(
        preflight.default_unit_dir(home), preflight.RUN, "jobhunter_anthropic"
    )
    path.parent.mkdir(parents=True)
    path.write_text(preflight.HEADER + "\n", encoding="utf-8")
    ok = evaluate(settings, mode=HOST_MANAGING)["scorer.screen"]
    assert ok.status == OK
    assert ok.source == "podman secret"


def test_secret_fix_per_mode(tmp_path):
    settings = good_settings(tmp_path)
    host = evaluate(settings)["scorer.screen"].fix
    assert "install -m 600 /dev/null ~/.env" in host.steps[0]
    assert "ANTHROPIC_API_KEY" in host.steps[1]
    assert "Ctrl-C" in host.steps[2]
    box = evaluate(settings, mode=CONTAINER)["scorer.screen"].fix
    assert box.steps[0].startswith("read -rs KEY && printf '%s' \"$KEY\" | podman secret create ")
    assert "--replace" not in box.steps[0]
    assert "container preflight" in box.steps[1]
    broken = evaluate(settings, {"ANTHROPIC_API_KEY": "has space"}, mode=CONTAINER)
    assert "podman secret create --replace" in broken["scorer.screen"].fix.steps[0]


def test_alerts_address(tmp_path):
    assert evaluate(good_settings(tmp_path))["mail.address"].status == BROKEN
    settings = good_settings(tmp_path, mail={"alerts_address": f"{SENTINEL}+jobs@example.org"})
    row = evaluate(settings)["mail.address"]
    assert row.status == OK
    assert SENTINEL not in json.dumps(row.as_dict())


def test_google_client_json(tmp_path, home, monkeypatch):
    path = tmp_path / "client.json"
    settings = good_settings(tmp_path, mail={"client_secrets_path": str(path)})
    assert evaluate(settings)["mail.client"].status == MISSING
    path.write_text("{}", encoding="utf-8")
    assert evaluate(settings)["mail.client"].status == OK
    path.unlink()
    assert evaluate(settings, mode=CONTAINER)["mail.client"].status == UNKNOWN
    monkeypatch.setenv("JOBHUNTER_GOOGLE_CLIENT_SECRETS", str(path))
    path.write_text("{}", encoding="utf-8")
    assert evaluate(good_settings(tmp_path))["mail.client"].status == OK


def write_token(home: Path):
    token = home / ".config" / "jobhunter" / "gmail_token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({"refresh_token": SENTINEL}), encoding="utf-8")


def write_auth_meta(settings: Settings, age: timedelta, storage: str = "file") -> None:
    path = Path(settings.paths.data_dir) / "gmail-auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"issued_at": (NOW - age).isoformat(), "storage": storage}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("days", "status", "due"),
    [(0, OK, False), (5, OK, False), (6, OK, True), (7, OK, True), (8, BROKEN, False)],
)
def test_gmail_token_age_while_the_app_is_in_testing(tmp_path, home, days, status, due):
    settings = good_settings(tmp_path)
    write_token(home)
    write_auth_meta(settings, timedelta(days=days, hours=1))
    row = evaluate(settings)["mail.token"]
    assert (row.status, row.due) == (status, due)
    assert row.level == "recommended"
    if days == 6:
        assert "tomorrow" in row.detail
    if status == BROKEN:
        assert row.fix.text == "jobhunter mail auth"


def test_gmail_keyring_storage_is_reported_without_opening_the_keyring(tmp_path, home):
    settings = good_settings(tmp_path)
    write_auth_meta(settings, timedelta(days=2), storage="keyring")
    # keyring.get_password raises under the autouse fixture; reaching it would fail the test
    row = evaluate(settings)["mail.token"]
    assert row.status == OK
    assert row.source == "keyring"


def test_gmail_token_without_metadata_is_unknown(tmp_path, home):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["mail.token"].status == UNKNOWN
    write_token(home)
    row = evaluate(settings)["mail.token"]
    assert row.status == UNKNOWN
    assert "mail status" in row.detail


def test_gmail_refresh_error_status_is_broken_with_fixed_wording(tmp_path, home):
    settings = good_settings(tmp_path)
    write_token(home)
    write_auth_meta(settings, timedelta(days=1))
    conn = make_db(settings)
    add_source(conn, "us-mailalerts", "broken", f"RefreshError: invalid_grant {SENTINEL}")
    conn.close()
    row = evaluate(settings)["mail.token"]
    assert row.status == BROKEN
    assert row.detail == "Gmail token expired or revoked"
    assert SENTINEL not in json.dumps(row.as_dict())


def test_gmail_successful_run_after_day_seven_turns_the_age_rule_off(tmp_path, home):
    settings = good_settings(tmp_path)
    write_token(home)
    write_auth_meta(settings, timedelta(days=10))
    assert evaluate(settings)["mail.token"].status == BROKEN
    conn = make_db(settings)
    add_source(conn, "us-mailalerts", "ok", None, last_ok=NOW - timedelta(hours=3))
    conn.close()
    row = evaluate(settings)["mail.token"]
    assert row.status == OK
    assert "left Testing" in row.detail


def test_gmail_token_in_container_modes(tmp_path, home, monkeypatch):
    settings = good_settings(tmp_path)
    assert evaluate(settings, mode=CONTAINER)["mail.token"].status == UNKNOWN
    Path(settings.paths.data_dir).mkdir(parents=True)
    (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n", encoding="utf-8")
    missing = evaluate(settings, mode=HOST_MANAGING)["mail.token"]
    assert missing.status == MISSING
    assert "--podman-secret" in missing.fix.text
    path = preflight.dropin_path(
        preflight.default_unit_dir(home), preflight.RUN, preflight.GMAIL_TOKEN_SECRET
    )
    path.parent.mkdir(parents=True)
    path.write_text(preflight.HEADER + "\n", encoding="utf-8")
    write_auth_meta(settings, timedelta(days=1), storage="podman secret jobhunter_gmail_token")
    assert evaluate(settings, mode=HOST_MANAGING)["mail.token"].status == OK


def test_mail_label_and_subscriptions_from_the_database(tmp_path):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["mail.label"].status == UNKNOWN
    assert evaluate(settings)["mail.subscriptions"].status == UNKNOWN  # no database
    conn = make_db(settings)
    assert evaluate(settings)["mail.subscriptions"].status == MISSING
    conn.execute(
        "INSERT INTO mail_message (message_id, family, origin_source_key, received_at, "
        "processed_at) VALUES ('m1', 'vos', 'src-a', ?, ?)",
        (NOW.isoformat(), (NOW - timedelta(days=1)).isoformat()),
    )
    for key in ("src-a", "src-b"):
        conn.execute(
            "INSERT INTO alert_signup (source_key, subscribed_at, updated_at) VALUES (?, ?, ?)",
            (key, NOW.isoformat(), NOW.isoformat()),
        )
    conn.commit()
    conn.close()
    rows = evaluate(settings)
    assert rows["mail.label"].status == OK
    assert rows["mail.subscriptions"].status == OK
    assert "2 subscribed, 1 receiving, 1 stale" in rows["mail.subscriptions"].detail


def test_email_rows_carry_their_step_numbers(tmp_path):
    rows = evaluate(good_settings(tmp_path))
    steps = {i: rows[i].step for i in ("mail.client", "mail.address", "mail.token", "mail.label")}
    assert steps == {"mail.client": 1, "mail.address": 2, "mail.token": 3, "mail.label": 4}
    assert rows["mail.subscriptions"].step == 5
    assert rows["mail.parsers"].level == "info"


def test_last_run_maps_known_errors_and_hides_unknown_ones(tmp_path):
    settings = good_settings(tmp_path)
    conn = make_db(settings)
    assert evaluate(settings)["run.last"].detail == "no run yet"
    add_source(conn, "us-usajobs")
    add_source(conn, "us-other")
    cur = conn.execute(
        "INSERT INTO run (started_at, ended_at, stages, exit_status) VALUES (?, ?, 'all', "
        "'degraded')",
        (NOW.isoformat(), NOW.isoformat()),
    )
    run_id = cur.lastrowid
    conn.execute(
        "INSERT INTO run_source (run_id, source_key, status, error) VALUES (?, 'us-usajobs', "
        "'broken', ?)",
        (run_id, f"UsajobsAuthMissing: set USAJOBS_API_KEY {SENTINEL}"),
    )
    conn.execute(
        "INSERT INTO run_source (run_id, source_key, status, error) VALUES (?, 'us-other', "
        "'broken', ?)",
        (run_id, f"ValueError: Bearer {SENTINEL}"),
    )
    conn.commit()
    conn.close()
    row = evaluate(settings)["run.last"]
    assert row.status == BROKEN
    assert "USAJOBS credentials missing" in row.detail
    assert "ValueError (see /sources)" in row.detail
    assert SENTINEL not in json.dumps(row.as_dict())


def test_schema_states_by_mode(tmp_path):
    settings = good_settings(tmp_path)
    conn = make_db(settings)
    top = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
    conn.execute("DELETE FROM schema_version WHERE version = ?", (top,))
    conn.commit()
    conn.close()
    host = evaluate(settings, mode=HOST)["db.schema"]
    assert host.status == OK  # a host without the marker migrates on the next command
    assert "migrates" in host.detail
    marker = Path(settings.paths.data_dir) / "deployment.toml"
    marker.write_text("x\n", encoding="utf-8")
    managed = evaluate(settings, mode=HOST_MANAGING)["db.schema"]
    assert managed.status == BROKEN
    assert managed.level == "essential"
    assert evaluate(settings, mode=CONTAINER)["db.schema"].status == BROKEN
    marker.unlink()
    conn = sqlite3.connect(settings.paths.db_path)
    conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (9999, 'x', 'y')")
    conn.commit()
    conn.close()
    assert evaluate(settings, mode=HOST)["db.schema"].status == BROKEN  # newer than this code


def test_permissions(tmp_path):
    settings = good_settings(tmp_path)
    make_db(settings).close()
    data = Path(settings.paths.data_dir)
    os.chmod(data, 0o700)
    os.chmod(settings.paths.db_path, 0o600)
    assert evaluate(settings)["data.permissions"].status == OK
    os.chmod(data, 0o755)
    loose = evaluate(settings)["data.permissions"]
    assert loose.status == BROKEN
    assert "0755" in loose.detail
    os.chmod(data, 0o700)
    os.chmod(settings.paths.db_path, 0o644)
    assert evaluate(settings)["data.permissions"].status == BROKEN


def test_secrets_and_env_file_permissions(tmp_path):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["secrets.valid"].status == OK
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n", encoding="utf-8")
    os.chmod(env_file, 0o644)
    row = evaluate(settings, env_files=[env_file])["secrets.valid"]
    assert row.status == BROKEN
    assert "readable by group or others" in row.detail
    assert "chmod 600" in row.fix.text
    os.chmod(env_file, 0o600)
    assert evaluate(settings, env_files=[env_file])["secrets.valid"].status == OK
    invalid = evaluate(settings, {"OPENROUTER_API_KEY": f"{SENTINEL} x"})["secrets.valid"]
    assert invalid.status == BROKEN
    assert "contains whitespace" in invalid.detail
    assert SENTINEL not in json.dumps(invalid.as_dict())


def test_scheduled_runs_by_mode(tmp_path, home):
    settings = good_settings(tmp_path)
    user_dir = home / ".config" / "systemd" / "user"
    assert evaluate(settings)["run.schedule"].status == MISSING
    assert evaluate(settings, mode=CONTAINER)["run.schedule"].status == UNKNOWN
    wants = user_dir / "timers.target.wants"
    wants.mkdir(parents=True)
    (wants / "jobhunter-run.timer").symlink_to(user_dir / "jobhunter-run.timer")
    assert evaluate(settings)["run.schedule"].status == OK
    Path(settings.paths.data_dir).mkdir(parents=True)
    (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n", encoding="utf-8")
    both = evaluate(settings, mode=HOST_MANAGING)["run.schedule"]
    assert both.status == BROKEN  # host timers still enabled beside the containers
    assert "schedule uninstall" in both.fix.text
    (wants / "jobhunter-run.timer").unlink()
    assert evaluate(settings, mode=HOST_MANAGING)["run.schedule"].status == MISSING
    (wants / "jobhunter-ctr-run.timer").symlink_to(user_dir / "jobhunter-ctr-run.timer")
    assert evaluate(settings, mode=HOST_MANAGING)["run.schedule"].status == OK


def test_backups(tmp_path):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["backups.auto"].status == MISSING
    auto = Path(settings.paths.data_dir) / "backups" / "auto"
    auto.mkdir(parents=True)
    from jobhunter.core import backup

    (auto / backup.auto_name(NOW - timedelta(days=10))).write_bytes(b"x")
    assert evaluate(settings)["backups.auto"].status == BROKEN
    (auto / backup.auto_name(NOW - timedelta(hours=5))).write_bytes(b"x")
    assert evaluate(settings)["backups.auto"].status == OK


def test_runner_row(tmp_path, fake_claude):
    settings = good_settings(tmp_path)
    ok = evaluate(settings)["apply.runner"]
    assert ok.status == OK
    from jobhunter.apply import runner_state

    runner_state.turn_off(Path(settings.paths.data_dir), "not a claude.ai login")
    off = evaluate(settings)["apply.runner"]
    assert off.status == BROKEN
    assert "claude.ai login" not in off.detail  # the stored reason is free text
    api = good_settings(tmp_path, apply={"runner": "api"})
    assert evaluate(api)["apply.runner"].status == MISSING
    assert evaluate(api, {"ANTHROPIC_API_KEY": FAKE_KEY})["apply.runner"].status == OK
    assert evaluate(settings, mode=CONTAINER)["apply.runner"].status == OPTIONAL_STATUS


def test_runner_row_without_claude_on_path(tmp_path):
    row = evaluate(good_settings(tmp_path))["apply.runner"]  # conftest hides the real binary
    assert row.status == MISSING
    assert "not on PATH" in row.detail


def test_extension_pairing(tmp_path):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["ext.pairing"].status == OPTIONAL_STATUS
    path = Path(settings.paths.data_dir) / "extension.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"token_sha256": "ab" * 32, "paired_at": "2026-01-01"}))
    assert evaluate(settings)["ext.pairing"].status == OK


def test_pdf_export_optional_when_not_installed(tmp_path, monkeypatch):
    from importlib import util

    monkeypatch.setattr(util, "find_spec", lambda name: None)
    row = evaluate(good_settings(tmp_path))["pdf.export"]
    assert row.status == OPTIONAL_STATUS
    assert row.level == "optional"


def test_containers_row_by_mode(tmp_path):
    settings = good_settings(tmp_path)
    assert evaluate(settings)["containers.deployment"].status == OPTIONAL_STATUS
    assert evaluate(settings, mode=CONTAINER)["containers.deployment"].status == UNKNOWN
    conn = make_db(settings)
    (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n", encoding="utf-8")
    managed = evaluate(settings, mode=HOST_MANAGING)["containers.deployment"]
    assert managed.level == "essential"
    assert managed.status == UNKNOWN
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO application (id, job_group_id, status, created_at, updated_at) "
        "VALUES (1, 1, 'applied', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO attachment (application_id, kind, path, added_at) "
        "VALUES (1, 'other', '/home/someone/cv.pdf', 'x')"
    )
    conn.commit()
    absolute = evaluate(settings, mode=HOST_MANAGING)["containers.deployment"]
    assert absolute.status == BROKEN
    assert "absolute" in absolute.detail
    assert "/home/someone" not in absolute.detail
    conn.close()


def test_console_exposure_is_info_and_never_a_fault(tmp_path):
    row = evaluate(good_settings(tmp_path, console={"host": "0.0.0.0"}))["console.exposure"]
    assert row.level == "info"
    assert row.status == OK
    assert "no authentication" in row.detail


def test_paths_are_shown_with_home_as_tilde_and_container_paths_mapped(tmp_path, home):
    settings = make_settings(tmp_path)
    settings = load_settings(
        tmp_path / "x.toml",
        overrides={
            "paths": {
                "data_dir": "/data", "db_path": "/data/jobhunter.db",
                "profile_dir": "/data/profile", "resume_path": "/data/resume",
            }
        },
    )  # fmt: skip
    row = evaluate(settings, mode=CONTAINER)["profile.preferences"]
    assert "~/.local/share/jobhunter/profile/preferences.yaml [/data/profile/preferences.yaml]" in (
        row.detail
    )


# --- the moved helper ------------------------------------------------------------------------


def test_absolute_path_count_moved_to_dbpaths(tmp_path):
    settings = good_settings(tmp_path)
    db_path, data_dir = Path(settings.paths.db_path), Path(settings.paths.data_dir)
    assert dbpaths.absolute_path_count(db_path, data_dir) == (None, "")
    conn = make_db(settings)
    conn.close()
    assert dbpaths.absolute_path_count(db_path, data_dir) == (0, "")


# --- secrets and the keyring fixture ----------------------------------------------------------


def test_anthropic_auth_token_is_a_known_secret():
    assert "ANTHROPIC_AUTH_TOKEN" in secrets.DEFAULT_NAMES
    env = {"ANTHROPIC_AUTH_TOKEN": "has space"}
    states = secrets.resolve(["ANTHROPIC_AUTH_TOKEN"], set(env), {}, lambda line: None, environ=env)
    assert states[0].state == secrets.INVALID  # validated like every other secret
    assert "ANTHROPIC_AUTH_TOKEN" not in env  # and dropped, so the SDK never sees it


def test_the_keyring_is_blocked_in_every_test():
    import keyring

    with pytest.raises(Exception, match="OS keyring"):
        keyring.get_password("jobhunter", "gmail")


# --- no leaks, no subprocess, no network ------------------------------------------------------


ALL_SECRET_NAMES = (*secrets.DEFAULT_NAMES, "ANTHROPIC_API_KEY_FILE", "MY_CUSTOM_KEY")


def sentinel_setup(tmp_path: Path, home: Path):
    """Every secret, the contact and the alerts address set to a sentinel; hostile free-form
    text everywhere a check might copy it from."""
    settings = make_settings(
        tmp_path,
        fetch={"user_agent_contact": f"{SENTINEL}-contact@example.org"},
        mail={"alerts_address": f"{SENTINEL}+jobs@example.org"},
        scoring={"openrouter": {"api_key_env": "MY_CUSTOM_KEY"}},
    )
    write_prefs(settings)
    write_resume(settings, "resume.md", f"# {SENTINEL}\n")
    write_token(home)
    write_auth_meta(settings, timedelta(days=6))
    (Path(settings.paths.data_dir) / "extension.json").write_text(
        json.dumps({"token_sha256": SENTINEL, "paired_at": SENTINEL}), encoding="utf-8"
    )
    conn = make_db(settings)
    add_source(conn, "us-mailalerts", "broken", f"RefreshError: {SENTINEL}")
    add_source(conn, "us-usajobs", "broken", f"UsajobsAuthMissing: {SENTINEL}")
    run_id = conn.execute(
        "INSERT INTO run (started_at, ended_at, stages, exit_status) VALUES (?, ?, 'all', "
        "'degraded')",
        (NOW.isoformat(), NOW.isoformat()),
    ).lastrowid
    conn.execute(
        "INSERT INTO run_source (run_id, source_key, status, error) VALUES (?, 'us-usajobs', "
        "'broken', ?)",
        (run_id, f"KeyError: {SENTINEL}"),
    )
    conn.commit()
    conn.close()
    env = {name: f"{SENTINEL}-value" for name in secrets.DEFAULT_NAMES}
    env["MY_CUSTOM_KEY"] = f"{SENTINEL}-custom"
    return settings, env


@pytest.mark.parametrize("mode", [HOST, CONTAINER, HOST_MANAGING])
def test_no_value_leaks_into_any_check(tmp_path, home, mode):
    settings, env = sentinel_setup(tmp_path, home)
    if mode == HOST_MANAGING:
        (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n", encoding="utf-8")
    checks = run_checks(
        settings,
        secrets_report=secrets.Report(
            secrets.resolve(secrets.known_names(settings), set(env), {}, print, environ=dict(env)),
            [],
        ),
        now=NOW,
        mode=mode,
        registry_rows=[],
        environ=dict(env),
        home=home,
    )
    text = json.dumps([c.as_dict() for c in checks], default=str)
    assert SENTINEL not in text
    assert "jobs@example.org" not in text
    assert "daniel" not in text.lower()
    # the sentinel really was in play: the secret rows saw the values
    assert any(c.id == "scorer.screen" and c.status == OK for c in checks) or mode != HOST


def test_logs_carry_no_value_either(tmp_path, home, caplog):
    settings, env = sentinel_setup(tmp_path, home)
    with caplog.at_level("DEBUG"):
        evaluate(settings, env)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("mode", [HOST, CONTAINER, HOST_MANAGING])
def test_no_subprocess_no_network_no_keyring(tmp_path, home, monkeypatch, mode):
    """Every check, in every mode, with Popen and the sockets guarded and the keyring raising
    (conftest). A check that shelled out, connected or opened the keyring fails here."""
    settings, env = sentinel_setup(tmp_path, home)
    if mode == HOST_MANAGING:
        (Path(settings.paths.data_dir) / "deployment.toml").write_text("x\n", encoding="utf-8")
    spawned: list[object] = []

    def no_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("a readiness check started a subprocess")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(os, "system", no_spawn)
    monkeypatch.setattr(os, "posix_spawn", no_spawn, raising=False)
    rows = evaluate(settings, env, mode=mode)
    assert len(rows) == len(readiness.CHECK_IDS)
    assert spawned == []


def test_run_checks_is_fast_with_warm_caches(tmp_path, home):
    settings = good_settings(tmp_path)
    write_prefs(settings)
    write_resume(settings)
    make_db(settings).close()
    env = {"ANTHROPIC_API_KEY": FAKE_KEY}
    evaluate(settings, env)  # warm: the lenient profile state is cached per process
    start = time.perf_counter()
    for _ in range(5):
        evaluate(settings, env)
    per_call = (time.perf_counter() - start) / 5
    assert per_call < 0.05, f"{per_call * 1000:.0f} ms per run_checks"
