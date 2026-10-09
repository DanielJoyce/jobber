"""systemd --user schedule: rendering, install/uninstall, and score --collect-pending.

Nothing here runs systemctl or touches the network. Subprocess calls are mocked, and HOME is a
temporary directory so no real unit files are written.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.ops import schedule
from jobhunter.ops.schedule import Install

runner = CliRunner()
EXEC = Path("/opt/jobber/.venv/bin/jobhunter")
REPO = Path("/opt/jobber")
PATH = "/opt/jobber/.venv/bin:/usr/local/bin:/usr/bin:/bin"
SERVICES = (
    "jobhunter-run.service",
    "jobhunter-collect.service",
    "jobhunter-verify.service",
)


@pytest.fixture
def inst() -> Install:
    return Install(jobhunter=EXEC, repo=REPO, path=PATH)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail the test if anything tries to run a command."""

    def boom(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(schedule.subprocess, "run", boom)


class Recorder:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout, stderr="")


# ─── Rendering ──────────────────────────────────────────────────────────────


def test_services_use_absolute_exec_and_repo_paths(inst):
    units = schedule.render_units(inst)
    for name in SERVICES:
        text = units[name]
        assert f"WorkingDirectory={REPO}" in text
        exec_start = re.search(r"^ExecStart=(\S+)", text, re.MULTILINE)
        assert exec_start is not None
        assert exec_start.group(1) == str(EXEC)
        assert Path(exec_start.group(1)).is_absolute()
    assert "ExecStart=/opt/jobber/.venv/bin/jobhunter run\n" in units["jobhunter-run.service"]
    assert (
        "ExecStart=/opt/jobber/.venv/bin/jobhunter score --collect-pending"
        in units["jobhunter-collect.service"]
    )
    assert (
        "ExecStart=/opt/jobber/.venv/bin/jobhunter sources verify"
        in units["jobhunter-verify.service"]
    )


def test_services_are_oneshot_niced_with_only_path_env(inst):
    units = schedule.render_units(inst)
    for name in SERVICES:
        text = units[name]
        assert "Type=oneshot" in text
        assert "Nice=10" in text
        assert [ln for ln in text.splitlines() if ln.startswith("Environment=")] == [
            f"Environment=PATH={PATH}"
        ]


def test_every_service_has_onfailure_to_notify_template(inst):
    units = schedule.render_units(inst)
    for name in SERVICES:
        assert "OnFailure=jobhunter-notify@%n.service" in units[name]


def test_timers_schedule_and_persistence(inst):
    units = schedule.render_units(inst)
    run = units["jobhunter-run.timer"]
    assert "OnCalendar=*-*-* 02:00:00" in run
    assert "Persistent=true" in run
    assert "RandomizedDelaySec=10m" in run
    assert "Unit=jobhunter-run.service" in run
    collect = units["jobhunter-collect.timer"]
    assert "OnCalendar=*-*-* 03:30:00" in collect
    assert "Persistent=true" in collect
    assert "Unit=jobhunter-collect.service" in collect
    verify = units["jobhunter-verify.timer"]
    assert "OnCalendar=Sun *-*-* 04:00:00" in verify
    assert "Persistent=true" in verify
    for name in ("jobhunter-run.timer", "jobhunter-collect.timer", "jobhunter-verify.timer"):
        assert "WantedBy=timers.target" in units[name]


def test_notify_template_wiring(inst):
    text = schedule.render_units(inst)["jobhunter-notify@.service"]
    assert "ExecStart=/bin/sh -c" in text
    assert "%i" in text  # the failed unit's name
    assert "journalctl --user -u" in text and "-n 20" in text
    assert "systemd-cat" in text and "notify-send" in text
    # systemd's "$$" escape must survive rendering so the shell sees "$1".
    assert '"$$1"' in text


def test_no_placeholder_left_unrendered(inst):
    for name, text in schedule.render_units(inst).items():
        assert "@EXEC@" not in text and "@REPO@" not in text and "@PATH@" not in text, name


def test_exec_path_with_spaces_is_quoted():
    odd = Install(jobhunter=Path("/opt/my jobs/bin/jobhunter"), repo=REPO, path=PATH)
    text = schedule.render_units(odd)["jobhunter-run.service"]
    assert 'ExecStart="/opt/my jobs/bin/jobhunter" run' in text


def test_default_install_resolves_venv_and_checkout(tmp_path, monkeypatch):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "jobhunter").write_text("")
    (venv_bin / "python").write_text("")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "jobhunter"\n')
    monkeypatch.setattr(schedule.sys, "executable", str(venv_bin / "python"))
    monkeypatch.chdir(checkout)
    inst = schedule.default_install()
    assert inst.jobhunter == venv_bin / "jobhunter"
    assert inst.jobhunter.is_absolute()
    assert inst.repo == checkout
    assert inst.path.split(":")[0] == str(venv_bin)


# ─── Dry run and install ────────────────────────────────────────────────────


def test_dry_run_writes_nothing(inst, home, no_subprocess):
    text = schedule.install(inst, home=home, dry_run=True)
    assert not (home / ".config").exists()
    assert "nothing written (dry run)" in text
    assert "/opt/jobber/.venv/bin/jobhunter run" in text
    assert "systemctl --user daemon-reload" in text
    assert "systemctl --user enable --now" in text
    assert "jobhunter-collect.timer" in text


