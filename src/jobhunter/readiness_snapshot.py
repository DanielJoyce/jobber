"""``<data dir>/readiness.json``: what the nightly run and other processes saw (specs/019 S4).

The console and the nightly run do not share an environment, so each writer records what it
sees in its own section and the console merges them into each check's **effective status**:

- ``run_inputs``: written by ``jobhunter run`` after it takes the run lock: each input check's
  id, status and fixed message, the mode and the trigger.
- ``run_outcome``: written by ``jobhunter run`` at the end: the score stage's fixed code and
  each failed source's mapped reason. A ``not_run`` outcome keeps the earlier score result.
- ``manual_inputs``: written by ``jobhunter doctor --snapshot`` (or ``jobhunter-ctr doctor
  --snapshot``); fills rows the console cannot see, never stands in for the nightly run.
- ``host``: written on the host by ``jobhunter doctor --containers --snapshot``: preflight's
  mapped rows, shown by the console container on its Containers row.

Every writer takes ``readiness.json.lock`` (``flock``), reads the file, replaces only its own
section and renames a 0600 temp file into place, so no writer erases another's section. A lock
it cannot get within the timeout means nothing is written.

Nothing here holds a value: rows are ``readiness.Check`` dicts (fixed wording by rule 3) and
outcomes are fixed codes. Reading never raises: a missing or damaged file is an empty snapshot.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunter import readiness as rd

FILE_NAME = "readiness.json"
LOCK_NAME = "readiness.json.lock"
VERSION = 1
FRESH = timedelta(hours=36)
LOCK_TIMEOUT_S = 5.0
_POLL_S = 0.05

RUN_INPUTS = "run_inputs"
RUN_OUTCOME = "run_outcome"
MANUAL_INPUTS = "manual_inputs"
HOST = "host"
SECTIONS = (RUN_INPUTS, RUN_OUTCOME, MANUAL_INPUTS, HOST)

TRIGGER_VAR = "JOBHUNTER_TRIGGER"
TIMER = "timer"
MANUAL = "manual"

# Score outcome codes (019 "Run outcome"); a failure is "failed:<exception type>".
SCORED = "scored"
SKIPPED_NO_CREDENTIAL = "skipped_no_credential"
CAP_REACHED = "cap_reached"
NO_PROFILE = "no_profile"
NOT_RUN = "not_run"
FAILED_PREFIX = "failed:"

# The input rows a snapshot carries and the console merges. Rows about the database's history
# (last run, schedule, labels, backups) or the console itself are left out: the run sees them
# before it runs, so its view is older than the console's, never a second opinion.
INPUT_IDS = (
    "config.file",
    "fetch.contact",
    "data.permissions",
    "profile.preferences",
    "profile.resume",
    "profile.completeness",
    "scorer.screen",
    "scorer.caps",
    "scorer.deep",
    "source.usajobs",
    "mail.client",
    "mail.address",
    "mail.token",
    "secrets.valid",
)

CONFIRM_SCORE = (
    "or confirm now with `{cmd} run --stage prefilter,score`. This scores waiting jobs and "
    "spends credits, up to the daily cap"
)

_RANK = {rd.OK: 0, rd.OPTIONAL_STATUS: 0, rd.MISSING: 1, rd.BROKEN: 2}


class LockTimeout(Exception):
    """Another writer held ``readiness.json.lock`` for longer than the timeout."""


# --- trigger -----------------------------------------------------------------------------------


def current_trigger(environ: Mapping[str, str] | None = None) -> str:
    """``timer`` when the unit set ``JOBHUNTER_TRIGGER=timer``; ``manual`` otherwise."""
    env = os.environ if environ is None else environ
    return TIMER if env.get(TRIGGER_VAR, "").strip().lower() == TIMER else MANUAL


# --- file ------------------------------------------------------------------------------------


def path_for(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def read(data_dir: Path) -> dict[str, Any]:
    """The snapshot, or ``{}`` when it is missing, unreadable or not an object."""
    try:
        with path_for(data_dir).open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def _locked(data_dir: Path, timeout: float) -> Iterator[None]:
    lock = Path(data_dir) / LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(str(lock)) from None
                time.sleep(_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_section(
    data_dir: Path,
    name: str,
    build: Mapping[str, Any] | Callable[[dict[str, Any] | None], Mapping[str, Any]],
    *,
    timeout: float = LOCK_TIMEOUT_S,
) -> bool:
    """Replace one section under the lock; the others are kept as they are on disk.

    ``build`` is the new section, or a function of the current one (``None`` when absent).
    Returns False, having written nothing, when the lock was not free within ``timeout``."""
    if name not in SECTIONS:
        raise ValueError(f"unknown section {name!r}")
    data_dir = Path(data_dir)
    try:
        with _locked(data_dir, timeout):
            data = read(data_dir)
            current = data.get(name)
            section = (
                build(current if isinstance(current, dict) else None) if callable(build) else build
            )
            data = {k: v for k, v in data.items() if k in SECTIONS}
            data[name] = dict(section)
            data["version"] = VERSION
            target = path_for(data_dir)
            tmp = target.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=1, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
    except LockTimeout:
        return False
    return True


# --- building sections ---------------------------------------------------------------------


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _row(check: rd.Check) -> dict[str, Any]:
    return check.as_dict()


def inputs_section(
    checks: Iterable[rd.Check], *, now: datetime, writer: str, trigger: str, mode: str
) -> dict[str, Any]:
    """``run_inputs`` or ``manual_inputs``: the input rows as this process saw them."""
    return {
        "checked_at": _iso(now),
        "writer": writer,
        "trigger": trigger,
        "mode": mode,
        "checks": [_row(c) for c in checks if c.id in INPUT_IDS],
    }


def host_section(checks: Iterable[rd.Check], *, now: datetime) -> dict[str, Any]:
    """``host``: preflight's mapped rows (fixed ids and messages only)."""
    return {
        "checked_at": _iso(now),
        "writer": "doctor --containers",
        "trigger": MANUAL,
        "checks": [_row(c) for c in checks if c.id.startswith("preflight.")],
    }


