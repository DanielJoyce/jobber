"""``jobhunter upgrade`` (specs/018-containers.md#version-skew, ticket C15).

In a container deployment (``<data dir>/deployment.toml`` exists) nothing migrates on its own,
so after a merge the host checkout is ahead of the image and of the database. This brings the
three back in step, on the host, in an order that never leaves an old console writing to a new
schema or a half-migrated database serving requests:

1. checks, which change nothing: a source checkout whose image inputs are clean (unless
   ``--allow-dirty``), a database not newer than this code, the config file the containers
   mount, podman reachable, no ``jobhunter-ctr`` one-off running;
2. build ``localhost/jobhunter:<commit>`` with ``BUILD_COMMIT``, while everything still runs
   (the units use ``:current``, so a new tag changes nothing yet; a failed build leaves
   everything as it was);
3. stop the timers, wait for running ``jobhunter-ctr-*`` services, take the run lock (C5),
   stop the console, take the console lock exclusive (C6, sees a console in any PID
   namespace) and check that no console process and no one-off is left; if any of this
   fails or is interrupted, what was stopped is started again and nothing has changed;
4. back up the database (C12 online backup, manual name, never pruned);
5. retag ``:current``, run ``jobhunter db migrate`` in a one-off container, check the image's
   ``jobhunter version`` against the host's (same commit, same highest migration, database
   current);
6. release the console lock, start the console and the timers that were running before,
   then print ``jobhunter version`` for the host and the image.

Any failure from step 4 on leaves the console and timers stopped and prints the exact
rollback: retag the previous image, ``jobhunter db restore`` the backup, start the units. It
also writes ``<data dir>/upgrade-pending.json``, so a rerun starts the units the first run
found active and keeps the first rollback point; a successful run removes it.
Every command goes through a :class:`~jobhunter.ops.proc.Runner`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from jobhunter.core import backup, db
from jobhunter.ops import ctr
from jobhunter.ops import version as ver
from jobhunter.ops.proc import Result, Runner
from jobhunter.xdg import mkdir_private

CONSOLE = "jobhunter-console"
TIMERS = ("jobhunter-ctr-run.timer", "jobhunter-ctr-collect.timer", "jobhunter-ctr-verify.timer")
SERVICE_PATTERN = "jobhunter-ctr-*.service"
# What goes into the image (Containerfile COPY lines) plus the build recipe itself. A change
# anywhere else in the checkout (notes, specs, tests) does not make the image differ from HEAD.
IMAGE_INPUTS = (
    "Containerfile",
    ".containerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
)
DIRTY_SUFFIX = "-dirty"
SHORT_LEN = 12
POLL_S = 5.0
BUILD_TIMEOUT_S = 3600.0
STEP_TIMEOUT_S = 300.0
MIGRATE_TIMEOUT_S = 1800.0
# Written when an upgrade fails after the console stopped, removed when one succeeds, so a
# rerun starts the units the first run found active and keeps the first rollback point.
PENDING_FILE = "upgrade-pending.json"


class UpgradeError(RuntimeError):
    """A check refused the upgrade before anything was changed."""


class Lock(Protocol):
    def try_acquire(self) -> bool: ...
    def release(self) -> None: ...


class ExclusiveLock(Protocol):
    def acquire(self) -> None: ...
    def release(self) -> None: ...


@dataclass
class Context:
    repo: Path
    db_path: Path
    data_dir: Path  # where the marker and backups live (the owning data dir)
    layout: ctr.Layout
    runner: Runner
    echo: Callable[[str], None]
    lock: Callable[[], Lock]
    wait_s: float = 1800.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = datetime.now
    host_info: Callable[[], ver.VersionInfo] | None = None
    # C6: held exclusive while no console may run (any PID namespace); and /proc checks.
    console_lock: Callable[[], ExclusiveLock] | None = None
    busy: Callable[[], list[str]] | None = None


@dataclass
class State:
    """What has happened so far: decides the rollback text."""

    podman: list[str] = field(default_factory=lambda: ["podman"])
    commit: str = ""
    tag: str = ""
    previous_image: str | None = None  # image id :current pointed at before
    previous_revision: str | None = None
    was_active: list[str] = field(default_factory=list)
    timers_stopped: list[str] = field(default_factory=list)
    console_stopped: bool = False
    previous_inspect_error: str | None = None  # podman image inspect failed for another reason
    backup_path: Path | None = None
    pending: dict | None = None  # an earlier failed upgrade's record (PENDING_FILE)
    db_had_schema: bool = False
    retagged: bool = False
    migrate_started: bool = False


class StepFailed(RuntimeError):
    def __init__(self, step: str, reason: str) -> None:
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


def _git(ctx: Context, *args: str) -> Result:
    return ctx.runner.run(["git", "-C", str(ctx.repo), *args], timeout=30)


def _shown(argv: Sequence[str]) -> str:
    import shlex

    return " ".join(shlex.quote(a) for a in argv)


def checkout_commit(ctx: Context, allow_dirty: bool) -> tuple[str, str, list[str]]:
    """``(BUILD_COMMIT, tag, dirty paths)``; raises :class:`UpgradeError` on a dirty checkout
    without ``allow_dirty`` or when the repo is not a git checkout."""
    head = _git(ctx, "rev-parse", "HEAD")
    short = _git(ctx, "rev-parse", f"--short={SHORT_LEN}", "HEAD")
    if not head.ok or not short.ok or not head.stdout.strip():
        raise UpgradeError(
            f"{ctx.repo} is not a git checkout of jobhunter ({head.tail() or 'git failed'}); "
            "jobhunter upgrade builds the image from the checkout the host CLI runs from"
        )
    status = _git(ctx, "status", "--porcelain", "--untracked-files=all", "--", *IMAGE_INPUTS)
    if not status.ok:
        raise UpgradeError(f"git status failed: {status.tail()}")
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    commit, tag = head.stdout.strip(), short.stdout.strip()
    if dirty:
        if not allow_dirty:
            listed = ", ".join(dirty[:5]) + (" ..." if len(dirty) > 5 else "")
            raise UpgradeError(
                f"the checkout has uncommitted changes in the image's inputs ({listed}); "
                "commit or stash them, or pass --allow-dirty to build them anyway (the image "
                f"is then tagged {tag}{DIRTY_SUFFIX} and matches no commit)"
            )
        commit, tag = commit + DIRTY_SUFFIX, tag + DIRTY_SUFFIX
    return commit, tag, dirty


def _check_database(ctx: Context) -> tuple[int | None, str]:
    schema, state, unknown, _ = ver.database_state(ctx.db_path)
    if state == "newer":
        raise UpgradeError(
            f"the database holds migration(s) {', '.join(map(str, unknown))} that this checkout "
            "does not have; check out the newer commit first (building this one would go back)"
        )
    if state == "unreadable":
        raise UpgradeError(f"cannot read the database schema of {ctx.db_path}")
    return schema, state


def _check_layout(ctx: Context) -> None:
    expected = ctx.layout.data_dir / "jobhunter.db"
    if Path(ctx.db_path).absolute() != expected.absolute():
        raise UpgradeError(
            f"the host database is {ctx.db_path}, but the containers use {expected} "
            "(/data/jobhunter.db); the run lock and the backup would protect the wrong file. "
            "Point [paths] db_path at the data directory's jobhunter.db"
        )
    if not ctx.layout.config_file.absolute().is_file():
        raise UpgradeError(
            f"{ctx.layout.config_file} does not exist and the containers mount it; "
            "run `jobhunter container preflight` (it creates an empty one)"
        )


def build_argv(state: State, repo: Path) -> list[str]:
    return [
        *state.podman,
        "build",
        "--build-arg",
        f"BUILD_COMMIT={state.commit}",
        "--tag",
        f"{ctx_image(state.tag)}",
        "--file",
        str(repo / "Containerfile"),
        str(repo),
    ]


def ctx_image(tag: str) -> str:
    return f"{ctr.IMAGE_REPO}:{tag}"


def _systemctl(state: State, *args: str) -> list[str]:
    return [*ctr.host_prefix(state.podman), "systemctl", "--user", *args]


def plan_lines(ctx: Context, state: State) -> list[str]:
    """What a real run would do, for ``--dry-run``."""
    sc = lambda *a: _shown(_systemctl(state, *a))  # noqa: E731
    pod = lambda *a: _shown([*state.podman, *a])  # noqa: E731
    backup_dest = _backup_dest(ctx)
    return [
        f"1. build:   {_shown(build_argv(state, ctx.repo))}",
        f"2. stop:    {sc('stop', *TIMERS)}   (only the timers that are active)",
        f"3. wait:    for running {SERVICE_PATTERN} (up to {ctx.wait_s:g} s; on timeout the "
        "timers are started again and nothing changes)",
        f"4. lock:    run lock {db_lock(ctx)} (not waiting)",
        f"5. stop:    {sc('stop', CONSOLE)}   (if active), then the console lock (exclusive), "
        "no console or jobhunter-ctr one-off may be running; else everything is started again "
        "and nothing changes",
        f"6. backup:  {backup_dest}",
        f"7. retag:   {pod('tag', ctx_image(state.tag), ctr.IMAGE)}",
        f"8. migrate: {_shown(ctr.run_argv(state.podman, ctx.layout, ['db', 'migrate']))}",
        f"9. verify:  {_shown(ctr.run_argv(state.podman, ctx.layout, ['version', '--json']))}"
        " (same commit, same highest migration, database current)",
        f"10. start:  {sc('start', CONSOLE, *TIMERS)}   (only what was active before)",
        "11. print jobhunter version for the host and the image",
    ]


def db_lock(ctx: Context) -> str:
    from jobhunter.core.runlock import lock_path

    return str(lock_path(ctx.db_path))


def _backup_dest(ctx: Context) -> Path:
    stamp = ctx.now().strftime("%Y%m%d-%H%M%S")
    return Path(ctx.data_dir) / "backups" / f"jobhunter-{stamp}-pre-upgrade.db"


class UnitStateUnknown(RuntimeError):
    """``systemctl is-active`` gave an answer that is neither active, inactive nor failed."""


ACTIVE_STATES = {"active", "reloading", "activating", "refreshing"}
INACTIVE_STATES = {"inactive", "failed", "deactivating"}


def _active(ctx: Context, state: State, unit: str) -> bool:
    """True or False from ``systemctl is-active``. Anything else (a D-Bus error, a failing
    escape chain) raises, so an error is never read as "not running". A unit that is not
    loaded at all prints "inactive", which is fine."""
    done = ctx.runner.run(_systemctl(state, "is-active", unit), timeout=STEP_TIMEOUT_S)
    word = done.stdout.strip().splitlines()[-1].strip() if done.stdout.strip() else ""
    if word in ACTIVE_STATES:
        return True
    if word in INACTIVE_STATES:
        return False
    raise UnitStateUnknown(
        f"systemctl --user is-active {unit} gave no usable answer "
        f"({word or done.tail() or f'exit {done.returncode}'})"
    )


def _running_services(ctx: Context, state: State) -> list[str]:
    done = ctx.runner.run(
        _systemctl(
            state,
            "list-units",
            "--type=service",
            "--state=activating,active,deactivating,reloading",
            "--plain",
            "--no-legend",
            SERVICE_PATTERN,
        ),
        timeout=STEP_TIMEOUT_S,
    )
    if not done.ok:
        raise StepFailed("wait", f"systemctl list-units failed: {done.tail()}")
    return [line.split()[0] for line in done.stdout.splitlines() if line.strip()]


def _oneoffs(ctx: Context, state: State) -> list[str]:
    done = ctx.runner.run(
        [*state.podman, "ps", "--filter", f"label={ctr.ONEOFF_LABEL}", "--format", "{{.ID}}"],
        timeout=STEP_TIMEOUT_S,
    )
    if not done.ok:
        raise UpgradeError(f"podman ps failed: {done.tail()}")
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _previous_image(ctx: Context, state: State) -> None:
    done = ctx.runner.run(
        [
            *state.podman,
            "image",
            "inspect",
            "--format",
            '{{.Id}} {{index .Labels "org.opencontainers.image.revision"}}',
            ctr.IMAGE,
        ],
        timeout=STEP_TIMEOUT_S,
    )
    if done.ok and done.stdout.split():
        parts = done.stdout.split()
        state.previous_image = parts[0]
        state.previous_revision = parts[1] if len(parts) > 1 and parts[1] != "<no" else None
        return
    said = (done.stderr + done.stdout).lower()
    if "no such image" not in said and "image not known" not in said:
        state.previous_inspect_error = done.tail() or f"exit {done.returncode}"


def _image_version(ctx: Context, state: State) -> dict:
    argv = ctr.run_argv(state.podman, ctx.layout, ["version", "--json"])
    done = ctx.runner.run(argv, timeout=STEP_TIMEOUT_S)
    if not done.ok:
        raise StepFailed("verify", f"`jobhunter version` in the image failed: {done.tail()}")
    try:
        start = done.stdout.index("{")
        info = json.loads(done.stdout[start:])
    except ValueError as exc:
        raise StepFailed("verify", "`jobhunter version --json` printed no JSON") from exc
    if not isinstance(info, dict):
        raise StepFailed("verify", "`jobhunter version --json` printed no object")
    return info


def _same_commit(host: str | None, image: str | None) -> bool:
    if not host or not image:
        return False
    return image.removesuffix(DIRTY_SUFFIX) == host.removesuffix(DIRTY_SUFFIX)


def verify(ctx: Context, state: State, host: ver.VersionInfo) -> dict:
    """The image runs the commit the host runs, ships the same migrations, and sees the
    database as current. Raises :class:`StepFailed` otherwise."""
    info = _image_version(ctx, state)
    problems = []
    if not _same_commit(host.commit or state.commit, info.get("commit")):
        problems.append(f"commit: host {host.commit or state.commit}, image {info.get('commit')}")
    if info.get("code_schema") != host.code_schema:
        problems.append(f"schema: host {host.code_schema}, image {info.get('code_schema')}")
    if info.get("db_state") != "current":
        problems.append(f"database seen from the image: {info.get('db_state')}")
    if problems:
        raise StepFailed("verify", "host and image disagree: " + "; ".join(problems))
    return info


# --- the record of a failed upgrade ---------------------------------------------------------


def _pending_path(ctx: Context) -> Path:
    return Path(ctx.data_dir) / PENDING_FILE


def _load_pending(ctx: Context) -> dict | None:
    try:
        data = json.loads(_pending_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_pending(ctx: Context, state: State, step: str) -> None:
    """Keep the first failed run's rollback point and active units across reruns."""
    first = state.pending or {}
    record = {
        "failed_at": step,
        "when": ctx.now().isoformat(timespec="seconds"),
        "was_active": state.was_active,
        "previous_image": first.get("previous_image") or state.previous_image,
        "previous_revision": first.get("previous_revision") or state.previous_revision,
        "backup_path": first.get("backup_path")
        or (str(state.backup_path) if state.backup_path else None),
        "migrated": bool(first.get("migrated")) or state.migrate_started,
    }
    path = _pending_path(ctx)
    try:
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        ctx.echo(f"warning: could not record the failed upgrade in {path}: {exc}")


