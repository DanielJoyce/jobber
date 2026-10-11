"""CLI behaviour of the run lock (specs/018 Run lock, C5): a second job spends nothing."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anthropic
import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import runlock
from jobhunter.pipeline import runner as pipeline_runner
from jobhunter.scoring import screen

cli = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Config on a temp DB; profile, scorer client and paid calls replaced by counters."""
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'data_dir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: SimpleNamespace())
    calls = SimpleNamespace(clients=0, submit=0, collect=0, run=0)

    def client():
        calls.clients += 1
        return SimpleNamespace()

    def submit_batch(*a, **k):
        calls.submit += 1
        return "msgbatch_1"

    def collect_pending(*a, **k):
        calls.collect += 1
        return {}

    def run_pipeline(*a, **k):
        calls.run += 1
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(anthropic, "Anthropic", client)
    monkeypatch.setattr(screen, "submit_batch", submit_batch)
    monkeypatch.setattr(screen, "collect_pending", collect_pending)
    monkeypatch.setattr(pipeline_runner, "run_pipeline", run_pipeline)
    monkeypatch.setattr(pipeline_runner, "format_summary", lambda report: "")
    monkeypatch.setattr(runlock, "POLL_S", 0.05)
    calls.db_path = db_path
    return calls


@pytest.fixture
def hold(run_lock_holder):
    return run_lock_holder


def test_second_submit_spends_nothing_while_another_job_runs(env, hold):
    h = hold(env.db_path)
    result = cli.invoke(app, ["score", "--submit"])
    assert result.exit_code == 0, result.output
    assert "another jobhunter job is running (run, pid" in result.output
    assert "nothing spent" in result.output
    assert (env.submit, env.clients) == (0, 0)
    h.release()
    h.wait()
    result = cli.invoke(app, ["score", "--submit"])
    assert result.exit_code == 0, result.output
    assert "submitted batch msgbatch_1" in result.output
    assert env.submit == 1


def test_interactive_run_reports_and_exits_zero(env, hold):
    hold(env.db_path)
    result = cli.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert "another jobhunter job is running" in result.output
    assert env.run == 0


def test_rescore_pending_without_wait_exits_zero(env, hold, monkeypatch):
    from jobhunter.scoring import rescore

    drained = []
    monkeypatch.setattr(rescore, "drain_pending", lambda *a, **k: drained.append(1) or [])
    hold(env.db_path)
    result = cli.invoke(app, ["score", "--rescore-pending"])
    assert result.exit_code == 0, result.output
    assert drained == []


def test_timer_run_waits_its_turn_then_runs(env, hold):
    # After downtime Persistent=true fires several timers at once: each must run in turn.
    h = hold(env.db_path)
    threading.Timer(0.3, h.release).start()
    result = cli.invoke(app, ["run", "--wait", "20s"])
    assert result.exit_code == 0, result.output
    assert env.run == 1


def test_collect_waits_for_the_running_job_then_collects(env, hold):
    h = hold(env.db_path)
    threading.Timer(0.3, h.release).start()
    result = cli.invoke(app, ["score", "--collect-pending"])  # waits 2h by default
    assert result.exit_code == 0, result.output
    assert env.collect == 1


def test_collect_defaults_to_a_two_hour_wait_and_wait_overrides_it(env, monkeypatch):
    seen = []
    real = runlock.RunLock.acquire

    def spy(self, wait=None):
        seen.append((self.command, wait))
        return real(self, wait)

    monkeypatch.setattr(runlock.RunLock, "acquire", spy)
    cli.invoke(app, ["score", "--collect-pending"])
    cli.invoke(app, ["score", "--collect-pending", "--wait", "10m"])
    cli.invoke(app, ["score", "--submit"])
    assert seen == [
        ("score --collect-pending", 7200.0),
        ("score --collect-pending", 600.0),
        ("score --submit", None),
    ]


def test_wait_running_out_exits_75_so_onfailure_fires(env, hold):
    hold(env.db_path)
    result = cli.invoke(app, ["run", "--wait", "0.3s"])
    assert result.exit_code == 75
    assert "gave up after waiting" in result.output
    assert env.run == 0


def test_a_holder_older_than_six_hours_exits_75(env, hold):
    hold(env.db_path)
    old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    lock_file = runlock.lock_path(env.db_path)
    meta = json.loads(lock_file.read_text())
    lock_file.write_text(json.dumps({**meta, "started_at": old}))
    for args in (["score", "--submit"], ["run", "--wait", "4h"]):
        result = cli.invoke(app, args)
        assert result.exit_code == 75, (args, result.output)
        assert "it may be hung" in result.output
    assert (env.submit, env.run) == (0, 0)


def test_bad_wait_is_a_usage_error(env):
    result = cli.invoke(app, ["run", "--wait", "soon"])
    assert result.exit_code == 2
    assert "not a duration" in result.output


def test_dry_run_takes_no_lock(env, hold):
    hold(env.db_path)
    result = cli.invoke(app, ["run", "--dry-run", "--stage", "list"])
    assert result.exit_code == 0, result.output
    assert "Run plan" in result.output


def test_lock_is_released_after_the_job(env):
    assert cli.invoke(app, ["score", "--submit"]).exit_code == 0
    assert runlock.RunLock(env.db_path, "probe").try_acquire()
