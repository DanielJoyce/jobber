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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from jobhunter import container, legacy_data, live_checks, model_access, readiness
from jobhunter import readiness_snapshot as snap
from jobhunter.config import MODEL_KEYS, Settings, load_settings, resolve_path, shown_value
from jobhunter.ops import preflight
from jobhunter.readiness import (
    BASICS,
    BROKEN,
    ESSENTIAL,
    FAILING,
    INFO,
    MISSING,
    OK,
    RECOMMENDED,
    RUNNING,
    SCORING,
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
    model_sources: list[tuple[str, str, str]] = field(default_factory=list)
    # --snapshot: one line per section written or not (stderr), e.g. "wrote manual_inputs"
    snapshot_notes: list[str] = field(default_factory=list)
    settings: Settings | None = None  # None when the config file would not load
    live: list[live_checks.LiveResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if not readiness.essentials_ok(self.checks):
            return 1
        # --live: an essential row's live check failing is also exit 1 (019 doctor).
        essential = {c.id for c in self.checks if c.level == ESSENTIAL}
        if any(r.failing and essential.intersection(r.rows) for r in self.live):
            return 1
        return 0


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


_ACCESS_STATUS = {
    model_access.OK: readiness.OK,
    model_access.LIKELY: readiness.OK,
    model_access.MISSING: MISSING,
    model_access.UNKNOWN: UNKNOWN,
}


def model_access_checks(settings: Settings, mode: str, now: datetime) -> list[Check]:
    """Doctor-only rows ``models.access.<use>``: can each model use run, and on what.

    Local and free (``model_access`` makes no call). Informational level, so a missing key is
    not counted twice next to the ``scorer.*`` rows. A blocked use (models.toml holds an
    invalid scorer) says so instead of naming a credential. Detail is fixed wording and
    variable names; the model value shown is a scorer spec or model id only."""
    layers = settings.models_layers
    blocked = layers.blocked if layers is not None else {}
    conn = readiness._open_ro(resolve_path(settings.paths.db_path))
    rows = []
    try:
        for use in model_access.USES:
            title = f"{model_access.USE_LABELS[use]} access"
            field_name = {"screen": "screen_scorer", "deep": "deep_scorer"}.get(use)
            unlocks = "Whether this use can run"
            cid = f"models.access.{use}"
            if field_name in blocked:
                rows.append(Check(cid, SCORING, INFO, MISSING, False, title,
                                  "blocked: models.toml holds an invalid value for it",
                                  unlocks, Fix("command", "jobhunter models show"),
                                  "models.toml"))  # fmt: skip
                continue
            try:
                choice = model_access.choice_for(use, settings)
                access = model_access.access_for(
                    use, choice, settings, mode=mode, now=now, conn=conn
                )
            except Exception as exc:  # never a traceback: a message could echo a value
                detail = f"could not be checked ({type(exc).__name__})"
                rows.append(Check(cid, SCORING, INFO, UNKNOWN, False, title, detail, unlocks))
                continue
            shown = shown_value(choice.model)
            on = f"{shown}: " if shown else ""
            if access.status == model_access.LIKELY:
                detail = f"{on}{access.detail} (likely)"
            else:
                detail = f"{on}{access.detail}"
            fix = Fix("route", "/models") if access.status == model_access.MISSING else None
            rows.append(Check(cid, SCORING, INFO, _ACCESS_STATUS[access.status], False, title,
                              detail, unlocks, fix, access.provider))  # fmt: skip
    finally:
        if conn is not None:
            conn.close()
    return rows


def model_sources(settings: Settings) -> list[tuple[str, str, str]]:
    """(``section.key``, shown value, source) for every model setting, for ``doctor -v``."""
    layers = settings.models_layers
    out = []
    for section, keys in MODEL_KEYS.items():
        for key in keys:
            value = getattr(getattr(settings, section), key)
            text = str(value).lower() if isinstance(value, bool) else value
            where = layers.source(section, key) if layers is not None else "unknown"
            out.append((f"{section}.{key}", shown_value(text) or "(not shown)", where))
    return out


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


def _preflight_skipped(why: str) -> Check:
    return Check(
        f"preflight.{PREFLIGHT_OTHER}", RUNNING, RECOMMENDED, BROKEN, False,
        PREFLIGHT_TITLES[PREFLIGHT_OTHER], f"not run: {why}", "The container deployment",
        Fix("command", "jobhunter container preflight --dry-run"),
    )  # fmt: skip


def _write_note(section: str, write: Callable[[], bool]) -> str:
    """Write one readiness.json section; the line to print about it (no value, no path)."""
    try:
        written = write()
    except snap.Unreadable:
        return (
            f"readiness.json: {section} not written (the file cannot be read; remove it and "
            "run this again)"
        )
    except OSError as exc:
        return f"readiness.json: {section} not written ({type(exc).__name__})"
    if not written:
        return f"readiness.json: {section} not written (its lock is busy; try again)"
    return f"readiness.json: wrote {section}"


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
    snapshot: bool = False,
    snapshot_timeout: float | None = None,
) -> Report:
    """Every local check plus ``data.layout``, and with ``containers`` preflight's mapped rows.

    A config file that will not load becomes a broken ``config.file`` row (exception type only)
    and the other checks run against code defaults.

    Rows carry their effective status (merged with a fresh ``readiness.json``). With
    ``snapshot`` this process's own view is first written as ``manual_inputs`` (and, with
    ``containers``, preflight's mapped rows as ``host``); ``run_inputs`` is never touched."""
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
        settings, secrets_report=secrets_report, now=now, mode=mode, home=home,
        use_snapshot=False,
        # in a container doctor is assumed to run through `jobhunter-ctr`, which gets the run's
        # secrets; `podman exec <console> jobhunter doctor` does not, and cannot be told apart
        # (documented in the help: use `jobhunter-ctr doctor`, not exec, with --snapshot)
        sees_run_secrets=mode == readiness.CONTAINER,
    )  # fmt: skip
    if config_error is not None:
        checks = [_config_failed(config_error) if c.id == "config.file" else c for c in checks]
    notes: list[str] = []
    kw = {} if snapshot_timeout is None else {"timeout": snapshot_timeout}
    if snapshot and config_error is not None:
        notes.append("readiness.json: not written (the config file will not load)")
    elif snapshot:
        notes.append(
            _write_note(
                "manual_inputs",
                lambda: snap.record_manual_inputs(checks, settings, now=now, mode=mode, **kw),
            )
        )
    viewer = "this container" if mode == readiness.CONTAINER else "this shell"
    data_dir = resolve_path(settings.paths.data_dir)
    # The placeholder Containers row (unknown before the merge): --containers replaces it with
    # this run's preflight rows, so an older host section must not keep it failing.
    placeholder = {c.id for c in checks if c.id == "containers.deployment" and c.status == UNKNOWN}
    checks = snap.apply(checks, data_dir, now=now, mode=mode, viewer=viewer)
    checks.insert(1, layout_check(settings, home))
    at = next((i for i, c in enumerate(checks) if c.id == "models.choices"), None)
    if at is not None:
        checks[at + 1 : at + 1] = model_access_checks(settings, mode, now)

    report = Report(
        checks,
        mode,
        group=wanted,
        snapshot_notes=notes,
        settings=None if config_error else settings,
    )
    if config_error is None:
        report.model_sources = model_sources(settings)
    if containers:
        if context_builder is None:
            raise RuntimeError("--containers needs a preflight context builder")
        mapped: list[Check]
        if config_error is not None:
            mapped = [_preflight_skipped("the config file will not load")]
        else:
            try:
                result = (run_preflight or preflight.run)(context_builder(), dry_run=True)
            except Exception as exc:  # never a traceback: it could echo a config value
                mapped = [_preflight_skipped(f"preflight failed ({type(exc).__name__})")]
            else:
                report.preflight_ran = True
                report.preflight_lines = list(result.lines)
                mapped = map_preflight(result)
                if snapshot and config_error is None:
                    rows = mapped
                    report.snapshot_notes.append(
                        _write_note("host", lambda: snap.record_host(rows, settings, now=now, **kw))
                    )
        # The placeholder row said "run doctor --containers"; the real rows replace it.
        checks = [c for c in checks if c.id not in placeholder]
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