def _apply_pending(ctx: Context, state: State) -> None:
    """A rerun after a failure: start what the first run found active, roll back to its point."""
    pending = state.pending
    if not pending:
        return
    ctx.echo(
        f"an earlier upgrade failed at step {pending.get('failed_at')} ({pending.get('when')}); "
        f"its record is {_pending_path(ctx)}"
    )
    earlier = [u for u in pending.get("was_active") or [] if u in (CONSOLE, *TIMERS)]
    state.was_active = [u for u in (CONSOLE, *TIMERS) if u in earlier or u in state.was_active]


def _rollback_image(state: State) -> tuple[str | None, str | None]:
    pending = state.pending or {}
    if pending.get("previous_image"):
        return pending["previous_image"], pending.get("previous_revision")
    return state.previous_image, state.previous_revision


def _rollback_backup(state: State) -> str | None:
    pending = state.pending or {}
    if pending.get("backup_path"):
        return pending["backup_path"]
    return str(state.backup_path) if state.backup_path else None


def rollback_lines(state: State, ctx: Context) -> list[str]:
    """Exact commands to put things back as they were before the (first failed) upgrade."""
    lines = ["Rollback (run on the host, in this order):"]
    steps: list[str] = []
    image, revision = _rollback_image(state)
    migrated = state.migrate_started or bool((state.pending or {}).get("migrated"))
    retagged = state.retagged or bool(state.pending)
    restore = None
    if migrated:
        saved = _rollback_backup(state)
        if saved:
            restore = (
                f"{_shown(['jobhunter', 'db', 'restore', saved])}   # the database before upgrade"
            )
        else:
            restore = (
                "no backup was needed (the database had no schema yet); a pre-migrate copy, if "
                f"any, is in {backup.auto_dir(ctx.data_dir)}"
            )
    if retagged and image:
        rev = f" (commit {revision})" if revision else ""
        steps.append(
            f"{_shown([*state.podman, 'tag', image, ctr.IMAGE])}   # the previous image{rev}"
        )
        if restore:
            steps.append(restore)
    elif retagged:
        # Restore first: an older checkout refuses a database that holds newer migrations.
        if restore:
            steps.append(restore)
        if state.previous_inspect_error:
            steps.append(
                f"the previous {ctr.IMAGE} could not be read before the upgrade "
                f"(podman image inspect: {state.previous_inspect_error}); find it with "
                f"podman images {ctr.IMAGE_REPO} and podman tag it {ctr.IMAGE}"
            )
        else:
            steps.append(
                f"no previous {ctr.IMAGE} existed: git checkout <last good commit>, then "
                "jobhunter upgrade"
            )
    elif restore:
        steps.append(restore)
    if state.was_active:
        steps.append(_shown(_systemctl(state, "start", *state.was_active)))
    else:
        steps.append("nothing was running before; start the units when ready")
    lines += [f"  {n}. {text}" for n, text in enumerate(steps, 1)]
    if retagged and revision:
        lines.append(
            f"Host commands then refuse until the checkout matches: git checkout "
            f"{revision.removesuffix(DIRTY_SUFFIX)}, or fix the cause and run jobhunter "
            "upgrade again (it starts the units listed above)."
        )
    else:
        lines.append(
            "Or fix the cause and run jobhunter upgrade again (it starts the units listed above)."
        )
    return lines