def outcome_section(
    previous: Mapping[str, Any] | None,
    *,
    now: datetime,
    trigger: str,
    run_id: int | None,
    score: str,
    sources: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """``run_outcome``. ``not_run`` keeps the earlier score result (with its time and trigger),
    so a partial manual run does not hide that the last real score stage was skipped."""
    section: dict[str, Any] = {
        "checked_at": _iso(now),
        "writer": "run",
        "trigger": trigger,
        "run_id": run_id,
        "score": score,
        "score_at": _iso(now),
        "score_trigger": trigger,
        "sources": [{"source": key, "reason": reason} for key, reason in sources],
    }
    if score == NOT_RUN and previous and previous.get("score") not in (None, NOT_RUN):
        for key in ("score", "score_at", "score_trigger"):
            if key in previous:
                section[key] = previous[key]
    return section


# --- reading sections ----------------------------------------------------------------------


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fresh(section: Any, now: datetime, key: str = "checked_at") -> datetime | None:
    """The section's time when it was written within 36 hours (and not in the future by more
    than a clock-skew minute); else None."""
    if not isinstance(section, dict):
        return None
    when = _parse(section.get(key))
    if when is None:
        return None
    if now - when > FRESH or when - now > timedelta(minutes=1):
        return None
    return when


_CHECK_FIELDS = {f.name for f in fields(rd.Check)}


def _fix_from(data: Any) -> rd.Fix | None:
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    steps = data.get("steps") or ()
    return rd.Fix(
        str(data.get("kind") or "command"),
        data["text"],
        tuple(str(s) for s in steps if isinstance(s, str)),
        _fix_from(data.get("alt")),
    )


def _check_from(data: Any) -> rd.Check | None:
    if not isinstance(data, dict):
        return None
    try:
        kwargs = {k: v for k, v in data.items() if k in _CHECK_FIELDS}
        kwargs["fix"] = _fix_from(data.get("fix"))
        check = rd.Check(**kwargs)
    except TypeError:
        return None
    if not isinstance(check.id, str) or (check.status not in _RANK and check.status != rd.UNKNOWN):
        return None
    return check


def _rows(section: Any) -> dict[str, rd.Check]:
    if not isinstance(section, dict) or not isinstance(section.get("checks"), list):
        return {}
    out: dict[str, rd.Check] = {}
    for item in section["checks"]:
        check = _check_from(item)
        if check is not None:
            out[check.id] = check
    return out


def _clock(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


def run_label(trigger: Any, when: datetime) -> str:
    """ "the nightly run at 02:00" only for timer data; "the manual run at 14:03" otherwise."""
    if trigger == TIMER:
        return f"the nightly run at {_clock(when)}"
    if trigger == MANUAL:
        return f"the manual run at {_clock(when)}"
    return f"the run at {_clock(when)}"


def manual_label(section: Mapping[str, Any], when: datetime) -> str:
    if section.get("writer") == "jobhunter-ctr":
        return f"as seen from `jobhunter-ctr` at {_clock(when)}"
    return f"as seen from your shell at {_clock(when)}"


# --- effective status ----------------------------------------------------------------------


def _confirm_fix(fix: rd.Fix | None, mode: str) -> rd.Fix:
    cmd = "jobhunter-ctr" if mode in (rd.CONTAINER, rd.HOST_MANAGING) else "jobhunter"
    confirm = rd.Fix("command", CONFIRM_SCORE.format(cmd=cmd))
    if fix is None:
        return confirm
    alt = fix.alt
    if alt is None:
        return replace(fix, alt=confirm)
    return replace(fix, alt=replace(alt, alt=confirm))


def _outcome_override(
    check: rd.Check,
    snapshot: Mapping[str, Any],
    now: datetime,
    mode: str,
) -> rd.Check | None:
    """A fresh ``skipped_no_credential`` or ``no_profile`` breaks the scorer or profile row
    whatever this process sees, until a newer ``run_inputs`` shows the row ok."""
    outcome = snapshot.get(RUN_OUTCOME)
    score_at = fresh(outcome, now, "score_at")
    if score_at is None or not isinstance(outcome, dict):
        return None
    code = outcome.get("score")
    if code == SKIPPED_NO_CREDENTIAL and check.id == "scorer.screen":
        cleared_by = ("scorer.screen",)
        why = "did not score: no credential for the screen scorer"
    elif code == NO_PROFILE and check.id == "profile.preferences":
        cleared_by = ("profile.preferences", "profile.resume")
        why = "did not score: the profile (preferences and resume) could not be loaded"
    else:
        return None
    inputs = snapshot.get(RUN_INPUTS)
    inputs_at = fresh(inputs, now)
    if inputs_at is not None and inputs_at > score_at:
        rows = _rows(inputs)
        if all(rows.get(i) is not None and rows[i].status == rd.OK for i in cleared_by):
            return None
    label = run_label(outcome.get("score_trigger"), score_at)
    return replace(
        check,
        status=rd.BROKEN,
        due=False,
        detail=f"{label} {why}; here: {check.detail}",
        fix=_confirm_fix(check.fix, mode),
    )


def _from_snapshot(check: rd.Check, snapshot: Mapping[str, Any], now: datetime) -> rd.Check:
    """A row this process cannot see: the newer of a fresh ``run_inputs`` or ``manual_inputs``
    row that is not itself unknown; else the row as it is (unknown)."""
    best: tuple[datetime, rd.Check, str] | None = None
    for name in (RUN_INPUTS, MANUAL_INPUTS):
        section = snapshot.get(name)
        when = fresh(section, now)
        if when is None or not isinstance(section, dict):
            continue
        row = _rows(section).get(check.id)
        if row is None or row.status == rd.UNKNOWN:
            continue
        if name == RUN_INPUTS:
            label = f"as seen by {run_label(section.get('trigger'), when)}"
        else:
            label = manual_label(section, when)
        if best is None or when > best[0]:
            best = (when, row, label)
    if best is None:
        return check
    _, row, label = best
    return replace(
        check,
        status=row.status,
        due=row.due,
        detail=f"{row.detail} ({label})",
        fix=row.fix if row.status in rd.FAILING or row.due else None,
        source=row.source or check.source,
    )


def _worse_of(check: rd.Check, snapshot: Mapping[str, Any], now: datetime, viewer: str) -> rd.Check:
    """A row both can see: the worse of this process's view and a fresh ``run_inputs``."""
    section = snapshot.get(RUN_INPUTS)
    when = fresh(section, now)
    if when is None or not isinstance(section, dict):
        return check
    row = _rows(section).get(check.id)
    if row is None or row.status == rd.UNKNOWN:
        return check
    if _RANK.get(row.status, 0) <= _RANK.get(check.status, 0):
        return check
    label = run_label(section.get("trigger"), when)
    return replace(
        check,
        status=row.status,
        due=False,
        detail=f"{viewer} sees it as {check.status} ({check.detail}); {label} did not: "
        f"{row.detail}",
        fix=row.fix or check.fix,
    )


def _host_row(check: rd.Check, snapshot: Mapping[str, Any], now: datetime) -> rd.Check:
    """The Containers row from a fresh ``host`` section (preflight's mapped rows)."""
    section = snapshot.get(HOST)
    when = fresh(section, now)
    if when is None or not isinstance(section, dict):
        return check
    rows = list(_rows(section).values())
    if not rows:
        return check
    bad = [r for r in rows if r.status in rd.FAILING]
    at = f"{when.astimezone().strftime('%Y-%m-%d %H:%M')}"
    if not bad:
        return replace(check, status=rd.OK, detail=f"the host's preflight at {at} passed",
                       fix=None)  # fmt: skip
    level = rd.ESSENTIAL if any(r.level == rd.ESSENTIAL for r in bad) else rd.RECOMMENDED
    titles = ", ".join(r.title for r in bad)
    return replace(
        check,
        level=level,
        status=rd.BROKEN,
        detail=f"the host's preflight at {at} found problems: {titles}",
        fix=rd.Fix("command", "jobhunter container preflight --dry-run (on the host)"),
    )


def effective(
    checks: Iterable[rd.Check],
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    mode: str,
    viewer: str = "the console",
) -> list[rd.Check]:
    """Each check's effective status (019 "Effective status"). Unknown rows stay unknown
    unless a fresh section settles them; nothing here turns a row unknown."""
    out: list[rd.Check] = []
    for check in checks:
        if check.id in INPUT_IDS:
            if check.status == rd.UNKNOWN:
                check = _from_snapshot(check, snapshot, now)
            else:
                check = _worse_of(check, snapshot, now, viewer)
            override = _outcome_override(check, snapshot, now, mode)
            if override is not None:
                check = override
        elif check.id == "containers.deployment" and check.status == rd.UNKNOWN:
            check = _host_row(check, snapshot, now)
        out.append(check)
    return out


def apply(
    checks: list[rd.Check],
    data_dir: Path,
    *,
    now: datetime,
    mode: str,
    viewer: str = "the console",
) -> list[rd.Check]:
    """``effective`` over the snapshot on disk; the checks unchanged when there is none."""
    snapshot = read(data_dir)
    if not snapshot:
        return checks
    return effective(checks, snapshot, now=now, mode=mode, viewer=viewer)


# --- writers ---------------------------------------------------------------------------------


def _data_dir(settings: Any) -> Path:
    from jobhunter.config import resolve_path

    return resolve_path(settings.paths.data_dir)


def record_run_inputs(
    settings: Any,
    *,
    secrets_report: Any,
    registry_rows: Iterable[Any] | None,
    now: datetime,
    trigger: str,
    environ: Mapping[str, str] | None = None,
    timeout: float = LOCK_TIMEOUT_S,
) -> bool:
    """``run`` writes what it sees, after it holds the run lock. Its own view, not merged."""
    mode = rd.detect_mode(settings=settings)
    checks = rd.run_checks(
        settings,
        secrets_report=secrets_report,
        now=now,
        mode=mode,
        registry_rows=registry_rows,
        environ=environ,
        use_snapshot=False,
    )
    section = inputs_section(checks, now=now, writer="run", trigger=trigger, mode=mode)
    return write_section(_data_dir(settings), RUN_INPUTS, section, timeout=timeout)


def source_reasons(results: Iterable[Any]) -> list[tuple[str, str]]:
    """Each failed source's mapped reason (019 rule 4): fixed wording, or the exception type."""
    out: list[tuple[str, str]] = []
    for res in results:
        if not getattr(res, "failed", False):
            continue
        error = getattr(res, "error", None)
        mapped = rd.map_error_text(error) or f"{rd._exception_type(error)} (see /sources)"
        out.append((str(res.key), mapped))
    return out


def record_run_outcome(
    settings: Any, report: Any, *, now: datetime, timeout: float = LOCK_TIMEOUT_S
) -> bool:
    """``run`` writes its outcome at the end; ``not_run`` keeps the earlier score result."""
    sources = source_reasons(report.non_ok)

    def build(previous: dict[str, Any] | None) -> dict[str, Any]:
        return outcome_section(
            previous,
            now=now,
            trigger=report.trigger,
            run_id=report.run_id,
            score=report.score_outcome,
            sources=sources,
        )

    return write_section(_data_dir(settings), RUN_OUTCOME, build, timeout=timeout)


def record_manual_inputs(
    checks: Iterable[rd.Check],
    settings: Any,
    *,
    now: datetime,
    mode: str,
    timeout: float = LOCK_TIMEOUT_S,
) -> bool:
    """``doctor --snapshot``: never touches ``run_inputs``."""
    from jobhunter import container

    writer = "jobhunter-ctr" if container.is_container() else "shell"
    section = inputs_section(checks, now=now, writer=writer, trigger=MANUAL, mode=mode)
    return write_section(_data_dir(settings), MANUAL_INPUTS, section, timeout=timeout)


def record_host(
    checks: Iterable[rd.Check], settings: Any, *, now: datetime, timeout: float = LOCK_TIMEOUT_S
) -> bool:
    """``doctor --containers --snapshot`` on the host."""
    return write_section(_data_dir(settings), HOST, host_section(checks, now=now), timeout=timeout)
