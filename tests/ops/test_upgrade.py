"""``jobhunter upgrade`` with fake podman, systemctl and git (spec 018 C15).

Nothing here starts a container or touches systemd: every external command goes to
:class:`FakeHost`, which records the argv sequence and answers like the real tools. The fake
``podman run ... db migrate`` really migrates the test database, as the container would.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from jobhunter.core import backup, db
from jobhunter.core.runlock import RunLock
from jobhunter.ops import ctr, upgrade
from jobhunter.ops import version as ver
from jobhunter.ops.proc import Result

REAL = db._load_migrations()
NEWEST = REAL[-1][0]
OLDER = REAL[-3][0]
COMMIT = "a1b2c3d4e5f6" + "0" * 28
SHORT = COMMIT[:12]
OLD_IMAGE = "sha256:0ld1mage"
OLD_REV = "9" * 40
UNITS = [upgrade.CONSOLE, *upgrade.TIMERS]


class FakeHost:
    """podman, systemctl, git and which, scripted. ``fail`` maps a step key to a Result."""

    def __init__(self, db_path: Path, *, which=("podman",)) -> None:
        self.db_path = db_path
        self.calls: list[list[str]] = []
        self.capture: list[bool] = []
        self.fail: dict[str, Result] = {}
        self.active = set(UNITS)
        self.running_services: list[str] = []
        self.dirty = ""
        self.oneoffs = ""
        self.previous = f"{OLD_IMAGE} {OLD_REV}"
        self.image_commit: str | None = None  # default: whatever was built
        self.built_commit: str | None = None
        self._which = set(which)

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self._which else None

    @staticmethod
    def key(argv: list[str]) -> str:
        """A short name for a call: 'git status', 'podman build', 'systemctl stop', ..."""
        while argv and argv[0] in {"flatpak-spawn", "--host", "host-spawn"}:
            argv = argv[1:]
        tool = argv[0]
        if tool == "systemctl":
            return f"systemctl {argv[2]}"
        if tool == "git":
            return f"git {argv[3]}"
        if tool == "podman" and argv[1] == "run":
            tail = argv[argv.index(ctr.IMAGE) + 1 :]
            return "podman run " + " ".join(tail)
        if tool == "podman" and argv[1] == "image":
            return "podman image inspect"
        return f"{tool} {argv[1]}"

    def run(self, argv, *, capture=True, timeout=None) -> Result:
        argv = list(argv)
        self.calls.append(argv)
        self.capture.append(capture)
        key = self.key(argv)
        if key in self.fail:
            return self.fail[key]
        if key == "git rev-parse":
            return Result(
                0, (SHORT if any(a.startswith("--short") for a in argv) else COMMIT) + "\n"
            )
        if key == "git status":
            return Result(0, self.dirty)
        if key == "podman version":
            return Result(0, "5.8.4\n")
        if key == "podman ps":
            return Result(0, self.oneoffs)
        if key == "podman image inspect":
            return Result(0, self.previous + "\n") if self.previous else Result(125, "", "no image")
        if key == "podman build":
            arg = argv[argv.index("--build-arg") + 1]
            self.built_commit = arg.split("=", 1)[1]
            return Result(0)
        if key == "systemctl is-active":
            on = argv[-1] in self.active
            return Result(0 if on else 3, "active\n" if on else "inactive\n")
        if key == "systemctl list-units":
            return Result(
                0, "".join(f"{s} loaded activating start x\n" for s in self.running_services)
            )
        if key in {"systemctl stop", "systemctl start", "podman tag"}:
            return Result(0)
        if key == "podman run db migrate":
            conn = db.connect(self.db_path)
            try:
                applied = db.migrate(conn, explicit=True)
            finally:
                conn.close()
            return Result(0, f"migrated: applied {applied}\n")
        if key == "podman run version --json":
            schema, state, unknown, pending = ver.database_state(self.db_path)
            info = {
                "package": "0.1.0",
                "commit": self.image_commit or self.built_commit,
                "commit_source": "build",
                "code_schema": NEWEST,
                "db_path": "/data/jobhunter.db",
                "db_schema": schema,
                "db_state": state,
                "unknown_migrations": unknown,
                "pending_migrations": pending,
                "deployment": "container",
                "marker": "/data/deployment.toml",
            }
            return Result(0, json.dumps(info, indent=2))
        raise AssertionError(f"unexpected command: {argv}")

    def steps(self) -> list[str]:
        return [self.key(c) for c in self.calls]

    def after(self, key: str) -> list[str]:
        keys = self.steps()
        return keys[keys.index(key) + 1 :] if key in keys else []


def make_db(path: Path, monkeypatch, version: int) -> None:
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in REAL if m[0] <= version])
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: REAL)


def schema_of(path: Path) -> int:
    raw = sqlite3.connect(path)
    try:
        return raw.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        raw.close()


def host_info() -> ver.VersionInfo:
    return ver.VersionInfo(
        package="0.1.0",
        commit=COMMIT,
        commit_source="git",
        code_schema=NEWEST,
        db_path="/x/jobhunter.db",
        db_schema=NEWEST,
        db_state="current",
        unknown_migrations=[],
        pending_migrations=[],
        deployment="container",
        marker="/x/deployment.toml",
    )


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


@pytest.fixture
def setup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "jobhunter.db"
    make_db(db_path, monkeypatch, OLDER)
    (data / db.DEPLOYMENT_MARKER).write_text('mode = "container"\n')
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    host = FakeHost(db_path)
    out: list[str] = []
    clock = Clock()
    ctx = upgrade.Context(
        repo=tmp_path / "repo",
        db_path=db_path,
        data_dir=data,
        layout=ctr.Layout(data, tmp_path / "cache", cfg),
        runner=host,
        echo=out.append,
        lock=lambda: RunLock(db_path, "upgrade"),
        wait_s=60,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: datetime(2026, 10, 10, 12, 0, 0),
        host_info=host_info,
    )
    return ctx, host, out, clock


def text(out: list[str]) -> str:
    return "\n".join(out)


HAPPY = [
    "git rev-parse",
    "git rev-parse",
    "git status",
    "podman version",
    "podman ps",
    "podman image inspect",
    "podman build",
    "systemctl is-active",
    "systemctl is-active",
    "systemctl is-active",
    "systemctl is-active",
    "systemctl stop",  # timers
    "systemctl list-units",
    "systemctl stop",  # console
    "podman tag",
    "podman run db migrate",
    "podman run version --json",
    "systemctl start",  # console
    "systemctl start",  # timers
]


def test_happy_path_runs_every_step_in_order(setup):
    ctx, host, out, _ = setup
    assert upgrade.run(ctx) == 0, text(out)
    assert host.steps() == HAPPY
    calls = host.calls
    build = calls[6]
    assert build[:4] == ["podman", "build", "--build-arg", f"BUILD_COMMIT={COMMIT}"]
    assert f"localhost/jobhunter:{SHORT}" in build
    assert host.capture[6] is False  # the build streams to the terminal
    assert calls[11] == ["systemctl", "--user", "stop", *upgrade.TIMERS]
    assert calls[13] == ["systemctl", "--user", "stop", upgrade.CONSOLE]
    assert calls[14] == ["podman", "tag", f"localhost/jobhunter:{SHORT}", ctr.IMAGE]
    assert calls[17] == ["systemctl", "--user", "start", upgrade.CONSOLE]
    assert calls[18] == ["systemctl", "--user", "start", *upgrade.TIMERS]
    # the database was backed up before migrating, and is now current
    backups = list((ctx.data_dir / "backups").glob("jobhunter-*-pre-upgrade.db"))
    assert len(backups) == 1 and schema_of(backups[0]) == OLDER
    assert schema_of(ctx.db_path) == NEWEST
    report = text(out)
    assert "upgrade done." in report
    assert f"commit:     {COMMIT} (git)" in report  # host
    assert f"commit:     {COMMIT} (build)" in report  # image
    # the run lock was released
    assert RunLock(ctx.db_path, "probe").try_acquire()


def test_migrate_runs_in_a_one_off_container_with_the_unit_mounts(setup):
    ctx, host, _out, _ = setup
    upgrade.run(ctx)
    migrate = next(c for c in host.calls if c[-2:] == ["db", "migrate"])
    assert migrate[:3] == ["podman", "run", "--rm"]
    assert f"{ctx.data_dir}:/data:z" in migrate
    assert f"{ctx.layout.config_file}:/config/config.toml:ro,z" in migrate
    assert "JOBHUNTER_DB_PATH=/data/jobhunter.db" in migrate
    assert migrate[migrate.index("--userns") : migrate.index("--userns") + 2] == [
        "--userns",
        ctr.USERNS,
    ]
    assert "--secret" not in migrate  # migrate and version need no secrets
    assert migrate[migrate.index(ctr.IMAGE) :] == [ctr.IMAGE, "db", "migrate"]


def test_dry_run_runs_nothing_but_git_reads(setup):
    ctx, host, out, _ = setup
    assert upgrade.run(ctx, dry_run=True) == 0
    assert host.steps() == ["git rev-parse", "git rev-parse", "git status"]
    report = text(out)
    assert "dry run: nothing is run" in report
    for word in ("build", "stop", "backup", "retag", "migrate", "verify", "start"):
        assert f"{word}:" in report, word
    assert f"BUILD_COMMIT={COMMIT}" in report
    assert not (ctx.data_dir / "backups").exists()
    assert schema_of(ctx.db_path) == OLDER


def test_dirty_checkout_is_refused_before_anything_runs(setup):
    ctx, host, _out, _ = setup
    host.dirty = " M src/jobhunter/cli.py\n?? src/jobhunter/new.py\n"
    with pytest.raises(upgrade.UpgradeError, match=r"uncommitted changes.*src/jobhunter/cli\.py"):
        upgrade.run(ctx)
    assert host.steps() == ["git rev-parse", "git rev-parse", "git status"]
    status = host.calls[2]
    assert status[-len(upgrade.IMAGE_INPUTS) :] == list(upgrade.IMAGE_INPUTS)


def test_allow_dirty_builds_a_dirty_tag(setup):
    ctx, host, out, _ = setup
    host.dirty = " M src/jobhunter/cli.py\n"
    assert upgrade.run(ctx, allow_dirty=True) == 0, text(out)
    assert host.built_commit == f"{COMMIT}-dirty"
    tag = next(c for c in host.calls if c[1] == "tag")
    assert tag[2] == f"localhost/jobhunter:{SHORT}-dirty"
    assert "warning: building uncommitted changes" in text(out)


def test_not_a_checkout_is_refused(setup):
    ctx, host, _out, _ = setup
    host.fail["git rev-parse"] = Result(128, "", "fatal: not a git repository")
    with pytest.raises(upgrade.UpgradeError, match="not a git checkout"):
        upgrade.run(ctx)


def test_newer_database_is_refused_before_anything_runs(setup, monkeypatch):
    ctx, host, _out, _ = setup
    raw = sqlite3.connect(ctx.db_path)
    raw.execute("INSERT INTO schema_version VALUES (?, 'future', 't')", (NEWEST + 1,))
    raw.commit()
    raw.close()
    with pytest.raises(upgrade.UpgradeError, match=f"{NEWEST + 1}"):
        upgrade.run(ctx)
    assert not any(k.startswith(("podman", "systemctl")) for k in host.steps())


def test_missing_config_file_is_refused(setup):
    ctx, _host, _out, _ = setup
    ctx.layout.config_file.unlink()
    with pytest.raises(upgrade.UpgradeError, match="container preflight"):
        upgrade.run(ctx)


def test_database_outside_the_mounted_data_dir_is_refused(setup, tmp_path):
    ctx, _host, _out, _ = setup
    ctx.db_path = tmp_path / "elsewhere.db"
    with pytest.raises(upgrade.UpgradeError, match="protect the wrong file"):
        upgrade.run(ctx)


def test_running_one_off_is_refused_before_build(setup):
    ctx, host, _out, _ = setup
    host.oneoffs = "abc123\n"
    with pytest.raises(upgrade.UpgradeError, match="one-off container is running"):
        upgrade.run(ctx)
    assert "podman build" not in host.steps()


def test_podman_unreachable_is_refused(setup):
    ctx, host, _out, _ = setup
    host._which = set()
    with pytest.raises(upgrade.UpgradeError, match="podman is not reachable"):
        upgrade.run(ctx)


def test_build_failure_changes_nothing(setup):
    ctx, host, out, _ = setup
    host.fail["podman build"] = Result(1, "", "error building")
    assert upgrade.run(ctx) == 1
    assert host.steps()[-1] == "podman build"
    assert "nothing was changed" in text(out)
    assert schema_of(ctx.db_path) == OLDER


def test_wait_timeout_restarts_timers_and_changes_nothing(setup):
    ctx, host, out, clock = setup
    host.running_services = ["jobhunter-ctr-run.service"]
    assert upgrade.run(ctx) == 1
    keys = host.steps()
    assert keys[-1] == "systemctl start"
    assert host.calls[-1] == ["systemctl", "--user", "start", *upgrade.TIMERS]
    assert keys.count("systemctl stop") == 1  # the timers only; the console never stopped
    assert "podman tag" not in keys and "podman run db migrate" not in keys
    assert clock.t >= 60
    assert "still running after 60 s: jobhunter-ctr-run.service" in text(out)
    assert not (ctx.data_dir / "backups").exists()


def test_waits_for_a_running_job_then_continues(setup):
    ctx, host, out, clock = setup
    real = host.run
    polls = {"n": 0}

    def run(argv, **kw):
        if FakeHost.key(list(argv)) == "systemctl list-units":
            polls["n"] += 1
            host.running_services = ["jobhunter-ctr-collect.service"] if polls["n"] < 3 else []
        return real(argv, **kw)

    host.run = run
    assert upgrade.run(ctx) == 0, text(out)
    assert polls["n"] == 3 and len(clock.slept) == 2
    assert "waiting for jobhunter-ctr-collect.service" in text(out)


def test_held_run_lock_restarts_timers_and_changes_nothing(setup):
    ctx, host, out, _ = setup
    other = RunLock(ctx.db_path, "run")
    other.acquire(None)
    try:
        assert upgrade.run(ctx) == 1
    finally:
        other.release()
    keys = host.steps()
    assert keys.count("systemctl stop") == 1
    assert host.calls[-1] == ["systemctl", "--user", "start", *upgrade.TIMERS]
    assert "holds the run lock" in text(out)
    assert "podman tag" not in keys


def test_only_units_that_were_active_are_stopped_and_started(setup):
    ctx, host, out, _ = setup
    host.active = {"jobhunter-ctr-run.timer"}
    assert upgrade.run(ctx) == 0, text(out)
    stops = [c for c in host.calls if c[2:3] == ["stop"]]
    starts = [c for c in host.calls if c[2:3] == ["start"]]
    assert stops == [["systemctl", "--user", "stop", "jobhunter-ctr-run.timer"]]
    assert starts == [["systemctl", "--user", "start", "jobhunter-ctr-run.timer"]]
    assert "jobhunter-console was not running before; not started" in text(out)


# step key that fails -> (retagged, migrate started)
FAILURES = {
    "stop-console": (False, False),
    "tag": (False, False),
    "migrate": (True, True),
    "verify-commit": (True, True),
    "verify-error": (True, True),
    "start-console": (True, True),
    "start-timers": (True, True),
}


def _arrange_failure(host: FakeHost, which: str) -> None:
    real = host.run
    if which == "tag":
        host.fail["podman tag"] = Result(125, "", "tag failed")
    elif which == "migrate":
        host.fail["podman run db migrate"] = Result(1, "", "MigrationFailed: boom")
    elif which == "verify-commit":
        host.image_commit = "f" * 40
    elif which == "verify-error":
        host.fail["podman run version --json"] = Result(1, "", "crashed")
    else:
        target = {
            "stop-console": ("stop", [upgrade.CONSOLE]),
            "start-console": ("start", [upgrade.CONSOLE]),
            "start-timers": ("start", list(upgrade.TIMERS)),
        }[which]

        def run(argv, **kw):
            if list(argv)[2:] == [target[0], *target[1]]:
                host.calls.append(list(argv))
                host.capture.append(True)
                return Result(1, "", f"Failed to {target[0]}")
            return real(argv, **kw)

        host.run = run


@pytest.mark.parametrize("which", list(FAILURES))
def test_failure_stops_everything_and_prints_the_exact_rollback(setup, which):
    ctx, host, out, _ = setup
    _arrange_failure(host, which)
    assert upgrade.run(ctx) == 1
    report = text(out)
    retagged, migrated = FAILURES[which]
    # left stopped: the last command stops the console and every timer
    assert host.calls[-1] == ["systemctl", "--user", "stop", upgrade.CONSOLE, *upgrade.TIMERS]
    assert "left stopped" in report
    assert "error: upgrade failed at step" in report
    retag_line = f"podman tag {OLD_IMAGE} {ctr.IMAGE}"
    assert (retag_line in report) is retagged
    backups = list((ctx.data_dir / "backups").glob("jobhunter-*-pre-upgrade.db"))
    assert len(backups) == (0 if which == "stop-console" else 1)
    assert ("jobhunter db restore" in report) is migrated
    if migrated:
        assert f"jobhunter db restore {backups[0]}" in report
    start_line = "systemctl --user start " + " ".join(UNITS)
    assert start_line in report
    if retagged:
        assert f"git checkout {OLD_REV}" in report
    else:
        assert "Nothing else changed" in report
    # nothing started after the failure
    keys = host.steps()
    if which in {"stop-console", "tag", "migrate", "verify-commit", "verify-error"}:
        assert "systemctl start" not in keys
    # the run lock was released even on failure
    assert RunLock(ctx.db_path, "probe").try_acquire()


def test_failed_migration_after_retag_never_starts_the_new_image(setup):
    ctx, host, _out, _ = setup
    _arrange_failure(host, "migrate")
    upgrade.run(ctx)
    assert host.after("podman run db migrate") == ["systemctl stop"]


def test_backup_failure_stops_before_retag(setup, monkeypatch):
    ctx, host, out, _ = setup

    def broken(conn, dest):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "online_backup", broken)
    assert upgrade.run(ctx) == 1
    assert "failed at step backup: disk full" in text(out)
    assert "podman tag" not in host.steps()
    assert "jobhunter db restore" not in text(out)


def test_first_upgrade_without_a_previous_image(setup):
    ctx, host, out, _ = setup
    host.previous = ""
    _arrange_failure(host, "migrate")
    assert upgrade.run(ctx) == 1
    assert f"no previous {ctr.IMAGE} existed" in text(out)


def test_new_database_is_not_backed_up(setup, tmp_path):
    ctx, _host, out, _ = setup
    ctx.db_path.unlink()
    assert upgrade.run(ctx) == 0, text(out)
    assert "backup: skipped" in text(out)
    assert not (ctx.data_dir / "backups").exists()


def test_systemctl_follows_the_host_escape_chain(setup):
    ctx, host, out, _ = setup
    host._which = {"flatpak-spawn"}
    assert upgrade.run(ctx) == 0, text(out)
    systemctl = [c for c in host.calls if "systemctl" in c]
    assert systemctl and all(c[:3] == ["flatpak-spawn", "--host", "systemctl"] for c in systemctl)
    build = next(c for c in host.calls if "build" in c)
    assert build[:3] == ["flatpak-spawn", "--host", "podman"]


def test_interrupt_mid_upgrade_still_leaves_things_stopped(setup):
    ctx, host, out, _ = setup
    real = host.run

    def run(argv, **kw):
        if FakeHost.key(list(argv)) == "podman run db migrate":
            raise KeyboardInterrupt
        return real(argv, **kw)

    host.run = run
    with pytest.raises(KeyboardInterrupt):
        upgrade.run(ctx)
    assert host.calls[-1] == ["systemctl", "--user", "stop", upgrade.CONSOLE, *upgrade.TIMERS]
    assert "jobhunter db restore" in text(out)