# --- quiesce and undo -----------------------------------------------------------------------


def _start(ctx: Context, state: State, units: list[str]) -> bool:
    if not units:
        return True
    done = ctx.runner.run(_systemctl(state, "start", *units), timeout=STEP_TIMEOUT_S)
    if done.ok:
        ctx.echo(f"started again: {' '.join(units)}")
        return True
    ctx.echo(
        f"warning: could not start {' '.join(units)} again ({done.tail()}); run: "
        f"{_shown(_systemctl(state, 'start', *units))}"
    )
    return False


def _undo_quiesce(ctx: Context, state: State) -> None:
    """Before anything changed: start again what this run stopped (console first)."""
    units = ([CONSOLE] if state.console_stopped else []) + state.timers_stopped
    _start(ctx, state, units)
    ctx.echo(f"nothing was changed: {ctr.IMAGE} and the database are as before.")


def _stop_everything(ctx: Context, state: State) -> bool:
    """Leave nothing half-running after a failure: console and timers stopped."""
    units = [CONSOLE, *TIMERS]
    done = ctx.runner.run(_systemctl(state, "stop", *units), timeout=STEP_TIMEOUT_S)
    if done.ok:
        ctx.echo("The console and the timers are left stopped.")
        return True
    ctx.echo(
        f"warning: could not confirm the console and timers stopped ({done.tail()}); check "
        f"{_shown(_systemctl(state, 'status', *units))} and stop them before the rollback."
    )
    return False


