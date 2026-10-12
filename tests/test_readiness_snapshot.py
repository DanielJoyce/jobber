"""The readiness snapshot (specs/019 S4): sectioned readiness.json with a lock, the writers
(run, doctor --snapshot), the effective status the console shows, the run trigger and the
timer rules. Synthetic data only; no network, no subprocess, no paid call."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobhunter import cli, readiness
from jobhunter import readiness_snapshot as snap
from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.ops import preflight, schedule
from jobhunter.readiness import BROKEN, CONTAINER, HOST, HOST_MANAGING, OK, UNKNOWN, essentials_ok
from test_readiness import (
    FAKE_KEY,
    NOW,
    SENTINEL,
    evaluate,
    good_settings,
    make_db,
    write_prefs,
    write_resume,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_caches():
    readiness.clear_caches()
    yield
    readiness.clear_caches()


@pytest.fixture
def home() -> Path:
    return Path(os.environ["HOME"])


def ready(tmp_path):
    settings = good_settings(tmp_path)
    write_prefs(settings)
    write_resume(settings)
    make_db(settings).close()
    return settings


def data_dir(settings) -> Path:
    return Path(settings.paths.data_dir)


def run_view(settings, env, *, mode=HOST, at=NOW):
    """What a run process with ``env`` sees (its own view, never merged)."""
    rows = evaluate(settings, env, mode=mode, use_snapshot=False, sees_run_secrets=True)
    return list(rows.values())


def put_inputs(settings, env, *, at, trigger="timer", mode=HOST, section=snap.RUN_INPUTS):
    checks = run_view(settings, env, mode=mode)
    writer = "run" if section == snap.RUN_INPUTS else "shell"
    body = snap.inputs_section(checks, now=at, writer=writer, trigger=trigger, mode=mode)
    assert snap.write_section(data_dir(settings), section, body)


def put_outcome(settings, score, *, at, trigger="timer"):
    def build(previous):
        return snap.outcome_section(
            previous, now=at, trigger=trigger, run_id=1, score=score, sources=[]
        )

    assert snap.write_section(data_dir(settings), snap.RUN_OUTCOME, build)


# --- the file --------------------------------------------------------------------------------


def test_each_writer_replaces_only_its_own_section(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    assert snap.write_section(d, snap.HOST, {"checked_at": "h"})
    assert snap.write_section(d, snap.MANUAL_INPUTS, {"checked_at": "m"})
    assert snap.write_section(d, snap.RUN_INPUTS, {"checked_at": "r"})
    data = snap.read(d)
    assert {k: data[k]["checked_at"] for k in snap.SECTIONS if k in data} == {
        "host": "h",
        "manual_inputs": "m",
        "run_inputs": "r",
    }
    assert snap.write_section(d, snap.MANUAL_INPUTS, {"checked_at": "m2"})
    data = snap.read(d)
    assert data["run_inputs"]["checked_at"] == "r" and data["host"]["checked_at"] == "h"
    assert stat.S_IMODE((d / "readiness.json").stat().st_mode) == 0o600
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]
    with pytest.raises(ValueError):
        snap.write_section(d, "elsewhere", {})


def test_a_lock_timeout_writes_nothing(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    fd = os.open(d / snap.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert snap.write_section(d, snap.RUN_INPUTS, {"checked_at": "x"}, timeout=0.1) is False
    finally:
        os.close(fd)
    assert not (d / "readiness.json").exists()


def test_a_damaged_file_reads_as_empty(tmp_path):
    (tmp_path / "readiness.json").write_text("{not json")
    assert snap.read(tmp_path) == {}
    (tmp_path / "readiness.json").write_text("[1, 2]")
    assert snap.read(tmp_path) == {}


def test_not_run_keeps_the_earlier_score_outcome(tmp_path):
    settings = ready(tmp_path)
    put_outcome(settings, snap.SKIPPED_NO_CREDENTIAL, at=NOW - timedelta(hours=2))
    put_outcome(settings, snap.NOT_RUN, at=NOW - timedelta(hours=1), trigger="manual")
    out = snap.read(data_dir(settings))["run_outcome"]
    assert out["score"] == snap.SKIPPED_NO_CREDENTIAL
    assert out["score_trigger"] == "timer"
    assert out["trigger"] == "manual"  # the outcome itself is the newer run's
    put_outcome(settings, snap.SCORED, at=NOW)
    assert snap.read(data_dir(settings))["run_outcome"]["score"] == snap.SCORED


# --- effective status ---------------------------------------------------------------------------


def test_outcome_skipped_breaks_the_scorer_the_console_sees_until_newer_inputs(tmp_path):
    settings = ready(tmp_path)
    key = {"ANTHROPIC_API_KEY": FAKE_KEY}
    assert essentials_ok(evaluate(settings, key).values())
    # the nightly run did not see the key
    put_inputs(settings, {}, at=NOW - timedelta(hours=3))
    put_outcome(settings, snap.SKIPPED_NO_CREDENTIAL, at=NOW - timedelta(hours=2))
    rows = evaluate(settings, key)
    row = rows["scorer.screen"]
    assert row.status == BROKEN
    assert "nightly run at" in row.detail and "did not score" in row.detail
    texts = [row.fix.text] + [f.text for f in (row.fix.alt, row.fix.alt and row.fix.alt.alt) if f]
    assert any("spends credits" in t and "run --stage prefilter,score" in t for t in texts)
    assert not essentials_ok(rows.values())
    # the banner (cheap rows) shows it too
    banner = evaluate(settings, key, cheap_only=True)
    assert banner["scorer.screen"].status == BROKEN
    # the user fixes it and runs again: newer run_inputs show the row ok
    put_inputs(settings, key, at=NOW - timedelta(minutes=5), trigger="manual")
    assert evaluate(settings, key)["scorer.screen"].status == OK


def test_worse_of_console_and_run_is_shown_with_the_disagreement(tmp_path):
    settings = ready(tmp_path)
    key = {"ANTHROPIC_API_KEY": FAKE_KEY}
    put_inputs(settings, {}, at=NOW - timedelta(hours=1))
    row = evaluate(settings, key)["scorer.screen"]
    assert row.status != OK
    assert "the console sees it as ok" in row.detail
    assert "nightly run at" in row.detail and "did not" in row.detail
    # the run seeing more than the console never makes a row better
    put_inputs(settings, key, at=NOW - timedelta(minutes=30))
    assert evaluate(settings, {})["scorer.screen"].status != OK


def test_manual_inputs_never_stand_in_for_the_run_on_rows_the_console_sees(tmp_path):
    settings = ready(tmp_path)
    put_inputs(settings, {}, at=NOW - timedelta(hours=1), section=snap.MANUAL_INPUTS)
    assert evaluate(settings, {"ANTHROPIC_API_KEY": FAKE_KEY})["scorer.screen"].status == OK


def test_stale_snapshot_leaves_console_rows_and_container_rows_unknown(tmp_path):
    settings = ready(tmp_path)
    old = NOW - timedelta(hours=40)
    put_inputs(settings, {"USAJOBS_API_KEY": "k", "USAJOBS_EMAIL": "e"}, at=old, mode=CONTAINER)
    put_outcome(settings, snap.SKIPPED_NO_CREDENTIAL, at=old)
    key = {"ANTHROPIC_API_KEY": FAKE_KEY}
    rows = evaluate(settings, key, mode=CONTAINER)
    assert rows["scorer.screen"].status == OK
    assert rows["source.usajobs"].status == UNKNOWN
    assert rows["mail.client"].status == UNKNOWN


def test_rows_the_console_cannot_see_take_the_newer_fresh_section(tmp_path, monkeypatch):
    settings = ready(tmp_path)
    monkeypatch.setattr(readiness, "usajobs_names", lambda rows: ("USAJOBS_API_KEY", "UJ_EMAIL"))
    usajobs = {"USAJOBS_API_KEY": "k", "UJ_EMAIL": "e"}
    put_inputs(settings, {}, at=NOW - timedelta(hours=5), mode=CONTAINER)
    rows = evaluate(settings, {}, mode=CONTAINER)
    assert rows["source.usajobs"].status == "missing"
    assert "as seen by the nightly run at" in rows["source.usajobs"].detail
    put_inputs(
        settings, usajobs, at=NOW - timedelta(hours=1), mode=CONTAINER,
        section=snap.MANUAL_INPUTS,
    )  # fmt: skip
    row = evaluate(settings, {}, mode=CONTAINER)["source.usajobs"]
    assert row.status == OK
    assert "as seen from your shell at" in row.detail


def test_host_section_fills_the_containers_row(tmp_path):
    settings = ready(tmp_path)
    assert evaluate(settings, mode=CONTAINER)["containers.deployment"].status == UNKNOWN
    fail = readiness.Check(
        "preflight.quadlet", "Running", "essential", BROKEN, False, "Quadlet dry run",
        "failed; run `jobhunter container preflight --dry-run` for details", "x",
    )  # fmt: skip
    body = snap.host_section([fail], now=NOW - timedelta(hours=2))
    snap.write_section(data_dir(settings), snap.HOST, body)
    row = evaluate(settings, mode=CONTAINER)["containers.deployment"]
    assert row.status == BROKEN and row.level == "essential"
    assert "Quadlet dry run" in row.detail
    ok = readiness.Check("preflight.podman", "Running", "recommended", OK, False, "Podman", "", "")
    snap.write_section(data_dir(settings), snap.HOST, snap.host_section([ok], now=NOW))
    assert evaluate(settings, mode=CONTAINER)["containers.deployment"].status == OK


def test_no_profile_outcome_breaks_preferences_until_newer_inputs(tmp_path):
    settings = ready(tmp_path)
    put_outcome(settings, snap.NO_PROFILE, at=NOW - timedelta(hours=1))
    assert evaluate(settings)["profile.preferences"].status == BROKEN
    put_inputs(settings, {}, at=NOW - timedelta(minutes=1))
    assert evaluate(settings)["profile.preferences"].status == OK


def test_unknown_rows_never_raise_the_banner(tmp_path):
    settings = ready(tmp_path)
    rows = evaluate(settings, {"ANTHROPIC_API_KEY": FAKE_KEY}, mode=CONTAINER)
    assert rows["source.usajobs"].status == UNKNOWN
    assert essentials_ok(rows.values())


def test_snapshot_never_holds_a_value(tmp_path):
    settings = ready(tmp_path)
    env = {
        "ANTHROPIC_API_KEY": f"sk-ant-{SENTINEL}",
        "OPENROUTER_API_KEY": f"{SENTINEL} bad",
        "USAJOBS_API_KEY": SENTINEL,
    }
    put_inputs(settings, env, at=NOW)
    put_outcome(settings, f"failed:{ValueError.__name__}", at=NOW)
    assert SENTINEL not in (data_dir(settings) / "readiness.json").read_text()
    reasons = snap.source_reasons(
        [SimpleNamespace(key="us-x", failed=True, error=f"ValueError: Bearer {SENTINEL}")]
    )
    assert reasons == [("us-x", "ValueError (see /sources)")]


# --- trigger and timers ---------------------------------------------------------------------


def test_trigger_from_the_environment():
    assert snap.current_trigger({"JOBHUNTER_TRIGGER": "timer"}) == "timer"
    assert snap.current_trigger({"JOBHUNTER_TRIGGER": "cron"}) == "manual"
    assert snap.current_trigger({}) == "manual"


def add_run(settings, started, trigger, *, ended=True):
    conn = sqlite3.connect(settings.paths.db_path)
    conn.execute(
        'INSERT INTO run (started_at, ended_at, stages, exit_status, "trigger") '
        "VALUES (?, ?, 'list', 'ok', ?)",
        (started.isoformat(), started.isoformat() if ended else None, trigger),
    )
    conn.commit()
    conn.close()


def enable_host_timers(home: Path, *, marked: bool | None) -> None:
    user_dir = schedule.unit_dir(home)
    wants = user_dir / "timers.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    if not (wants / "jobhunter-run.timer").is_symlink():
        (wants / "jobhunter-run.timer").symlink_to(user_dir / "jobhunter-run.timer")
    if marked is not None:
        line = schedule.TRIGGER_LINE + "\n" if marked else ""
        (user_dir / "jobhunter-run.service").write_text(f"[Service]\n{line}ExecStart=x\n")


def test_a_manual_run_does_not_hide_dead_timers(tmp_path, home):
    settings = ready(tmp_path)
    enable_host_timers(home, marked=True)
    add_run(settings, NOW - timedelta(days=3), "timer")
    add_run(settings, NOW - timedelta(hours=1), "manual")
    row = evaluate(settings)["run.schedule"]
    assert row.status == BROKEN
    assert "timers not running" in row.detail
    last = evaluate(settings)["run.last"]
    assert "manual run at" in last.detail and "nightly" not in last.detail
    add_run(settings, NOW - timedelta(minutes=30), "timer")
    ok = evaluate(settings)["run.schedule"]
    assert ok.status == OK and "nightly run at" in ok.detail
    assert "nightly run at" in evaluate(settings)["run.last"].detail


def test_unmarked_unit_falls_back_and_never_reports_dead_timers(tmp_path, home):
    settings = ready(tmp_path)
    enable_host_timers(home, marked=False)
    add_run(settings, NOW - timedelta(days=3), "timer")
    add_run(settings, NOW - timedelta(hours=1), "manual")
    row = evaluate(settings)["run.schedule"]
    assert row.status == OK
    assert "timers not running" not in row.detail
    assert "timer runs are not marked yet" in row.detail
    assert "jobhunter schedule install" in row.detail
    assert row.due  # an action to take, in the recommended line


def test_marked_unit_with_only_migrated_null_rows_falls_back(tmp_path, home):
    settings = ready(tmp_path)
    enable_host_timers(home, marked=True)
    add_run(settings, NOW - timedelta(days=5), None)  # from before migration 0038
    row = evaluate(settings)["run.schedule"]
    assert row.status == OK
    assert "timers not running" not in row.detail
    assert "no timer-started run recorded yet" in row.detail
    assert evaluate(settings)["run.last"].detail.startswith("run at")


def test_container_mode_judges_timer_rows_and_falls_back_without_them(tmp_path):
    settings = ready(tmp_path)
    add_run(settings, NOW - timedelta(hours=2), "manual")
    row = evaluate(settings, mode=CONTAINER)["run.schedule"]
    assert row.status == UNKNOWN and "timer runs are not marked yet" in row.detail
    add_run(settings, NOW - timedelta(days=3), "timer")
    assert evaluate(settings, mode=CONTAINER)["run.schedule"].status == BROKEN
    add_run(settings, NOW - timedelta(hours=1), "timer")
    assert evaluate(settings, mode=CONTAINER)["run.schedule"].status == OK


def test_host_managing_reads_the_trigger_dropin(tmp_path, home):
    settings = ready(tmp_path)
    (data_dir(settings) / "deployment.toml").write_text("x\n")
    user_dir = schedule.unit_dir(home)
    wants = user_dir / "timers.target.wants"
    wants.mkdir(parents=True)
    (wants / "jobhunter-ctr-run.timer").symlink_to(user_dir / "jobhunter-ctr-run.timer")
    units = preflight.default_unit_dir(home)
    units.mkdir(parents=True)
    (units / f"{preflight.RUN}.container").write_text("[Container]\nExec=run\n")
    add_run(settings, NOW - timedelta(days=3), "timer")
    unmarked = evaluate(settings, mode=HOST_MANAGING)["run.schedule"]
    assert unmarked.status == OK and "jobhunter container preflight" in unmarked.detail
    path = preflight.trigger_dropin_path(units, preflight.RUN)
    path.parent.mkdir()
    path.write_text(preflight.trigger_dropin_text())
    assert evaluate(settings, mode=HOST_MANAGING)["run.schedule"].status == BROKEN


def test_last_run_reads_a_database_from_before_the_trigger_column(tmp_path):
    settings = good_settings(tmp_path)
    d = data_dir(settings)
    d.mkdir(parents=True)
    conn = sqlite3.connect(settings.paths.db_path)
    conn.execute(
        "CREATE TABLE run (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, "
        "stages TEXT NOT NULL, exit_status TEXT, counts TEXT)"
    )
    conn.execute(
        "INSERT INTO run (started_at, ended_at, stages, exit_status) VALUES (?, ?, 'x', 'ok')",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    conn.close()
    row = evaluate(settings)["run.last"]
    assert row.status == OK and row.detail.startswith("run at")


# --- migration ---------------------------------------------------------------------------------


def test_migration_leaves_existing_runs_null(tmp_path):
    conn = sqlite3.connect(":memory:")
    shipped = db._load_migrations()
    (ours,) = [version for version, name, _sql in shipped if "run_trigger" in name]
    for version, _name, sql in shipped:
        if version < ours:
            conn.executescript(sql)
    conn.execute("INSERT INTO run (started_at, stages) VALUES ('2026-01-01', 'list')")
    (sql,) = [sql for version, _name, sql in shipped if version == ours]
    conn.executescript(sql)
    assert conn.execute('SELECT "trigger" FROM run').fetchone()[0] is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""INSERT INTO run (started_at, stages, "trigger") VALUES ('x','y','cron')""")
    conn.close()