def test_dry_run_cli_writes_nothing(home, no_subprocess):
    result = runner.invoke(app, ["schedule", "install", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "daemon-reload" in result.output
    assert not (home / ".config").exists()


def test_install_writes_units_without_running_systemctl(inst, home, no_subprocess):
    text = schedule.install(inst, home=home)
    target = home / ".config" / "systemd" / "user"
    for name in schedule.UNIT_FILES:
        assert (target / name).read_text(encoding="utf-8") == schedule.render_units(inst)[name]
    assert "not run (pass --enable to run these)" in text
    assert "systemctl --user enable --now" in text


def test_install_enable_runs_only_the_two_commands(inst, home, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(schedule.subprocess, "run", rec)
    schedule.install(inst, home=home, enable=True)
    assert rec.calls == schedule.enable_commands()
    assert "jobhunter-run.timer" in rec.calls[1] and "jobhunter-verify.timer" in rec.calls[1]


def test_dry_run_and_enable_are_exclusive(inst, home, no_subprocess):
    with pytest.raises(schedule.ScheduleError):
        schedule.install(inst, home=home, dry_run=True, enable=True)
    assert not (home / ".config").exists()


def test_status_is_read_only_list_timers(monkeypatch):
    rec = Recorder(stdout="NEXT LEFT ... jobhunter-run.timer\n")
    monkeypatch.setattr(schedule.subprocess, "run", rec)
    out = schedule.status()
    assert rec.calls == [["systemctl", "--user", "list-timers", "jobhunter-*"]]
    assert "jobhunter-run.timer" in out


def test_uninstall_disables_and_removes_units(inst, home, monkeypatch):
    schedule.install(inst, home=home)
    target = home / ".config" / "systemd" / "user"
    keep = target / "unrelated.service"
    keep.write_text("")
    rec = Recorder()
    monkeypatch.setattr(schedule.subprocess, "run", rec)
    schedule.uninstall(home=home)
    assert rec.calls[0][:4] == ["systemctl", "--user", "disable", "--now"]
    assert set(schedule.TIMERS) <= set(rec.calls[0])
    assert rec.calls[1] == ["systemctl", "--user", "daemon-reload"]
    for name in schedule.UNIT_FILES:
        assert not (target / name).exists()
    assert keep.exists()


# ─── score --collect-pending with a fake client ─────────────────────────────


class FakeBatches:
    def __init__(self, statuses: dict[str, str], failing: set[str] = frozenset()) -> None:
        self.statuses = statuses
        self.failing = set(failing)
        self.retrieved: list[str] = []

    def retrieve(self, batch_id):
        self.retrieved.append(batch_id)
        if batch_id in self.failing:
            raise RuntimeError("network down")
        return SimpleNamespace(id=batch_id, processing_status=self.statuses[batch_id])

    def results(self, batch_id):
        return iter(())


class FakeClient:
    def __init__(self, statuses: dict[str, str], failing: set[str] = frozenset()) -> None:
        self.messages = SimpleNamespace(batches=FakeBatches(statuses, failing))


@pytest.fixture
def batch_db(tmp_path, monkeypatch):
    """A config pointing at a temp DB with one collected and two pending batches."""
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndb_path = "{db_path}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    conn = db.connect(db_path)
    db.migrate(conn)
    for batch_id, collected in (
        ("msgbatch_old", "2026-10-08T03:30:00Z"),
        ("msgbatch_a", None),
        ("msgbatch_b", None),
    ):
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "request_count, submitted_at, collected_at) "
            "VALUES (?, 'screen', 'anthropic:claude-haiku-4-5', 'p', 's', 1, "
            "'2026-10-08T02:00:00Z', ?)",
            (batch_id, collected),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: SimpleNamespace())
    return db_path


def test_collect_pending_polls_every_uncollected_batch_only(batch_db, monkeypatch):
    client = FakeClient({"msgbatch_a": "in_progress", "msgbatch_b": "in_progress"})
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    result = runner.invoke(app, ["score", "--collect-pending"])
    assert result.exit_code == 0, result.output
    assert sorted(client.messages.batches.retrieved) == ["msgbatch_a", "msgbatch_b"]
    assert '"status": "in_progress"' in result.output


def test_collect_pending_continues_past_a_failing_batch(batch_db, monkeypatch):
    client = FakeClient(
        {"msgbatch_a": "in_progress", "msgbatch_b": "in_progress"}, failing={"msgbatch_a"}
    )
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    result = runner.invoke(app, ["score", "--collect-pending"])
    assert result.exit_code == 1  # a failed collect makes the unit fail, so OnFailure fires
    assert sorted(client.messages.batches.retrieved) == ["msgbatch_a", "msgbatch_b"]
    assert "error:" in result.output


def test_collect_pending_with_nothing_pending(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndb_path = "{db_path}"\n')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: SimpleNamespace())
    client = FakeClient({})
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    result = runner.invoke(app, ["score", "--collect-pending"])
    assert result.exit_code == 0, result.output
    assert "no pending batches" in result.output
    assert client.messages.batches.retrieved == []


def test_collect_pending_is_exclusive_with_other_modes():
    result = runner.invoke(app, ["score", "--collect-pending", "--submit"])
    assert result.exit_code == 2
    assert "--collect-pending" in result.output