def _wait_for_services(ctx: Context, state: State) -> bool:
    deadline = ctx.monotonic() + ctx.wait_s
    told = False
    while True:
        try:
            running = _running_services(ctx, state)
        except StepFailed as exc:
            ctx.echo(f"error: {exc.reason}")
            return False
        if not running:
            return True
        if ctx.monotonic() >= deadline:
            ctx.echo(
                f"error: still running after {ctx.wait_s:g} s: {' '.join(running)}. Run "
                "jobhunter upgrade again when it finishes (or pass a longer --wait)."
            )
            return False
        if not told:
            ctx.echo(f"waiting for {' '.join(running)} to finish ...")
            told = True
        ctx.sleep(POLL_S)


def _quiesce(ctx: Context, state: State, lock: Lock, console_lock: ExclusiveLock) -> bool:
    """Stop timers, wait for jobs, take the run lock, stop the console, then make sure no
    console and no one-off is left. False when it cannot; nothing has changed yet."""
    timers = [t for t in TIMERS if t in state.was_active]
    if timers:
        done = ctx.runner.run(_systemctl(state, "stop", *timers), timeout=STEP_TIMEOUT_S)
        state.timers_stopped = timers  # a failed stop may still have stopped some
        if not done.ok:
            ctx.echo(f"error: could not stop the timers: {done.tail()}")
            return False
        ctx.echo(f"stopped: {' '.join(timers)}")
    if not _wait_for_services(ctx, state):
        return False
    try:
        got = lock.try_acquire()
    except Exception as exc:  # StaleHolder: a hung job
        ctx.echo(f"error: {exc}")
        got = False
    if not got:
        ctx.echo(
            "error: another jobhunter job holds the run lock. Run jobhunter upgrade again "
            "when it finishes."
        )
        return False
    if CONSOLE in state.was_active:
        done = ctx.runner.run(_systemctl(state, "stop", CONSOLE), timeout=STEP_TIMEOUT_S)
        state.console_stopped = True
        if not done.ok:
            ctx.echo(f"error: could not stop {CONSOLE}: {done.tail() or done.returncode}")
            return False
        ctx.echo(f"stopped: {CONSOLE}")
    from jobhunter.core.runlock import ConsoleBusy

    busy: list[str] = []
    try:
        console_lock.acquire()
    except ConsoleBusy as exc:
        busy.append(str(exc))
    busy += (ctx.busy or (lambda: _busy_report(ctx)))()
    try:
        running = _oneoffs(ctx, state)
    except UpgradeError as exc:
        busy.append(str(exc))
    else:
        busy += [f"jobhunter-ctr one-off container running: {cid}" for cid in running]
    if busy:
        ctx.echo("error: the database is still in use:")
        for line in busy:
            ctx.echo(f"  {line}")
        ctx.echo("Stop it (a host `jobhunter console`: Ctrl-C), then run jobhunter upgrade again.")
        return False
    return True