def summary(checks: list[Check], live: list[live_checks.LiveResult] | None = None) -> str:
    failing_ids = {c.id for c in readiness.failing(checks, ESSENTIAL)}
    essential_ids = {c.id for c in checks if c.level == ESSENTIAL}
    for r in live or []:  # a refused live check is an essential failing too (same as exit 1)
        if r.failing:
            failing_ids |= essential_ids.intersection(r.rows)
    essentials = len(failing_ids)
    recommended = len(readiness.failing(checks, RECOMMENDED))
    unknown = sum(1 for c in checks if c.status == UNKNOWN)
    head = (
        f"{essentials} essential{'s' if essentials != 1 else ''} failing"
        if essentials
        else "all essentials ok"
    )
    cmd = container.doctor_command()
    return f"{head}, {recommended} recommended, {unknown} unknown. Details: {cmd} -v"


TEST_BUTTON = "use Test (free)"  # readiness wording for the /setup button; no button in a terminal


def terminal_detail(detail: str, mode: str) -> str:
    """Row text for the CLI: the /setup button does not exist here, so name the command."""
    return detail.replace(TEST_BUTTON, f"run `{live_checks.doctor_live_command(mode)}`")


def render(report: Report, *, verbose: bool = False) -> list[str]:
    out: list[str] = []
    for group in readiness.GROUPS:
        rows = [c for c in report.checks if c.group == group and shown(c, verbose)]
        if not rows:
            continue
        out.append(group)
        width = max(len(c.title) for c in rows)
        for c in rows:
            out.append(
                f"  {label(c):<8} {c.title:<{width}}  {terminal_detail(c.detail, report.mode)}"
            )
            if verbose:
                out.append(f"           unlocks: {c.unlocks}")
                if c.source:
                    out.append(f"           from: {c.source}")
                out.append(f"           help: /help/{readiness.help_for(c.id, report.mode)}")
            if c.fix and (verbose or c.status in FAILING or c.due):
                out += [f"           {line}" for line in _fix_lines(c.fix)]
    if report.preflight_ran:
        out.append("Container preflight (dry run)")
        out += [f"  {line.render()}" for line in report.preflight_lines]
    if verbose and report.model_sources:
        out.append("Model settings (where each value comes from)")
        width = max(len(k) for k, _, _ in report.model_sources)
        out += [f"  {k:<{width}}  {v}  [{src}]" for k, v, src in report.model_sources]
    if verbose:
        out.append(f"mode: {report.mode}")
        out.append(WAL_NOTE)
    out += render_live(report)
    out.append(summary(report.checks, report.live))
    return out