# --- the writers through the CLI -----------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{data / "jobhunter.db"}"\n'
        f'profile_dir = "{data / "profile"}"\nresume_path = "{data / "resume"}"\n'
        f'cache_dir = "{tmp_path / "cache"}"\n[fetch]\nuser_agent_contact = "jobs@example.org"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_registry", lambda *a, **k: [])
    data.mkdir()
    return data


def test_run_writes_inputs_and_outcome_and_a_partial_run_keeps_the_score(cli_env, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TRIGGER", "timer")
    first = runner.invoke(app, ["run"])
    assert first.exit_code == 0, first.output
    data = snap.read(cli_env)
    assert data["run_inputs"]["trigger"] == "timer" and data["run_inputs"]["writer"] == "run"
    assert {c["id"] for c in data["run_inputs"]["checks"]} == set(snap.INPUT_IDS)
    assert data["run_outcome"]["score"] == snap.NO_PROFILE  # no preferences.yaml
    conn = sqlite3.connect(cli_env / "jobhunter.db")
    assert conn.execute('SELECT "trigger" FROM run').fetchall() == [("timer",)]
    conn.close()
    monkeypatch.delenv("JOBHUNTER_TRIGGER")
    second = runner.invoke(app, ["run", "--stage", "resolve"])
    assert second.exit_code == 0, second.output
    out = snap.read(cli_env)["run_outcome"]
    assert out["trigger"] == "manual"
    assert out["score"] == snap.NO_PROFILE and out["score_trigger"] == "timer"
    conn = sqlite3.connect(cli_env / "jobhunter.db")
    assert [r[0] for r in conn.execute('SELECT "trigger" FROM run ORDER BY id')] == [
        "timer",
        "manual",
    ]
    conn.close()


def test_doctor_snapshot_writes_manual_inputs_and_never_run_inputs(cli_env, monkeypatch):
    run_inputs = {"checked_at": "2026-01-01T00:00:00+00:00", "writer": "run", "checks": []}
    snap.write_section(cli_env, snap.RUN_INPUTS, run_inputs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"sk-ant-{SENTINEL}")
    result = runner.invoke(app, ["doctor", "--snapshot"])
    assert result.exit_code in (0, 1), result.output
    assert "wrote manual_inputs" in result.output
    data = snap.read(cli_env)
    assert data["run_inputs"] == run_inputs
    assert data["manual_inputs"]["writer"] == "shell"
    assert data["manual_inputs"]["trigger"] == "manual"
    assert SENTINEL not in (cli_env / "readiness.json").read_text()
    assert SENTINEL not in result.output
    assert "host" not in data


def test_plain_doctor_writes_no_snapshot(cli_env):
    runner.invoke(app, ["doctor"])
    assert not (cli_env / "readiness.json").exists()


def test_doctor_shows_the_effective_status(cli_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    write = cli_env / "profile"
    write.mkdir()
    snap.write_section(
        cli_env,
        snap.RUN_OUTCOME,
        snap.outcome_section(
            None, now=snap.datetime.now(snap.UTC), trigger="timer", run_id=1,
            score=snap.SKIPPED_NO_CREDENTIAL, sources=[],
        ),
    )  # fmt: skip
    rows = {r["id"]: r for r in json.loads(runner.invoke(app, ["doctor", "--json"]).output)}
    assert rows["scorer.screen"]["status"] == BROKEN
    assert runner.invoke(app, ["doctor"]).exit_code == 1


# --- preflight's trigger drop-in -------------------------------------------------------------


def test_preflight_plans_and_keeps_the_trigger_dropin(tmp_path):
    units = tmp_path / "units"
    plan = preflight.plan_dropins(units, [], [])
    wanted = {preflight.trigger_dropin_path(units, u) for u in preflight.TRIGGER_UNITS}
    assert set(plan.write) == wanted
    for text in plan.write.values():
        assert text.startswith(preflight.HEADER + "\n")
        assert "Environment=JOBHUNTER_TRIGGER=timer" in text
    preflight.apply_dropins(plan)
    again = preflight.plan_dropins(units, [], [])
    assert again.remove == [] and set(again.keep) == wanted and not again.write
    # not mistaken for a secret drop-in by the generator check
    assert preflight._final_dropins(units, again) == set()
    # leave removes it with the rest
    assert all(p in preflight.our_dropins(units) for p in wanted)