def _busy_report(ctx: Context) -> list[str]:
    from jobhunter.ops import restore

    return restore.busy_report(Path(ctx.db_path))


def _default_console_lock(ctx: Context) -> ExclusiveLock:
    from jobhunter.core.runlock import ConsoleLock

    return ConsoleLock(ctx.db_path, exclusive=True)


# --- the upgrade ----------------------------------------------------------------------------


def run(ctx: Context, *, allow_dirty: bool = False, dry_run: bool = False) -> int:
    """Run the upgrade; returns the exit code. Raises :class:`UpgradeError` for a refused
    check (nothing changed)."""
    state = State()
    state.commit, state.tag, dirty = checkout_commit(ctx, allow_dirty)
    if dirty:
        ctx.echo(f"warning: building uncommitted changes ({len(dirty)} path(s)), tag {state.tag}")
    schema, db_state = _check_database(ctx)
    state.db_had_schema = bool(schema)
    _check_layout(ctx)
    state.pending = _load_pending(ctx)
    if dry_run:
        ctx.echo(
            f"dry run: nothing is run. Would upgrade to {ctx_image(state.tag)} ({state.commit})."
        )
        ctx.echo(f"database: {ctx.db_path} ({db_state})")
        if state.pending:
            ctx.echo(
                f"an earlier upgrade failed at step {state.pending.get('failed_at')}; "
                f"see {_pending_path(ctx)}"
            )
        ctx.echo("podman and systemctl are reached through the host-escape chain when not on PATH.")
        for line in plan_lines(ctx, state):
            ctx.echo(line)
        return 0

    try:
        state.podman = ctr.find_podman(ctx.runner)
    except ctr.CtrError as exc:
        raise UpgradeError(str(exc)) from exc
    if running := _oneoffs(ctx, state):
        raise UpgradeError(
            f"a jobhunter-ctr one-off container is running ({', '.join(running)}); "
            "let it finish, then run jobhunter upgrade again"
        )
    # podman refuses a missing bind source; the units' ExecStartPre would create these.
    try:
        mkdir_private(ctx.layout.data_dir)
        mkdir_private(ctx.layout.cache_dir)
    except OSError as exc:
        raise UpgradeError(f"cannot create the container bind sources: {exc}") from exc
    _previous_image(ctx, state)

    # 1. Build while everything still runs: the units use :current, not this tag.
    ctx.echo(f"building {ctx_image(state.tag)} (BUILD_COMMIT={state.commit}) ...")
    built = ctx.runner.run(build_argv(state, ctx.repo), capture=False, timeout=BUILD_TIMEOUT_S)
    if not built.ok:
        ctx.echo(f"error: podman build failed (exit {built.returncode}); nothing was changed.")
        return 1

    try:
        state.was_active = [u for u in (CONSOLE, *TIMERS) if _active(ctx, state, u)]
    except UnitStateUnknown as exc:
        raise UpgradeError(str(exc)) from exc
    _apply_pending(ctx, state)

    # 2-5. Quiesce. Until it is done, any failure or interrupt starts again what was stopped.
    lock = ctx.lock()
    console_lock = (ctx.console_lock or (lambda: _default_console_lock(ctx)))()
    try:
        try:
            quiet = _quiesce(ctx, state, lock, console_lock)
        except BaseException:
            ctx.echo("interrupted before anything was changed.")
            console_lock.release()
            _undo_quiesce(ctx, state)
            raise
        if not quiet:
            console_lock.release()
            _undo_quiesce(ctx, state)
            return 1
        return _locked_steps(ctx, state, console_lock)
    finally:
        console_lock.release()
        lock.release()