def to_json_data(report: Report) -> list[dict]:
    return [{**c.as_dict(), "detail": terminal_detail(c.detail, report.mode)}
            for c in report.checks]  # fmt: skip


# --- live checks (S6) ---------------------------------------------------------------------------


def live_plan(report: Report) -> list[live_checks.Planned]:
    if report.settings is None:
        return []
    planned = live_checks.plan(report.settings, report.mode)
    if report.group is None:
        return planned
    shown = {c.id for c in report.checks}  # --group limits --live to that group's rows
    limited = [
        replace(p, rows=rows) for p in planned if (rows := tuple(r for r in p.rows if r in shown))
    ]
    return limited


def live_preamble(report: Report, planned: list[live_checks.Planned]) -> list[str]:
    """What ``--live`` will contact, printed before anything is contacted."""
    out = ["Live checks (free: no model is called, nothing is stored)."]
    if report.settings is None:
        return [*out, "  none run: the config file will not load"]
    contacts = [
        line
        for p in planned
        if not p.instead
        and (line := live_checks.contact_line(p.kind, p.rows[0], report.settings, report.mode))
    ]
    if contacts:
        out.append("Will contact:")
        out += [f"  {c}" for c in contacts]
    else:
        out.append("Nothing to contact here.")
    return out


def run_live(
    report: Report,
    planned: list[live_checks.Planned],
    *,
    now: Callable[[], datetime] | None = None,
    deps: live_checks.Deps | None = None,
) -> None:
    if report.settings is None or not planned:
        return
    clock = now or (lambda: datetime.now(UTC))
    report.live = live_checks.run_plan(planned, report.settings, report.mode, now=clock, deps=deps)


LIVE_LABELS = {
    live_checks.OK: "ok",
    live_checks.FAILED: "FAILED",
    live_checks.ERROR: "ERROR",
    live_checks.NOT_RUN: "not run",
}


def render_live(report: Report) -> list[str]:
    if not report.live:
        return []
    out = ["Live checks"]
    width = max(len(r.title) for r in report.live)
    for r in report.live:
        parts = [live_checks.fact_text(f) for f in r.facts] + list(r.notes)
        out.append(f"  {LIVE_LABELS[r.outcome]:<8} {r.title:<{width}}  {'; '.join(parts)}")
        if r.instead:
            out.append(f"           run: {r.instead}")
    return out
