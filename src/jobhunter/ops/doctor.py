"""``jobhunter doctor``: the readiness checks in the terminal (specs/019, ticket S2).

This module gathers, renders and serialises; ``readiness.run_checks`` computes (019 rule 5).
The command itself is in ``cli.py``.

Read-only. The database is opened through a read-only connection and never migrated, written or
backed up. SQLite may still create ``jobhunter.db-wal`` and ``jobhunter.db-shm`` next to a WAL
database in order to read it; they hold no new data. Plain doctor runs no subprocess other
than ``legacy_data.check``'s ``git rev-parse``. ``--containers`` starts one container (through
preflight's dry run), so it is explicit-only.

No output carries a secret, an address or a contact string: ``Check`` rows already hold fixed
wording, a config file that will not load is reported by exception type only, and preflight's
free-form lines (podman and quadlet stderr) reach the terminal only; ``--json`` carries the
mapped rows with fixed messages.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobhunter import legacy_data, readiness
from jobhunter.config import Settings, load_settings
from jobhunter.ops import preflight
from jobhunter.readiness import (
    BASICS,
    BROKEN,
    ESSENTIAL,
    FAILING,
    OK,
    RECOMMENDED,
    RUNNING,
    UNKNOWN,
    Check,
    Fix,
)

WAL_NOTE = (
    "The database is opened read-only and never migrated. SQLite may create jobhunter.db-wal "
    "and jobhunter.db-shm beside it to read it; they hold no new data."
)

PREFLIGHT_OTHER = "preflight_other"

PREFLIGHT_TITLES = {
    "podman": "Podman",
    "bind_sources": "Bind-mount sources",
    "owner": "File ownership",
    "labels": "SELinux labels",
    "host_timers": "Host timers",
    "db_paths": "Stored paths",
    "version_skew": "Host and image versions",
    "env_files": ".env files",
    "mounted_config": "Mounted config file",
    "secrets": "Podman secrets and drop-ins",
    "dropin_conflict": "Drop-in conflicts",
    "base_units": "Base units",
    "quadlet": "Quadlet dry run",
    "marker": "Deployment marker",
    PREFLIGHT_OTHER: "Other preflight findings",
}
PREFLIGHT_DETAIL = {
    preflight.OK: "passed",
    preflight.WARN: "needs attention; run `jobhunter container preflight --dry-run` for details",
    preflight.FAIL: "failed; run `jobhunter container preflight --dry-run` for details",
}
_SEVERITY = {preflight.OK: 0, preflight.WARN: 1, preflight.FAIL: 2}

LABELS = {"missing": "MISSING", "broken": "BROKEN", "unknown": "unknown", "ok": "ok",
          "optional": "optional"}  # fmt: skip


class UsageError(ValueError):
    """A bad option value; the command prints it and exits 2."""


@dataclass
class Report:
    checks: list[Check]
    mode: str
    preflight_lines: list[preflight.Line] = field(default_factory=list)
    preflight_ran: bool = False
    group: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if readiness.essentials_ok(self.checks) else 1


def normalize_group(name: str) -> str:
    """The canonical group name for ``--group`` (case, ``-`` and ``_`` are not significant)."""
    key = name.strip().lower().replace("-", " ").replace("_", " ")
    for group in readiness.GROUPS:
        if group.lower() == key:
            return group
    raise UsageError(
        f"unknown group {name!r}; choose one of: {', '.join(g.lower() for g in readiness.GROUPS)}"
    )


def _tilde(path: Path, home: Path) -> str:
    text = str(path)
    for h in dict.fromkeys((str(home), os.path.realpath(home))):
        if text == h:
            return "~"
        if text.startswith(h + "/"):
            return "~" + text[len(h) :]
    return text


def layout_check(settings: Settings, home: Path) -> Check:
    """Doctor-only row: data left in the old repo-relative layout (``legacy_data.check``).

    Runs ``git rev-parse`` in a subprocess, which is why ``/setup`` cannot show it."""
    title, unlocks = "Data location", "Every command (the old layout blocks them)"
    status = legacy_data.check(settings)
    fix = Fix("command", "jobhunter migrate-paths")
    if status.blocked:
        old = ", ".join(_tilde(p, home) for p in status.unmigrated)
        detail = (
            f"your data is still in {old}; nothing else will run until it is moved"
            if status.kind == legacy_data.UNMIGRATED
            else f"two databases: the configured one and {old}; jobhunter will not pick one"
        )
        return Check("data.layout", BASICS, ESSENTIAL, BROKEN, False, title, detail, unlocks, fix)
    notes = status.warnings
    detail = (
        "an old database is still on disk; see `jobhunter paths`" if notes else "current layout"
    )
    return Check("data.layout", BASICS, ESSENTIAL, OK, False, title, detail, unlocks)


def _config_failed(exc: Exception) -> Check:
    return Check(
        "config.file", BASICS, ESSENTIAL, BROKEN, False, "Settings file",
        f"the config file will not load ({type(exc).__name__}); fix it with `jobhunter paths` "
        "to find the file",
        "Every command and the console", Fix("command", "jobhunter paths"), "config.toml",
        reads_at_start=True,
    )  # fmt: skip


def map_preflight(result: preflight.Result) -> list[Check]:
    """One ``preflight.<id>`` row per id: the worst line wins; the message is fixed.

    WARN and FAIL are both ``broken``. Essential only for a FAIL while the deployment marker
    exists (the units will not start); otherwise recommended. A line without a known id goes
    to ``preflight_other`` with its severity kept."""
    worst: dict[str, str] = {}
    for line in result.lines:
        pid = line.id if line.id in preflight.PREFLIGHT_IDS else PREFLIGHT_OTHER
        if pid not in worst or _SEVERITY[line.status] > _SEVERITY[worst[pid]]:
            worst[pid] = line.status
    order = [*preflight.PREFLIGHT_IDS, PREFLIGHT_OTHER]
    rows = []
    for pid in order:
        if pid not in worst:
            continue
        status = worst[pid]
        failing_ = status != preflight.OK
        level = ESSENTIAL if status == preflight.FAIL and result.marker_present else RECOMMENDED
        rows.append(
            Check(
                f"preflight.{pid}", RUNNING, level, BROKEN if failing_ else OK, False,
                PREFLIGHT_TITLES[pid], PREFLIGHT_DETAIL[status], "The container deployment",
                Fix("command", "jobhunter container preflight --dry-run") if failing_ else None,
            )
        )  # fmt: skip
    return rows


def gather(
    *,
    now: datetime | None = None,
    group: str | None = None,
    containers: bool = False,
    secrets_report=None,
    settings_loader: Callable[[], Settings] = load_settings,
    context_builder: Callable[[], preflight.Context] | None = None,
    run_preflight: Callable[..., preflight.Result] | None = None,
    home: Path | None = None,
) -> Report:
    """Every local check plus ``data.layout``, and with ``containers`` preflight's mapped rows.

    A config file that will not load becomes a broken ``config.file`` row (exception type only)
    and the other checks run against code defaults."""
    home = home or Path.home()
    now = now or datetime.now(UTC)
    wanted = normalize_group(group) if group else None
    config_error: Exception | None = None
    try:
        settings = settings_loader()
    except Exception as exc:  # any load failure: reported, never traced
        config_error = exc
        settings = load_settings(Path("/nonexistent/jobhunter-no-config.toml"))
    mode = readiness.detect_mode(settings=settings)
    checks = readiness.run_checks(
        settings, secrets_report=secrets_report, now=now, mode=mode, home=home
    )
    if config_error is not None:
        checks = [_config_failed(config_error) if c.id == "config.file" else c for c in checks]
    checks.insert(1, layout_check(settings, home))

    report = Report(checks, mode, group=wanted)
    if containers:
        if context_builder is None:
            raise RuntimeError("--containers needs a preflight context builder")
        result = (run_preflight or preflight.run)(context_builder(), dry_run=True)
        report.preflight_ran = True
        report.preflight_lines = list(result.lines)
        mapped = map_preflight(result)
        # The placeholder row said "run doctor --containers"; the real rows replace it.
        checks = [
            c for c in checks if not (c.id == "containers.deployment" and c.status == UNKNOWN)
        ]
        report.checks = [*checks, *mapped]
    if wanted:
        report.checks = [c for c in report.checks if c.group == wanted]
    return report


# --- rendering -----------------------------------------------------------------------------


def label(check: Check) -> str:
    if check.status == OK and check.due:
        return "DUE"
    return LABELS.get(check.status, check.status)


def shown(check: Check, verbose: bool) -> bool:
    """Plain output lists essentials and anything not ok; ``-v`` lists every row."""
    if verbose or check.level == ESSENTIAL:
        return True
    return check.due or check.status in (*FAILING, UNKNOWN)


def _fix_lines(fix: Fix, prefix: str = "fix") -> list[str]:
    verb = {"route": "open the console at ", "link": "see "}.get(fix.kind, "")
    lines = [f"{prefix}: {verb}{fix.text}"]
    lines += [f"       {step}" for step in fix.steps]
    if fix.alt:
        lines += _fix_lines(fix.alt, "or")
    return lines


def summary(checks: list[Check]) -> str:
    essentials = len(readiness.failing(checks, ESSENTIAL))
    recommended = len(readiness.failing(checks, RECOMMENDED))
    unknown = sum(1 for c in checks if c.status == UNKNOWN)
    head = (
        f"{essentials} essential{'s' if essentials != 1 else ''} failing"
        if essentials
        else "all essentials ok"
    )
    return f"{head}, {recommended} recommended, {unknown} unknown. Details: jobhunter doctor -v"


def render(report: Report, *, verbose: bool = False) -> list[str]:
    out: list[str] = []
    for group in readiness.GROUPS:
        rows = [c for c in report.checks if c.group == group and shown(c, verbose)]
        if not rows:
            continue
        out.append(group)
        width = max(len(c.title) for c in rows)
        for c in rows:
            out.append(f"  {label(c):<8} {c.title:<{width}}  {c.detail}")
            if verbose:
                out.append(f"           unlocks: {c.unlocks}")
                if c.source:
                    out.append(f"           from: {c.source}")
            if c.fix and (verbose or c.status in FAILING or c.due):
                out += [f"           {line}" for line in _fix_lines(c.fix)]
    if report.preflight_ran:
        out.append("Container preflight (dry run)")
        out += [f"  {line.render()}" for line in report.preflight_lines]
    if verbose:
        out.append(f"mode: {report.mode}")
        out.append(WAL_NOTE)
    out.append(summary(report.checks))
    return out


def to_json_data(report: Report) -> list[dict]:
    return [c.as_dict() for c in report.checks]