def _locked_steps(ctx: Context, state: State, console_lock: ExclusiveLock) -> int:
    step = "backup"
    try:
        state.backup_path = _take_backup(ctx, state)

        step = "retag"
        done = ctx.runner.run(
            [*state.podman, "tag", ctx_image(state.tag), ctr.IMAGE], timeout=STEP_TIMEOUT_S
        )
        if not done.ok:
            raise StepFailed(step, done.tail() or f"exit {done.returncode}")
        state.retagged = True
        ctx.echo(f"tagged: {ctx_image(state.tag)} -> {ctr.IMAGE}")

        step = "migrate"
        state.migrate_started = True
        done = ctx.runner.run(
            ctr.run_argv(state.podman, ctx.layout, ["db", "migrate"]), timeout=MIGRATE_TIMEOUT_S
        )
        for line in (done.stdout + done.stderr).splitlines():
            if line.strip():
                ctx.echo(f"  {line}")
        if not done.ok:
            raise StepFailed(step, f"jobhunter db migrate in the image exited {done.returncode}")

        step = "verify"
        host = (ctx.host_info or (lambda: _host_info(ctx)))()
        image = verify(ctx, state, host)

        step = "start"
        console_lock.release()  # the console takes it shared when it starts
        if CONSOLE in state.was_active:
            done = ctx.runner.run(_systemctl(state, "start", CONSOLE), timeout=STEP_TIMEOUT_S)
            if not done.ok:
                raise StepFailed(step, f"{CONSOLE} did not start: {done.tail()}")
            ctx.echo(f"started: {CONSOLE}")
        else:
            ctx.echo(f"{CONSOLE} was not running before; not started")
        timers = [t for t in TIMERS if t in state.was_active]
        if timers:
            done = ctx.runner.run(_systemctl(state, "start", *timers), timeout=STEP_TIMEOUT_S)
            if not done.ok:
                raise StepFailed(step, f"timers did not start: {done.tail()}")
            ctx.echo(f"started: {' '.join(timers)}")
        else:
            ctx.echo("no timers were running before; none started")
    except (Exception, KeyboardInterrupt) as exc:
        reason = exc.reason if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}"
        ctx.echo(f"error: upgrade failed at step {step}: {reason}")
        _stop_everything(ctx, state)
        if not state.retagged and not state.migrate_started and not state.pending:
            ctx.echo(f"Nothing else changed: {ctr.IMAGE} and the database are as before.")
        for line in rollback_lines(state, ctx):
            ctx.echo(line)
        _save_pending(ctx, state, step)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return 1

    _pending_path(ctx).unlink(missing_ok=True)
    ctx.echo("upgrade done.")
    ctx.echo("host:")
    for line in host.lines():
        ctx.echo(f"  {line}")
    ctx.echo("image:")
    for line in _image_lines(image):
        ctx.echo(f"  {line}")
    return 0


def _image_lines(info: dict) -> list[str]:
    try:
        return ver.VersionInfo(**info).lines()
    except TypeError:
        return [json.dumps(info)]


def _host_info(ctx: Context) -> ver.VersionInfo:
    return ver.collect(ctx.db_path, ctx.data_dir)


def _take_backup(ctx: Context, state: State) -> Path | None:
    """C12 online backup under a manual name; None for a missing or schema-less database."""
    if not Path(ctx.db_path).is_file() or not state.db_had_schema:
        ctx.echo("backup: skipped (no database schema yet)")
        return None
    dest = _backup_dest(ctx)
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        db.check_not_newer(conn)
        backup.online_backup(conn, dest)
    except (sqlite3.Error, backup.BackupError, db.SchemaError) as exc:
        raise StepFailed("backup", str(exc)) from exc
    finally:
        conn.close()
    ctx.echo(f"backup: {dest}")
    return dest
