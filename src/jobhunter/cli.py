"""Typer entrypoint (specs/002-architecture.md#process-model)."""

from __future__ import annotations

import contextvars
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from jobhunter import container, secrets
from jobhunter.config import Settings, env_files, load_env_files, load_settings, resolve_path
from jobhunter.core import db
from jobhunter.pipeline import runner
from jobhunter.sources.registry import enabled_sources, load_registry, sync_sources_table


class _JobhunterGroup(TyperGroup):
    """Turns a schema refusal (newer database, or pending migrations in a container
    deployment) into a one-line error and exit 1 from any command, instead of a traceback."""

    def invoke(self, ctx: typer.Context) -> object:
        token = _CURRENT_COMMAND.set(self._command_path(ctx))
        try:
            return super().invoke(ctx)
        except db.SchemaError as exc:
            typer.echo(f"error: {exc}", err=True)
            typer.echo(f"Run `{container.doctor_command()}` for details.", err=True)
            raise typer.Exit(1) from exc
        finally:
            _CURRENT_COMMAND.reset(token)

    def _command_path(self, ctx: typer.Context) -> str | None:
        """``"db migrate"`` for ``jobhunter db migrate --x``: the subcommand names only."""
        # Click 8.2 keeps the not-yet-dispatched subcommand names in _protected_args.
        pending = getattr(ctx, "_protected_args", None) or getattr(ctx, "protected_args", [])
        tokens = [*pending, *ctx.args]
        names: list[str] = []
        group: object = self
        # Duck-typed: typer ships its own vendored click, so click.Group is the wrong class.
        while hasattr(group, "get_command") and tokens:
            sub = group.get_command(ctx, tokens[0])
            if sub is None:
                break
            names.append(tokens.pop(0))
            group = sub
        return " ".join(names) or None


_CURRENT_COMMAND: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jobhunter_command", default=None
)


app = typer.Typer(
    cls=_JobhunterGroup,
    help="jobhunter: sweep job banks, score fit, track applications.",
    no_args_is_help=True,
)
sources_app = typer.Typer(help="Inspect and verify configured sources.", no_args_is_help=True)
mail_app = typer.Typer(help="Email alert ingest.", no_args_is_help=True)
applylinks_app = typer.Typer(help="Apply-link resolution.", no_args_is_help=True)
schedule_app = typer.Typer(help="Nightly systemd --user timers.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(mail_app, name="mail")
app.add_typer(applylinks_app, name="applylinks")
llm_app = typer.Typer(help="Local model server: status and benchmark.", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")
app.add_typer(llm_app, name="llm")
apply_app = typer.Typer(help="Assisted apply: packet drafts (specs/017).", no_args_is_help=True)
app.add_typer(apply_app, name="apply")
ext_app = typer.Typer(help="The jobhunter browser extension: pairing.", no_args_is_help=True)
app.add_typer(ext_app, name="ext")
secrets_app = typer.Typer(help="API keys and other secrets (never prints values).")
app.add_typer(secrets_app, name="secrets")
db_app = typer.Typer(help="Database schema and backups.", no_args_is_help=True)
app.add_typer(db_app, name="db")
container_app = typer.Typer(
    help="Container deployment on the host (specs/018): preflight.", no_args_is_help=True
)
app.add_typer(container_app, name="container")

NOT_IMPLEMENTED = "not implemented yet"

# What the last CLI start found for each known secret (specs/018 C2); `secrets status` prints it.
_SECRETS_REPORT: secrets.Report | None = None


def _host_only(command: str, host_command: str | None = None) -> None:
    """Exit with the host command when running in container mode (specs/018 C1)."""
    if container.is_container():
        typer.echo(container.refusal(command, host_command), err=True)
        raise typer.Exit(2)


# Commands that run while old-layout data is unmigrated: they report on it, move it, or do
# not touch user data at all (schedule only writes systemd units, version only reads).
UNGUARDED_COMMANDS = {"paths", "migrate-paths", "init", "schedule", "secrets", "version", "doctor"}

# specs/018-containers.md#version-skew: commands that neither migrate on their own nor refuse
# because migrations are pending; they still refuse a newer database where they would write.
# Every other command that opens the database goes through _migrate. Some of these land in
# later tickets (secrets status C2, container preflight C13, db backup and restore C6,
# upgrade C15); each must open the database without _migrate.
MIGRATION_EXEMPT_COMMANDS = frozenset(
    {
        "version",
        "paths",
        "secrets status",
        "container preflight",
        "db backup",
        "db restore",
        "upgrade",
        "db migrate",
        "doctor",
    }
)


RESTORE_MIGRATE_WAIT_S = 120.0


def _check_scorer_override(
    settings, spec: str | None, *, screen: bool = True, deep: bool = False
) -> None:
    """Fail fast (one line, exit 2) on a scorer that must not run, checking only what the mode
    uses: the ``--scorer`` override (else screen_scorer) when ``screen``, deep_scorer when
    ``deep``. The configured scorers go through ``scorer_problems``, which also reports a use
    that an invalid models.toml blocks (specs/020: fail closed, never fall back)."""
    from jobhunter.config import check_scorer_spec

    problems = settings.scoring.scorer_problems()
    try:
        if spec is not None:
            check_scorer_spec(spec, "--scorer")
        elif screen and "screen_scorer" in problems:
            raise ValueError(problems["screen_scorer"])
        if deep and "deep_scorer" in problems:
            raise ValueError(problems["deep_scorer"])
    except ValueError as exc:
        typer.echo(f"scorer error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _migrate(conn) -> list[int]:
    """Bring the schema up to date the automatic way (specs/018 Version skew).

    Without the deployment marker this migrates, after a pre-migrate backup into
    ``<data dir>/backups/auto``; with it, pending migrations raise ``MigrationsPending``
    ("run `jobhunter upgrade`"), which _JobhunterGroup prints as an error.

    Called from a command in MIGRATION_EXEMPT_COMMANDS it neither migrates nor refuses on
    pending migrations; it still refuses a database newer than this code.
    """
    if _invoked_command() in MIGRATION_EXEMPT_COMMANDS:
        db.check_not_newer(conn)
        return []
    path = db.database_file(conn)
    data_dir = None
    if path is not None:
        data_dir = db.owning_data_dir(path, resolve_path(load_settings().paths.data_dir))
    return db.migrate(conn, data_dir=data_dir, on_backup=_say_backed_up, on_wait=_say_waiting)


def _invoked_command() -> str | None:
    """The running subcommand path without the program name, e.g. ``"db migrate"``."""
    return _CURRENT_COMMAND.get()


def _say_backed_up(path: Path) -> None:
    typer.echo(f"pre-migrate backup: {path}", err=True)


def _say_waiting(lock: Path) -> None:
    typer.echo(f"waiting for another jobhunter process to finish migrating ({lock})", err=True)


@app.callback()
def _load_env(ctx: typer.Context) -> None:
    """Load .env secrets (e.g. USAJOBS_API_KEY) before any command runs, then refuse to run
    against a new database while the real one is still in the old layout."""
    if ctx.invoked_subcommand == "console":
        # Before .env and *_FILE are loaded: a console Restart re-execs with this environment,
        # so the new process reads the .env files afresh (specs/019 Addendum B, S12b).
        from jobhunter.console import restart

        restart.snapshot_environ()
    # Snapshot the migrations this code ships now, at start: a job that waits hours for the
    # run lock across a fast-forward must treat migrations merged meanwhile as unknown.
    db.prime_migrations()
    global _SECRETS_REPORT
    _SECRETS_REPORT = secrets.startup(
        load_settings, load_env_files, lambda line: typer.echo(line, err=True)
    )
    if ctx.invoked_subcommand == "console":
        # The console's config watch compares .env files against what was loaded now
        # (salted digests, in memory only; specs/019 Addendum B).
        from jobhunter import config_watch

        config_watch.snapshot_env(env_files())
    if ctx.resilient_parsing or ctx.invoked_subcommand in UNGUARDED_COMMANDS:
        return
    if "--help" in sys.argv[1:]:
        return
    _refuse_split_data()


def _refuse_split_data() -> None:
    from jobhunter import legacy_data

    try:
        settings = load_settings()
    except (ValueError, OSError):
        return  # a broken config file: the command itself reports it
    status = legacy_data.check(settings)
    if status.blocked:
        typer.echo(f"error: {status.message}", err=True)
        typer.echo(f"Run `{container.doctor_command()}` for details.", err=True)
        raise typer.Exit(1)
    for note in status.warnings:
        typer.echo(note, err=True)


def _stub() -> None:
    typer.echo(NOT_IMPLEMENTED)


WAIT_HELP = (
    "Wait up to DURATION (e.g. 30m, 2h) for another jobhunter job to finish, then run; "
    "exit 75 if it does not. Timers pass this. Without it, a running job means: report and "
    "exit 0, nothing done."
)
WaitOption = Annotated[str | None, typer.Option("--wait", metavar="DURATION", help=WAIT_HELP)]


def _parse_wait(wait: str | None) -> float | None:
    from jobhunter.core.runlock import parse_duration

    if wait is None:
        return None
    try:
        return parse_duration(wait)
    except ValueError as exc:
        typer.echo(f"--wait: {exc}", err=True)
        raise typer.Exit(2) from exc


def _human(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds / 3600:g}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g}m"
    return f"{seconds:g}s"


@contextmanager
def _job_lock(settings, command: str, wait: float | None) -> Iterator[None]:
    """Hold the run lock (specs/018 Run lock, C5) for the whole job.

    Held by another job: without ``wait``, say so and exit 0 (nothing spent); with ``wait``,
    retry until it runs out, then exit 75. A holder older than 6 h exits 75 either way, so
    systemd's OnFailure= reports a hung job.
    """
    from jobhunter.core import runlock

    lock = runlock.RunLock(resolve_path(settings.paths.db_path), command)

    def waiting(holder: runlock.Holder | None) -> None:
        who = holder.describe() if holder else "details unknown"
        typer.echo(f"waiting up to {_human(wait or 0)} for the run lock held by {who}", err=True)

    try:
        lock.acquire(wait, on_wait=waiting)
    except (runlock.StaleHolder, runlock.LockTimeout) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(runlock.EXIT_STALE) from exc
    except runlock.LockHeld as exc:
        typer.echo(f"{exc}; not started, nothing spent. Try again when it finishes.", err=True)
        raise typer.Exit(0) from exc
    try:
        yield
    finally:
        lock.release()


def _snapshot_write(what: str, write: Callable[[], bool]) -> None:
    """Write a readiness.json section; a failure is one log line, never a failed run."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        if not write():
            logger.warning("readiness.json: %s not written (its lock was busy)", what)
    except Exception as exc:  # never fail the run over the snapshot
        logger.warning("readiness.json: %s not written (%s)", what, type(exc).__name__)


@app.command()
def run(
    stage: Annotated[
        list[str] | None,
        typer.Option(help="Stage(s) to run: repeatable or comma list. Default: all."),
    ] = None,
    state: Annotated[
        str | None, typer.Option(help="Comma list of state codes; US includes national rows.")
    ] = None,
    since: Annotated[str | None, typer.Option(help="Override the watermark (YYYY-MM-DD).")] = None,
    full: Annotated[bool, typer.Option("--full", help="Ignore watermarks.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan only.")] = False,
    max_resolve: Annotated[
        int, typer.Option(help="Cap on detail-page fetches per run.")
    ] = runner.DEFAULT_MAX_RESOLVE,
    refetch_details: Annotated[
        bool,
        typer.Option(
            "--refetch-details",
            help="Fetch detail pages again even for postings already pulled.",
        ),
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="With --refetch-details: refetch only this source key (repeatable).",
        ),
    ] = None,
    wait: WaitOption = None,
) -> None:
    """Run the ingest pipeline."""
    if source and not refetch_details:
        typer.echo("error: --source needs --refetch-details", err=True)
        raise typer.Exit(2)
    known = {r.key for r in load_registry()} if source else set()
    if unknown := sorted(set(source or ()) - known):
        typer.echo(f"error: unknown source key(s): {', '.join(unknown)}", err=True)
        raise typer.Exit(2)
    try:
        stages = runner.parse_stages(stage)
        since_dt = datetime.fromisoformat(since).replace(tzinfo=UTC) if since else None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    wait_s = _parse_wait(wait)
    states = runner.parse_states(state)
    settings = load_settings()
    rows = load_registry()
    loader = runner.default_profile_loader(settings)

    if dry_run:
        db_path = resolve_path(settings.paths.db_path)
        conn = db.connect(db_path if db_path.is_file() else ":memory:")
        try:
            if not db_path.is_file():
                _migrate(conn)
            text = runner.dry_run_plan(
                conn,
                rows,
                profile=loader(),
                stages=stages,
                states=states,
                since=since_dt,
                full=full,
            )
        finally:
            conn.close()
        typer.echo(text)
        return

    from jobhunter import readiness_snapshot

    db_path = resolve_path(settings.paths.db_path)
    trigger = readiness_snapshot.current_trigger()
    with _job_lock(settings, "run", wait_s):
        # After the lock, so a run that times out on it writes nothing (019 S4).
        _snapshot_write(
            "run inputs",
            lambda: readiness_snapshot.record_run_inputs(
                settings,
                secrets_report=_SECRETS_REPORT,
                registry_rows=rows,
                now=datetime.now(UTC),
                trigger=trigger,
            ),
        )
        conn = db.connect(db_path)
        try:
            _migrate(conn)
            report = runner.run_pipeline(
                conn,
                settings,
                rows,
                profile_loader=loader,
                stages=stages,
                states=states,
                since=since_dt,
                full=full,
                max_resolve=max_resolve,
                trigger=trigger,
                refetch_details=set(source) if source else refetch_details,
            )
        finally:
            conn.close()
        _snapshot_write(
            "run outcome",
            lambda: readiness_snapshot.record_run_outcome(settings, report, now=datetime.now(UTC)),
        )
    summary = runner.format_summary(report)
    if summary:
        typer.echo(summary)
    raise typer.Exit(report.exit_code)


@app.command()
def score(
    submit: Annotated[
        bool,
        typer.Option(
            "--submit",
            help="Submit a screen batch. Without --scorer it scores only new job groups "
            "(none scored under any model), like the nightly run; --scorer screens every "
            "group eligible for that scorer. To score already-scored jobs again, use --rescore.",
        ),
    ] = False,
    collect: Annotated[
        str | None, typer.Option("--collect", metavar="BATCH_ID", help="Collect a batch.")
    ] = None,
    collect_pending: Annotated[
        bool,
        typer.Option(
            "--collect-pending",
            help="Collect every submitted batch not yet collected (nightly 03:30 timer).",
        ),
    ] = False,
    rescore_pending: Annotated[
        bool,
        typer.Option(
            "--rescore-pending",
            help="Run every queued /prefs re-score request (nightly collect timer).",
        ),
    ] = False,
    rescore: Annotated[
        str | None,
        typer.Option(
            "--rescore",
            metavar="SCOPE",
            help="Re-score now: 'recent' (open A-E from the last 14 days) or 'all'. "
            "Shows the estimate and asks first.",
        ),
    ] = None,
    group: Annotated[
        int | None,
        typer.Option(
            "--group",
            metavar="GID",
            help="Score this one job group now (a pasted posting, specs/017). Shows the "
            "estimate and asks first; bypasses the prefilter rules because you chose it.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="With --rescore or --group: skip the confirmation prompt."),
    ] = False,
    limit: Annotated[int, typer.Option(help="Most job groups to submit.", min=1)] = 500,
    deep: Annotated[
        int | None,
        typer.Option("--deep", metavar="N", min=1, help="Deep-pass the top N screened groups."),
    ] = None,
    deep_group: Annotated[
        int | None,
        typer.Option("--deep-group", metavar="GID", help="Deep-pass one job group."),
    ] = None,
    scorer_override: Annotated[
        str | None,
        typer.Option(
            "--scorer",
            metavar="SPEC",
            help="Screen with this scorer instead of scoring.screen_scorer, e.g. "
            "openrouter:openai/gpt-oss-120b (for comparing scorers on the same jobs).",
        ),
    ] = None,
    jobs_per_request: Annotated[
        int | None,
        typer.Option(
            "--jobs-per-request",
            min=1,
            max=25,
            help="Judge this many jobs per request (openai-compat, openrouter, local scorers; "
            "overrides scoring.<provider>.jobs_per_request; at most 16). Decisions scorers "
            "(jev:/decisions:) default to 8, at most 25. specs/016 Packed requests.",
        ),
    ] = None,
    wait: Annotated[
        str | None,
        typer.Option(
            "--wait",
            metavar="DURATION",
            help=WAIT_HELP + " --collect-pending waits 2h unless given.",
        ),
    ] = None,
) -> None:
    """Screen batches (specs/006 Stage 2) or the Opus deep pass (Stage 3)."""
    modes = [
        submit,
        collect is not None,
        collect_pending,
        deep is not None,
        deep_group is not None,
        rescore_pending,
        rescore is not None,
        group is not None,
    ]
    if sum(modes) != 1:
        typer.echo(
            "pass exactly one of --submit, --collect, --collect-pending, --deep, --deep-group, "
            "--rescore-pending, --rescore or --group",
            err=True,
        )
        raise typer.Exit(2)
    if rescore is not None and rescore not in ("recent", "all"):
        typer.echo("--rescore takes 'recent' or 'all'", err=True)
        raise typer.Exit(2)
    if yes and rescore is None and group is None:
        typer.echo("--yes applies to --rescore and --group only", err=True)
        raise typer.Exit(2)
    wait_s = _parse_wait(wait)
    if wait_s is None and collect_pending:
        # Collecting spends nothing new (the batches are paid for), so waiting is always safe.
        wait_s = COLLECT_PENDING_WAIT_S
    names = ["submit", "collect", "collect-pending", "deep", "deep-group", "rescore-pending"]
    names += ["rescore", "group"]
    mode = next(name for name, on in zip(names, modes, strict=True) if on)
    settings = load_settings()

    def lock() -> AbstractContextManager[None]:
        return _job_lock(settings, f"score --{mode}", wait_s)

    # --group and --rescore ask y/N first: they take the lock only after the answer (and
    # re-check what was confirmed), so an open prompt never blocks the timers.
    prompts = mode in ("group", "rescore")
    with nullcontext() if prompts else lock():
        _score_mode(
            paid_lock=lock if prompts else nullcontext,
            submit=submit,
            collect=collect,
            collect_pending=collect_pending,
            rescore_pending=rescore_pending,
            rescore=rescore,
            group=group,
            yes=yes,
            limit=limit,
            deep=deep,
            deep_group=deep_group,
            scorer_override=scorer_override,
            jobs_per_request=jobs_per_request,
        )


COLLECT_PENDING_WAIT_S = 2 * 3600.0


def _score_mode(
    *,
    submit: bool,
    collect: str | None,
    collect_pending: bool,
    rescore_pending: bool,
    rescore: str | None,
    group: int | None,
    yes: bool,
    limit: int,
    deep: int | None,
    deep_group: int | None,
    scorer_override: str | None,
    jobs_per_request: int | None,
    paid_lock: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> None:
    """The body of ``score``: under the run lock, or for --group and --rescore taking it
    through ``paid_lock`` once the user has said yes."""
    from datetime import UTC, datetime

    import anthropic

    from jobhunter.config import load_settings
    from jobhunter.core import db
    from jobhunter.scoring import screen
    from jobhunter.scoring.profile import ProfileError, load_profile_for
    from jobhunter.scoring.scorers import ScorerError, privacy_notice, scorer_from_string

    settings = load_settings()
    is_deep = deep is not None or deep_group is not None
    # Check only the spec this mode uses: --collect-pending reads stored batch models and
    # --rescore-pending reads each request's own spec, so a bad config must not block them.
    # --collect ID spends nothing new: it collects the batch under the model stored on it, so
    # a blocked screen_scorer (an invalid models.toml) does not stop it (specs/020).
    collect_stored = collect is not None and scorer_override is None
    _check_scorer_override(
        settings,
        scorer_override,
        screen=not (is_deep or collect_pending or rescore_pending or collect_stored),
        deep=is_deep,
    )
    try:
        profile = load_profile_for(settings)
    except ProfileError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if group is not None:
        if scorer_override is not None or jobs_per_request is not None:
            typer.echo("--group scores with scoring.screen_scorer; no other options", err=True)
            raise typer.Exit(2)
        _score_group(settings, profile, group, yes, paid_lock)
        return
    rescoring = rescore_pending or rescore is not None
    if scorer_override is not None and not (submit or collect is not None or rescoring):
        typer.echo("--scorer applies to --submit, --collect and re-score runs only", err=True)
        raise typer.Exit(2)
    if jobs_per_request is not None and not submit:
        typer.echo("--jobs-per-request applies to --submit only", err=True)
        raise typer.Exit(2)
    scorer = scorer_override or settings.scoring.screen_scorer
    if jobs_per_request is not None and scorer.startswith("anthropic:"):
        typer.echo(
            "--jobs-per-request needs an openai-compat, openrouter or local scorer", err=True
        )
        raise typer.Exit(2)
    decisions_scorer = scorer.split(":", 1)[0] in ("jev", "decisions")
    if jobs_per_request is not None and jobs_per_request > 16 and not decisions_scorer:
        typer.echo("--jobs-per-request is at most 16 for chat scorers", err=True)
        raise typer.Exit(2)
    if rescoring:
        _score_rescore(settings, profile, rescore, scorer_override, yes, paid_lock)
        return
    screening = submit or collect is not None
    blocked_collect = collect_stored and "screen_scorer" in settings.scoring.scorer_problems()
    if submit:
        from jobhunter.scoring.scorers import MISSING, credential_for

        cred = credential_for(scorer, settings.scoring)
        if cred.status == MISSING:
            hint = f" (set {', '.join(cred.names)})" if cred.names else ""
            typer.echo(f"scorer error: no credential for {scorer}{hint}", err=True)
            raise typer.Exit(2)
    if screening and not blocked_collect and (notice := privacy_notice(scorer, settings.scoring)):
        typer.echo(notice, err=True)
    conn = db.connect(settings.paths.db_path)
    _migrate(conn)
    # The screen scorer may not be Anthropic; only build its client where it is needed. Only
    # Anthropic batches are collected, so a blocked screen_scorer still collects with it.
    batches = not screening or blocked_collect or scorer.startswith("anthropic:")
    client = anthropic.Anthropic() if batches else None
    now = datetime.now(UTC)
    # Plain --submit follows the nightly rule (new groups only), so a model change never
    # re-screens scored jobs without a confirmed re-score; an explicit --scorer is a
    # deliberate comparison run and keeps per-scorer eligibility (specs/020 M9).
    new_only = submit and scorer_override is None
    try:
        if submit and new_only:
            _note_already_scored(conn, profile, scorer)
        if deep is not None or deep_group is not None:
            from jobhunter.scoring import deep as stage3
            from jobhunter.scoring import deep_run

            deep_scorer = settings.scoring.deep_scorer
            gids = [deep_group] if deep_group is not None else None
            if gids is None:
                assert deep is not None
                gids = stage3.shortlist(conn, profile, deep, scorer=deep_scorer)
            deep_spent = 0.0
            for gid in gids:
                try:
                    res = stage3.deep_score(
                        conn,
                        client,
                        profile,
                        gid,
                        now=now,
                        scorer=deep_scorer,
                        remaining_usd=lambda: screen.remaining_budget(conn, settings.scoring, now),
                        rejection_days=settings.scoring.employer_rejection_days,
                    )
                except anthropic.APIError as exc:
                    typer.echo(f"group {gid}: API error: {exc}", err=True)
                    continue
                if res.status == "capped":
                    # A call starts only when the cap covers its worst case (specs/020 M11).
                    why = deep_run.cap_message(conn, settings.scoring, now, deep_scorer)
                    typer.echo(f"group {gid}: not started, spend cap reached. {why}", err=True)
                    if deep_spent == 0.0:
                        raise typer.Exit(1)  # nothing was deep-scored: not a success
                    break
                deep_spent += res.cost_usd
                flag = " DISAGREES with screen" if res.disagreement else ""
                typer.echo(f"group {gid}: {res.status}{flag} {res.detail}".rstrip())
        elif submit and client is None:
            try:
                sync_scorer = scorer_from_string(scorer, scoring=settings.scoring)
                if jobs_per_request is not None:
                    sync_scorer.jobs_per_request = jobs_per_request  # type: ignore[attr-defined]
                sync = screen.score_sync(
                    conn,
                    sync_scorer,
                    profile,
                    limit=limit,
                    now=now,
                    remaining_usd=lambda: screen.remaining_budget(conn, settings.scoring, now),
                    rejection_days=settings.scoring.employer_rejection_days,
                    new_only=new_only,
                )
            except ScorerError as exc:
                typer.echo(f"scorer error: {exc}", err=True)
                raise typer.Exit(1) from exc
            typer.echo(json.dumps(sync.as_dict(), indent=2))
        elif submit:
            batch_id = screen.submit_batch(
                conn,
                client,
                profile,
                limit=limit,
                now=now,
                scorer=scorer,
                remaining_usd=lambda: screen.remaining_budget(conn, settings.scoring, now),
                rejection_days=settings.scoring.employer_rejection_days,
                new_only=new_only,
            )
            typer.echo(f"submitted batch {batch_id}" if batch_id else "nothing to submit")
        elif collect_pending:
            assert client is not None  # the anthropic client is built for non-screen modes
            results = screen.collect_pending(conn, client, profile, now=now)
            if not results:
                typer.echo("no pending batches")
            else:
                typer.echo(json.dumps({b: r.as_dict() for b, r in results.items()}, indent=2))
            if any(r.status.startswith("error") for r in results.values()):
                raise typer.Exit(1)
        else:
            assert collect is not None
            if client is None:
                typer.echo(f"{scorer} does not batch; nothing to collect", err=True)
                raise typer.Exit(2)
            result = screen.collect_batch(conn, client, collect, profile, now=now)
            typer.echo(json.dumps(result.as_dict(), indent=2))
    finally:
        conn.close()


def _note_already_scored(conn, profile, scorer: str) -> None:
    """One line when ``new_only`` leaves out groups that are eligible under ``scorer`` only
    because they were scored by another model (or prompt version)."""
    from jobhunter.scoring import screen

    big = 10**9
    all_n = len(screen.eligible_groups(conn, profile, scorer=scorer, limit=big))
    new_n = len(screen.eligible_groups(conn, profile, scorer=scorer, limit=big, new_only=True))
    if all_n > new_n:
        typer.echo(f"{all_n - new_n} already scored; use a re-score to score them again", err=True)


def _score_group(
    settings,
    profile,
    group_id: int,
    yes: bool,
    paid_lock: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> None:
    """``score --group GID``: estimate, confirm, then score that one group (specs/017)."""
    from jobhunter.apply import score as group_score
    from jobhunter.scoring import rescore as rs
    from jobhunter.scoring.scorers import privacy_notice

    conn = db.connect(settings.paths.db_path)
    _migrate(conn)
    now = datetime.now(UTC)
    try:
        est = group_score.estimate(conn, profile, settings.scoring, group_id, now)
        how = {"default": "assumed", "floor": "no cache reuse"}.get(est.cost_source, "measured")
        typer.echo(
            f"group {group_id}: score with {est.scorer}, estimated ${est.estimated_usd:.4f} "
            f"({how}); "
            f"spend cap remaining {rs.usd(max(est.remaining_usd, 0))}"
        )
        if est.refusal:
            typer.echo(f"refused: {est.refusal}", err=True)
            raise typer.Exit(1)
        if notice := privacy_notice(est.scorer, settings.scoring):
            typer.echo(notice, err=True)
        if not yes and not typer.confirm("Score it? This spends credits.", default=False):
            typer.echo("not run")
            raise typer.Exit(1)
        try:
            # Locked only after the answer; score_now re-checks the confirmed estimate token.
            # The claim's clock starts once the lock is held, not when the estimate was shown:
            # a long wait at the prompt must not shorten the claim window (or score against a
            # stale day's cap).
            with paid_lock():
                out = group_score.score_now(
                    conn,
                    profile,
                    settings.scoring,
                    group_id,
                    token=est.token,
                    now=datetime.now(UTC),
                )
        except group_score.ScoreRefused as exc:
            typer.echo(f"not scored: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"{out.status}: {out.detail}")
    finally:
        conn.close()


def _score_rescore(
    settings,
    profile,
    scope: str | None,
    scorer_override: str | None,
    yes: bool,
    paid_lock: Callable[[], AbstractContextManager[None]] = nullcontext,
):
    """``score --rescore SCOPE`` (estimate, confirm, run) or ``--rescore-pending`` (drain)."""
    from jobhunter.scoring import rescore as rs
    from jobhunter.scoring.scorers import ScorerError, privacy_notice

    conn = db.connect(settings.paths.db_path)
    _migrate(conn)
    clock = lambda: datetime.now(UTC)  # noqa: E731
    spec = scorer_override or settings.scoring.screen_scorer
    # --rescore-pending reads each request's own spec; only an explicit --scorer is checked
    _check_scorer_override(settings, scorer_override, screen=scope is not None)
    try:
        if scope is None:
            results = rs.drain_pending(
                conn, profile, settings.scoring, now=clock, scorer=scorer_override
            )
            if not results:
                typer.echo("no pending re-score requests")
            failed = False
            for rid, res in results:
                if isinstance(res, str):
                    typer.echo(f"request {rid}: {res}")
                    continue
                row = rs.get_request(conn, rid)
                typer.echo(
                    f"request {rid}: {row['status']} scored {res.scored} of {res.total}, "
                    f"errors {res.errored}, ${res.cost_usd:.4f}"
                    + (f" ({res.note})" if res.note else "")
                    + (f" error: {row['error']}" if row["error"] else "")
                )
                failed = failed or row["status"] == "failed"
            if failed:
                raise typer.Exit(1)
            return
        now = clock()
        try:
            rs.refresh_catalog(spec, settings.scoring)
            plan = rs.make_plan(conn, profile, settings.scoring, scope, spec, now)
        except (rs.RescoreError, ScorerError) as exc:
            typer.echo(f"re-score: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(
            f"scope {scope}: {plan.total} groups with {spec} "
            f"({plan.rescored} of {plan.waiting} waiting were scored before); "
            f"estimated ${plan.estimated_usd:.2f} at ${plan.cost_per_job:.5f}/job "
            f"({plan.cost_source}); spend cap remaining {rs.usd(max(plan.remaining_usd, 0))}"
        )
        if plan.total and plan.reserve_differs:
            typer.echo(
                f"while it runs the spend cap holds ${plan.reserve_per_job:.4f}/job "
                f"(${plan.reserved_usd:.2f} for all {plan.total})"
            )
        if plan.total and not plan.refusal and plan.fits_now < plan.total:
            typer.echo(f"only {plan.fits_now} of {plan.total} fit under the cap now")
        if plan.refusal:
            typer.echo(f"refused: {plan.refusal}", err=True)
            raise typer.Exit(1)
        if plan.total == 0:
            typer.echo("nothing to re-score")
            return
        if notice := privacy_notice(spec, settings.scoring):
            typer.echo(notice, err=True)
        if not yes and not typer.confirm("Run it?", default=False):
            typer.echo("not run")
            raise typer.Exit(1)
        # The run lock is taken only after the answer, so an open prompt blocks no other job;
        # the plan is rebuilt under the lock and must still be the one confirmed.
        with paid_lock():
            try:
                fresh = rs.make_plan(conn, profile, settings.scoring, scope, spec, clock())
            except (rs.RescoreError, ScorerError) as exc:
                typer.echo(f"re-score: {exc}", err=True)
                raise typer.Exit(1) from exc
            if fresh.token != plan.token or fresh.refusal:
                typer.echo(
                    "the plan changed while you were deciding (new jobs, profile or costs); "
                    "nothing spent. Run it again to see the new estimate.",
                    err=True,
                )
                raise typer.Exit(1)
            rid = rs.create_request(conn, profile, fresh, now)
            try:
                res = rs.run_request(
                    conn,
                    rid,
                    profile,
                    settings.scoring,
                    scorer=spec,
                    now=clock,
                    prefiltered=plan.prefilter,  # the first plan ran the prefilter (rescore.py)
                )
            except rs.RescoreError as exc:
                rs.cancel_request(conn, rid, clock())
                typer.echo(f"re-score: {exc}", err=True)
                raise typer.Exit(1) from exc
        row = rs.get_request(conn, rid)
        typer.echo(
            f"{row['status']}: scored {res.scored} of {res.total}, errors {res.errored}, "
            f"${res.cost_usd:.4f}" + (f" ({res.note})" if res.note else "")
        )
        if row["error"]:
            typer.echo(f"error: {row['error']}", err=True)
            raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def backfill(
    days: Annotated[int, typer.Option(help="Ingest postings listed in the last N days.", min=1)],
    state: Annotated[
        str | None, typer.Option(help="Comma list of state codes; US includes national rows.")
    ] = None,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            "--budget-usd",
            min=0.0,
            help="Ceiling for this backfill (default: scoring.weekly_cap_usd). Replaces the "
            "daily cap for this command only.",
        ),
    ] = None,
    scorer_override: Annotated[
        str | None,
        typer.Option("--scorer", metavar="SPEC", help="Screen with this scorer."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Ingest and print the estimate; submit nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Submit without the y/N prompt.")] = False,
    wait: Annotated[
        bool, typer.Option("--wait", help="Poll batches until they finish or the wait ends.")
    ] = False,
    max_wait_minutes: Annotated[
        int, typer.Option(help="With --wait: stop polling after this many minutes.", min=1)
    ] = 240,
    chunk_size: Annotated[int, typer.Option(help="Jobs per Message Batch.", min=1)] = 5000,
    max_resolve: Annotated[
        int, typer.Option(help="Cap on detail-page fetches during ingest.", min=0)
    ] = 5000,
) -> None:
    """One-time historical ingest and screen within a budget (specs/006 Cost)."""
    import sys

    from jobhunter.pipeline import backfill as bf
    from jobhunter.scoring.scorers import ScorerError, privacy_notice, scorer_from_string

    settings = load_settings()
    rows = load_registry()
    spec = scorer_override or settings.scoring.screen_scorer
    _check_scorer_override(settings, scorer_override)
    try:
        scorer = scorer_from_string(spec, scoring=settings.scoring)
    except (ScorerError, ValueError) as exc:
        typer.echo(f"scorer error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if notice := privacy_notice(scorer.name, settings.scoring):
        typer.echo(notice, err=True)
    client = getattr(scorer, "client", None) if scorer.supports_batching else None
    budget = settings.scoring.weekly_cap_usd if budget_usd is None else budget_usd

    def confirm(est: bf.Estimate) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            return False
        return typer.confirm(
            f"Submit up to {est.fundable} jobs (about ${est.cost_usd:.2f}, budget ${budget:.2f})?",
            default=False,
        )

    db_path = resolve_path(settings.paths.db_path)
    # Interactive and paid: a running job means report and exit 0 (its own --wait polls
    # batches, not the lock).
    with _job_lock(settings, "backfill", None):
        conn = db.connect(db_path)
        try:
            _migrate(conn)
            result = bf.run_backfill(
                conn,
                settings,
                rows,
                scorer=scorer,
                client=client,
                days=days,
                states=runner.parse_states(state),
                budget_usd=budget,
                chunk_size=chunk_size,
                max_resolve=max_resolve,
                dry_run=dry_run,
                confirm=confirm,
                wait=wait,
                max_wait_s=max_wait_minutes * 60.0,
                out=typer.echo,
            )
        finally:
            conn.close()
    if result.status == "declined" and not sys.stdin.isatty():
        typer.echo("no terminal to confirm on; re-run with --yes to submit", err=True)
    raise typer.Exit(result.exit_code)


@app.command()
def dedupe(
    apply_url: Annotated[
        bool,
        typer.Option("--apply-url", help="Merge job groups that share a normalized apply URL."),
    ] = False,
    cross_state: Annotated[
        bool,
        typer.Option(
            "--cross-state",
            help="Merge the same posting syndicated to several states (employer + title block).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="With --cross-state: report merges, write nothing."),
    ] = False,
) -> None:
    """Run dedupe passes by hand."""
    if not (apply_url or cross_state):
        typer.echo("nothing to do: pass --apply-url and/or --cross-state", err=True)
        raise typer.Exit(2)
    if dry_run and not cross_state:
        typer.echo("--dry-run only applies to --cross-state", err=True)
        raise typer.Exit(2)
    from jobhunter.pipeline.dedupe_url import merge_by_apply_url
    from jobhunter.pipeline.dedupe_xstate import merge_cross_state

    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    conn = db.connect(db_path)
    try:
        if not dry_run:
            _migrate(conn)
        now = datetime.now(UTC)
        if apply_url:
            res = merge_by_apply_url(conn, now=now)
            typer.echo(", ".join(f"{k}={v}" for k, v in vars(res).items()))
        if cross_state:
            xres = merge_cross_state(conn, now=now, dry_run=dry_run)
            typer.echo(
                ("dry-run: would merge " if dry_run else "merged ") + f"{xres.would_merge} groups"
            )
            typer.echo(", ".join(f"{k}={v}" for k, v in vars(xres).items()))
        if not dry_run:
            # A merge can abandon a packet: its employer-named copies go now (019561b (3)).
            from jobhunter.apply.export import sync_abandoned

            sync_abandoned(conn, resolve_path(settings.paths.data_dir))
    finally:
        conn.close()


@app.command(name="backfill-salary")
def backfill_salary_cmd() -> None:
    """Fill missing salaries from description text (free, local, idempotent; specs/003)."""
    from jobhunter.pipeline.normalize import backfill_salary

    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    conn = db.connect(db_path)
    try:
        _migrate(conn)
        before, after = backfill_salary(conn)
    finally:
        conn.close()
    for label, counts in (("before", before), ("after", after)):
        typer.echo(f"{label}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


@app.command(name="paths")
def paths_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show each path, where it came from (default, config file, env) and whether it exists."""
    from jobhunter import legacy_data
    from jobhunter.config import config_file, config_file_source, models_file

    settings = load_settings()
    config_file_path = config_file(None)
    sources = settings.paths.sources
    router = settings.scoring.openrouter
    catalog_source = (
        "config file" if "catalog_cache" in router.model_fields_set else "under cache_dir"
    )
    if container.is_container() and not as_json:
        typer.echo("mode: container")
    rows = [
        ("config_file", config_file_path, config_file_source()),
        ("data_dir", settings.paths.data_dir, sources["data_dir"]),
        ("db_path", settings.paths.db_path, sources["db_path"]),
        ("profile_dir", settings.paths.profile_dir, sources["profile_dir"]),
        ("resume_path", settings.paths.resume_path, sources["resume_path"]),
        ("cache_dir", settings.paths.cache_dir, sources["cache_dir"]),
        ("openrouter_catalog", router.catalog_cache, catalog_source),
        # Model choices saved on /models or with `jobhunter models set` (specs/020).
        ("models_file", models_file(settings.paths.data_dir), "under data_dir"),
    ]
    resolved = [(name, resolve_path(path), source) for name, path, source in rows]
    status = legacy_data.check(settings)
    if as_json:
        info: dict[str, object] = {
            name: {"path": str(path), "source": source, "exists": path.exists()}
            for name, path, source in resolved
        }
        info["old_layout"] = {
            "state": status.kind,
            "unmigrated": [str(p) for p in status.unmigrated],
            "stale": [str(p) for p in status.stale],
            "message": status.message,
        }
        info["mode"] = "container" if container.is_container() else "host"
        typer.echo(json.dumps(info, indent=2))
        return
    for name, path, source in resolved:
        mark = "exists" if path.exists() else "missing"
        typer.echo(f"{name:<19} {mark:<8} {path}  [{source}]")
    if status.blocked:
        typer.echo(f"stopped: {status.message}")
    for note in status.warnings:
        typer.echo(note)


@app.command(name="migrate-paths")
def migrate_paths_cmd(
    apply: Annotated[
        bool, typer.Option("--apply", help="Do the move (default: a dry run that changes nothing).")
    ] = False,
    from_dir: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="Folder holding the old data/, profile/ and resume/ (default: the main "
            "checkout or the working directory, wherever data/jobhunter.db is).",
        ),
    ] = None,
) -> None:
    """Move the old ./data, ./profile and ./resume to the XDG locations (dry run unless --apply).

    --apply needs the database closed everywhere (stop the console and the timers). It copies,
    verifies, then renames the old folders to *.migrated-YYYYMMDD; it never deletes anything.
    """
    from jobhunter.ops import migrate_paths as mp

    _host_only("migrate-paths")
    settings = load_settings()
    plan = mp.plan(settings, from_dir=from_dir)
    for line in mp.render_plan(plan, apply=apply):
        typer.echo(line)
    if plan.problems:
        raise typer.Exit(1)
    if not apply:
        return
    if plan.nothing_to_do:
        if mp.secure_data(settings):
            typer.echo("checked: the data directory and its files are owner-only.")
        return
    try:
        for line in mp.apply(plan, settings):
            typer.echo(line)
    except mp.MigrateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def init() -> None:
    """Set up a fresh install: the data directory (0700) and an empty database (0600).

    Refuses while data from the old repo-relative layout exists; `jobhunter migrate-paths`
    moves that instead.
    """
    from jobhunter import legacy_data
    from jobhunter.ops.migrate_paths import secure_data
    from jobhunter.xdg import mkdir_private

    settings = load_settings()
    status = legacy_data.check(settings)
    if status.unmigrated:
        old = ", ".join(map(str, status.unmigrated))
        typer.echo(
            f"error: your data is still in {old}; run `jobhunter migrate-paths` to move it "
            "instead of starting a new database.",
            err=True,
        )
        raise typer.Exit(1)
    db_path = resolve_path(settings.paths.db_path)
    existed = db_path.exists()
    mkdir_private(resolve_path(settings.paths.data_dir))
    conn = db.connect(db_path)
    try:
        _migrate(conn)
    finally:
        conn.close()
    secure_data(settings)
    if existed:
        typer.echo(f"already set up: {db_path} (schema up to date, permissions checked)")
        return
    typer.echo(f"created {db_path}")
    typer.echo(
        f"next: open http://{settings.console.host}:{settings.console.port}/setup, "
        "or run `jobhunter doctor`"
    )
    typer.echo(
        f"new here? read http://{settings.console.host}:{settings.console.port}"
        "/help/getting-started"
    )


def _warn_essentials(settings: Settings, host: str, port: int) -> None:
    """One stderr line at console start when an essential is failing (019 "Banner")."""
    from jobhunter import readiness

    try:
        checks = readiness.run_checks(
            settings,
            secrets_report=_SECRETS_REPORT,
            now=datetime.now(UTC),
            mode=readiness.detect_mode(settings=settings),
            cheap_only=True,
        )
    except Exception:  # a hint must never stop the console starting
        return
    n = sum(1 for c in checks if c.level == readiness.ESSENTIAL and c.status in readiness.FAILING)
    if n:
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        typer.echo(
            f"setup: {n} essential item(s) missing or broken; open http://{shown}:{port}/setup",
            err=True,
        )


@app.command()
def console(
    host: Annotated[str | None, typer.Option(help="Bind host (default: config).")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port (default: config).")] = None,
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help=(
                "Allow a non-loopback host (no auth!). Without it every request must name "
                "localhost or a loopback IP in Host; foo.localhost, 0.0.0.0 or an /etc/hosts "
                "alias get 403."
            ),
        ),
    ] = False,
) -> None:
    """Start the local web console."""
    import uvicorn

    from jobhunter.config import load_settings
    from jobhunter.console.app import check_host, create_app

    settings = load_settings()
    bind_host = host or settings.console.host
    bind_port = port or settings.console.port
    try:
        # JOBHUNTER_PUBLISHED_LOOPBACK_ONLY=1 (container mode) lets the bind be 0.0.0.0 because
        # the unit publishes the port on host loopback only. The Host/Origin check still sees
        # allow_remote=False, so a non-loopback Host is refused either way.
        interlock = container.published_loopback_only() and bind_host == "0.0.0.0"
        check_host(bind_host, allow_remote or interlock)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    from jobhunter.core.runlock import ConsoleBusy

    try:
        app_ = create_app(
            settings,
            allow_remote=allow_remote,
            ext_port=settings.console.public_port or bind_port,
        )
    except ConsoleBusy as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    app_.state.secrets_report = _SECRETS_REPORT  # the secret rows on /setup
    _warn_essentials(settings, bind_host, bind_port)
    shown = "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host
    typer.echo(f"help: http://{shown}:{bind_port}/help/getting-started", err=True)
    from jobhunter.config_watch import ConfigWatcher

    # Fields set on the command line win over config: a change to them has no effect (019 B).
    pinned = [f for f, v in (("console.host", host), ("console.port", port)) if v is not None]
    app_.state.config_watch = ConfigWatcher(cli_overrides=pinned)
    _serve(app_, bind_host, bind_port, uvicorn)


def _serve(app_, bind_host: str, bind_port: int, uvicorn) -> None:
    """Run uvicorn with a handle on the server, so a console Restart can stop it gracefully
    and then re-exec or exit 42 (specs/019 Addendum B "Restart", S12b)."""
    from jobhunter.console import restart
    from jobhunter.ops import version

    config = uvicorn.Config(
        app_, host=bind_host, port=bind_port, timeout_graceful_shutdown=restart.drain_seconds()
    )
    server = uvicorn.Server(config)
    how = restart.kind()
    if how is not None:

        def shutdown() -> None:
            app_.state.restart_requested = True
            server.should_exit = True

        app_.state.restart_shutdown = shutdown
    app_.state.restart_kind = how
    app_.state.bind_host = bind_host
    app_.state.started_commit = version.build_commit()[0]
    interrupted = False
    try:
        server.run()
    except KeyboardInterrupt:  # uvicorn re-raises a captured Ctrl-C after it has stopped
        interrupted = True
    if not server.started:
        raise typer.Exit(3)  # uvicorn's STARTUP_FAILURE, never EXIT_RESTART
    # Ctrl-C wins over an accepted restart: you asked the console to stop, so it stops.
    if app_.state.restart_requested and how is not None and not interrupted:
        restart.after_serve(
            how=how, leftover=app_.state.work.wait_idle, drain=restart.drain_seconds()
        )


@ext_app.command("pair")
def ext_pair() -> None:
    """Make a one-time pairing code for the extension's options page (valid 10 minutes)."""
    from jobhunter.apply import ext_pairing

    settings = load_settings()
    code, expires = ext_pairing.new_code(
        ext_pairing.state_path(settings.paths.data_dir), datetime.now(UTC)
    )
    typer.echo(f"Pairing code: {code}")
    typer.echo(
        f"Type it on the jobhunter extension's options page before "
        f"{expires.astimezone():%H:%M}. It works once; pairing revokes any earlier pairing."
    )


@ext_app.command("unpair")
def ext_unpair() -> None:
    """Revoke the extension's token (and any pending code)."""
    from jobhunter.apply import ext_pairing

    settings = load_settings()
    if ext_pairing.unpair(ext_pairing.state_path(settings.paths.data_dir)):
        typer.echo("Unpaired: the extension's token no longer works.")
    else:
        typer.echo("Nothing was paired.")


@ext_app.command("status")
def ext_status() -> None:
    """Whether an extension is paired (never prints the token)."""
    from jobhunter.apply import ext_pairing

    settings = load_settings()
    st = ext_pairing.status(ext_pairing.state_path(settings.paths.data_dir), datetime.now(UTC))
    typer.echo(f"paired: {'yes, since ' + st['paired_at'] if st['paired'] else 'no'}")
    if st["code_expires_at"]:
        typer.echo(f"a pairing code is waiting until {st['code_expires_at']}")


@app.command(name="eval")
def eval_(
    prompt_version: Annotated[
        str | None, typer.Option("--prompt-version", help="Default: newest scored.")
    ] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    tier: Annotated[str, typer.Option("--tier", help="screen or deep.")] = "screen",
    compare: Annotated[
        str | None,
        typer.Option("--compare", help="Variants: prompt_version[@model], two or more."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    more: Annotated[list[str] | None, typer.Argument(hidden=True)] = None,
) -> None:
    """Measure scorer quality against hand labels (specs/006 Calibration).

    `eval --compare A B [C...]` compares prompt_version[@model] variants on the same labels.
    """
    from jobhunter.scoring import evaluate as ev
    from jobhunter.scoring.profile import ProfileError, load_profile_for

    if tier not in ("screen", "deep"):
        typer.echo("--tier must be screen or deep", err=True)
        raise typer.Exit(2)
    specs = [compare, *(more or [])] if compare is not None else []
    if compare is not None and len(specs) < 2:
        typer.echo("--compare needs at least two variants", err=True)
        raise typer.Exit(2)
    if compare is None and more:
        typer.echo("unexpected arguments; use --compare A B", err=True)
        raise typer.Exit(2)
    settings = load_settings()
    try:
        profile = load_profile_for(settings)
    except ProfileError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(1) from exc
    conn = db.connect(settings.paths.db_path)
    _migrate(conn)
    try:
        if compare is not None:
            try:
                keys = [ev.parse_variant(s) for s in specs]
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(2) from exc
            result: ev.EvalReport | ev.CompareReport = ev.compare(conn, profile, keys, tier=tier)
        else:
            result = ev.evaluate(
                conn, profile, prompt_version=prompt_version, model=model, tier=tier
            )
    finally:
        conn.close()
    typer.echo(result.to_json() if as_json else result.to_text())


@sources_app.command("list")
def sources_list(
    enabled: Annotated[bool, typer.Option("--enabled", help="Only enabled sources.")] = False,
    state: Annotated[str | None, typer.Option(help="Only this state code, e.g. NY.")] = None,
) -> None:
    """List configured sources."""
    rows = load_registry()
    if enabled:
        rows = enabled_sources(rows)
    if state:
        rows = [r for r in rows if (r.state or "").upper() == state.upper()]
    if not rows:
        typer.echo("no matching sources")
        return
    typer.echo(f"{'KEY':<24}{'ST':<4}{'FAMILY':<11}{'TIER':<9}{'POLICY':<9}ROBOTS")
    for r in rows:
        typer.echo(
            f"{r.key:<24}{(r.state or '-'):<4}{r.family:<11}{r.tier.value:<9}"
            f"{r.policy.value:<9}{r.robots.status}"
        )


@sources_app.command("verify")
def sources_verify(
    state: Annotated[
        str | None, typer.Option(help="Comma list of state codes; US includes national rows.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    wait: WaitOption = None,
) -> None:
    """Probe every registry entry and diff against recorded values (specs/003 breakage detection).

    Meant to run weekly (a timer will schedule it). Fetches each entry URL and robots.txt through
    FetchContext (robots and rate limits apply, so it is slow by design), checks family signals,
    the robots.txt hash, and the shared JobLink bundle hash. Writes the source table's robots and
    verified columns, never registry.yaml. Exits 1 when any source drifted, broke or was blocked.
    """
    from jobhunter.sources import verify as vf

    wait_s = _parse_wait(wait)
    settings = load_settings()
    with _job_lock(settings, "sources verify", wait_s):
        conn = db.connect(resolve_path(settings.paths.db_path))
        _migrate(conn)
        rows = load_registry()
        try:
            sync_sources_table(conn, rows)
            report = vf.verify_all(
                conn,
                rows,
                now=datetime.now(UTC),
                states=runner.parse_states(state),
                settings=settings,
                on_result=None if as_json else lambda r: typer.echo(vf.format_row(r)),
            )
        finally:
            conn.close()
    if as_json:
        typer.echo(report.to_json())
    else:
        typer.echo(vf.format_summary(report))
    if not report.clean:
        raise typer.Exit(1)


@mail_app.command("auth")
def mail_auth(
    manual: Annotated[
        bool,
        typer.Option("--manual", help="No local server: paste the redirected URL back here."),
    ] = False,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Loopback port (default: random; 8765 with --manual)."),
    ] = None,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not try to open a browser; print the URL.")
    ] = False,
    podman_secret: Annotated[
        str | None,
        typer.Option(
            "--podman-secret",
            metavar="NAME",
            help="Store the token as podman secret NAME (stdin only) instead of locally.",
        ),
    ] = None,
    keep_local: Annotated[
        bool,
        typer.Option("--keep-local", help="With --podman-secret, also keep a local copy."),
    ] = False,
    from_local: Annotated[
        bool,
        typer.Option(
            "--from-local",
            help="With --podman-secret: move the locally stored token, no new consent.",
        ),
    ] = False,
) -> None:
    """Run the Gmail OAuth flow and store the refresh token (OS keyring, else a 0600 file).

    For the container deployment, run on the host: `jobhunter mail auth --podman-secret
    jobhunter_gmail_token`. While the Google app is in Testing mode the refresh token
    expires after 7 days, so repeat that one command weekly; it checks podman first, then
    replaces the secret. If podman fails after consent, the token is kept locally.
    """
    from jobhunter.config import load_settings
    from jobhunter.mail import auth

    _host_only("mail auth", "jobhunter mail auth --podman-secret jobhunter_gmail_token")
    settings = load_settings()
    if (keep_local or from_local) and podman_secret is None:
        typer.echo("error: --keep-local and --from-local need --podman-secret.", err=True)
        raise typer.Exit(2)
    if podman_secret is not None:
        try:
            outcome = auth.authenticate_to_podman_secret(
                settings,
                podman_secret,
                manual=manual,
                port=port,
                open_browser=not no_browser,
                keep_local=keep_local,
                from_local=from_local,
                echo=lambda line: typer.echo(line, err=True),
            )
        except auth.MailAuthError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        if outcome == "local":
            raise typer.Exit(1)
        if outcome == "new":
            typer.echo(
                f"Gmail authorised; created podman secret {podman_secret}. Next: rerun "
                "`jobhunter container preflight`, then `systemctl --user daemon-reload` and "
                "`systemctl --user restart jobhunter-console`. The scheduled runs pick the "
                "secret up at their next run; nothing here starts a run."
            )
        else:
            typer.echo(
                f"Gmail authorised; replaced podman secret {podman_secret}. Nothing else to "
                "do; the next run picks up the new token. Repeat weekly while the app is in "
                "Testing mode."
            )
        return
    try:
        where = auth.authenticate(settings, manual=manual, port=port, open_browser=not no_browser)
    except auth.MailAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Gmail authorised; token stored in the {where}.")


@mail_app.command("setup")
def mail_setup(
    xml: Annotated[
        bool, typer.Option("--xml", help="Print Gmail filter XML; no OAuth needed.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be created; write nothing.")
    ] = False,
) -> None:
    """Create the alerts label and the +jobs filters in Gmail (idempotent)."""
    from jobhunter.config import load_settings
    from jobhunter.mail import auth, setup

    settings = load_settings()
    try:
        if xml:
            for w in setup.domain_warnings(settings):
                typer.echo(f"warning: {w}", err=True)
            typer.echo(setup.filters_xml(settings), nl=False)
            return
        setup.validate_address(settings)
        if dry_run and (auth.load_token() is None):
            # No credentials: show the plan without looking at the mailbox.
            typer.echo(f"would ensure label {settings.mail.label}")
            for crit in setup.wanted_filters(settings):
                typer.echo(f"would ensure filter {setup.describe(crit)}")
            for w in setup.domain_warnings(settings):
                typer.echo(f"warning: {w}")
            return
        service = auth.build_service(settings)
        report = setup.setup_mailbox(service, settings, dry_run=dry_run)
    except (setup.MailSetupError, auth.MailAuthError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for line in report.lines():
        typer.echo(line)
    if not dry_run:
        _record_mail_setup(settings)


def _record_mail_setup(settings) -> None:
    """Record ``mail_setup_at`` for the achievements board (specs/021 ``setup.mail``) after a
    real, successful ``mail setup``. The mailbox is already set up by now, so a database that
    cannot take the write (pending container migration, busy) is reported, not fatal."""
    import sqlite3

    from jobhunter.core import achievement_setting

    try:
        conn = db.connect(resolve_path(settings.paths.db_path))
        try:
            _migrate(conn)
            achievement_setting.record_mail_setup(conn, datetime.now(UTC))
        finally:
            conn.close()
    except (db.SchemaError, sqlite3.Error) as exc:
        typer.echo(f"note: mailbox set up, but not recorded in the database: {exc}", err=True)


@mail_app.command("status")
def mail_status() -> None:
    """Show token, label, filter and message-count status (read-only)."""
    from jobhunter.config import load_settings
    from jobhunter.mail import auth, setup

    settings = load_settings()
    has_token = auth.load_token() is not None
    where = auth.token_location() if has_token else None
    typer.echo(f"token: {where or ('present' if has_token else 'no')}")
    if not has_token:
        typer.echo("run: jobhunter mail auth")
        raise typer.Exit(1)
    try:
        info = setup.mailbox_status(auth.build_service(settings), settings)
    except (setup.MailSetupError, auth.MailAuthError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"label {info['label']}: {'present' if info['label_present'] else 'missing'}")
    for desc, ok in info["filters"].items():
        typer.echo(f"filter {desc}: {'present' if ok else 'missing'}")
    if info["messages"] is not None:
        typer.echo(f"messages under label: {info['messages']}")


@mail_app.command("match")
def mail_match(
    days: Annotated[int, typer.Option(help="How many days back to scan.")] = 14,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print proposals; write nothing.")
    ] = False,
) -> None:
    """Propose application events/records from recent mail (never applied automatically).

    Rejection emails are also recorded as employer rejections (matched to a job or not).
    """
    from jobhunter.config import load_settings, resolve_path
    from jobhunter.core import db
    from jobhunter.mail import auth, match
    from jobhunter.mail.gmail_api import GmailRateLimited

    settings = load_settings()
    if auth.load_token() is None:
        typer.echo("no Gmail token in the keyring; skipping. Run: jobhunter mail auth")
        return
    conn = db.connect(resolve_path(settings.paths.db_path))
    try:
        _migrate(conn)
        result = match.scan(
            conn, auth.build_service(settings), settings, days=days, dry_run=dry_run
        )
    except (auth.MailAuthError, GmailRateLimited) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    for p in result.proposals:
        ev = p.evidence
        typer.echo(
            f"{p.confidence:.2f} {p.kind:<15} {p.proposed_action} -> {p.proposed_status} "
            f"| {ev.get('sender', '')} | {ev.get('subject', '')}"
        )
    for r in result.rejections:
        where = f"job group {r.job_group_id}" if r.job_group_id else "no known job"
        review = "" if r.confirmed else " | needs review on /rejections"
        typer.echo(
            f"employer rejection: {r.employer or 'employer unknown'} | "
            f"{r.title or 'title unknown'} | {r.received_at[:10]} | {where}{review}"
        )
    verb = "would store" if dry_run else "stored"
    n = len(result.proposals) if dry_run else result.stored
    n_rej = len(result.rejections) if dry_run else result.rejections_stored
    dupes = f"; {result.thread_duplicates} repeats in a thread skipped" * bool(
        result.thread_duplicates
    )
    typer.echo(
        f"scanned {result.scanned} new messages ({result.already_seen} seen before); "
        f"{verb} {n} proposals and {n_rej} employer rejections{dupes}"
    )


@mail_app.command("sync")
def mail_sync(
    limit: Annotated[int, typer.Option(help="Most alert messages to read this run.")] = 200,
) -> None:
    """Ingest job-alert emails: list, resolve (robots permitting), normalize, dedupe."""
    from jobhunter.config import load_settings
    from jobhunter.mail import auth, sync
    from jobhunter.mail.gmail_api import GmailRateLimited
    from jobhunter.sources.adapters.mailalerts import SOURCE_KEY, MailAlertsAdapter

    settings = load_settings()
    why = MailAlertsAdapter.unavailable(settings)
    if why:
        typer.echo(f"mail sync skipped: {why}")
        return
    rows = load_registry()
    row = next((r for r in rows if r.key == SOURCE_KEY), None)
    if row is None or row.policy.value != "enabled":
        typer.echo(f"mail sync skipped: source {SOURCE_KEY} is not enabled in the registry")
        return
    adapter = MailAlertsAdapter(rows=rows, limit=limit)
    db_path = resolve_path(settings.paths.db_path)
    conn = db.connect(db_path)
    try:
        _migrate(conn)
        report = runner.run_pipeline(
            conn,
            settings,
            [row],
            adapters={row.family: lambda: adapter},
            profile_loader=lambda: None,
            stages=["list", "resolve", "normalize", "dedupe"],
        )
    except (auth.MailAuthError, sync.MailSyncError, GmailRateLimited) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    st = adapter.stats
    fams = ", ".join(f"{k} {v}" for k, v in sorted(st.families.items())) or "none"
    typer.echo(
        f"messages {st.messages} ({fams}); entries {st.entries}: "
        f"{st.new} new, {st.sightings} sightings of jobs already held in full"
    )
    summary = runner.format_summary(report)
    if summary:
        typer.echo(summary)
    raise typer.Exit(report.exit_code)


@mail_app.command("sample")
def mail_sample(
    save: Annotated[Path, typer.Option("--save", help="Directory for the .eml copies.")],
    n: Annotated[int, typer.Option("-n", "--count", help="How many messages.")] = 10,
) -> None:
    """Save the newest labelled alerts as scrubbed .eml files, to re-validate the parsers.

    Your address (mail.alerts_address, the mailbox address, and every To/Cc address) is
    replaced with a placeholder and routing headers are dropped. Review before committing.
    """
    from jobhunter.config import load_settings
    from jobhunter.mail import auth, sync

    settings = load_settings()
    try:
        service = auth.build_service(settings)
        me = service.users().getProfile(userId="me").execute().get("emailAddress", "")
        addresses = [a for a in (settings.mail.alerts_address, me) if a and "<" not in a]
        saved = sync.save_samples(service, settings.mail.label, save, n, addresses)
    except (auth.MailAuthError, sync.MailSyncError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for path in saved:
        typer.echo(f"saved {path}")
    typer.echo(
        f"{len(saved)} message(s) saved. Review them, then copy into "
        "tests/fixtures/mail/<family>/ and add parser tests."
    )


@applylinks_app.command("unknown")
def applylinks_unknown(
    top: Annotated[int, typer.Option(help="How many hosts to show.")] = 20,
) -> None:
    """List the most common apply-link destinations with no ATS rule yet."""
    from jobhunter.config import load_settings, resolve_path
    from jobhunter.core import db
    from jobhunter.pipeline.applylink import unknown_hosts

    path = resolve_path(load_settings().paths.db_path)
    if not path.is_file():
        typer.echo("no apply links resolved yet")
        return
    conn = db.connect(path)
    try:
        _migrate(conn)
        rows = unknown_hosts(conn, top=top)
    finally:
        conn.close()
    if not rows:
        typer.echo("no unknown apply-link hosts")
        return
    typer.echo(f"{'COUNT':>6}  HOST")
    for host, n in rows:
        typer.echo(f"{n:>6}  {host}")


@schedule_app.command("install")
def schedule_install(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the units and commands; write nothing.")
    ] = False,
    enable: Annotated[
        bool,
        typer.Option("--enable", help="Also run systemctl daemon-reload and enable --now."),
    ] = False,
) -> None:
    """Write systemd --user units to ~/.config/systemd/user (specs/002 Process model)."""
    from jobhunter.ops import schedule

    _host_only("schedule install")
    try:
        inst = schedule.default_install()
        typer.echo(schedule.install(inst, dry_run=dry_run, enable=enable))
    except schedule.ScheduleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2 if dry_run and enable else 1) from exc


@schedule_app.command("status")
def schedule_status() -> None:
    """Show the jobhunter timers (systemctl --user list-timers, read-only)."""
    from jobhunter.ops import schedule

    _host_only("schedule status")
    try:
        typer.echo(schedule.status())
    except schedule.ScheduleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Disable the timers and remove the unit files."""
    from jobhunter.ops import schedule

    _host_only("schedule uninstall")
    try:
        typer.echo(schedule.uninstall())
    except schedule.ScheduleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@llm_app.command("status")
def llm_status() -> None:
    """Is the local model server (scoring.local) reachable, and which models are loaded?"""
    from jobhunter.config import load_settings
    from jobhunter.scoring.scorers import local_server_status

    cfg = load_settings().scoring.local
    status = local_server_status(cfg)
    typer.echo(f"runtime: {cfg.runtime}")
    typer.echo(f"url: {status.url}")
    if status.unauthorized:
        typer.echo("server: up, but it rejected this client (HTTP 401 Unauthorized)")
        typer.echo(
            f"set {cfg.api_key_env or 'api_key_env'} in ~/.env or the environment to the key "
            "the server was started with"
        )
        raise typer.Exit(1)
    if not status.reachable:
        typer.echo(f"server: DOWN ({status.detail})")
        typer.echo(
            "start it with scripts/setup-local-llm.sh (prints the command; installs nothing)"
        )
        raise typer.Exit(1)
    typer.echo("server: up" + (f" ({status.detail})" if status.detail else ""))
    if status.models:
        typer.echo("models:")
        for m in status.models:
            typer.echo(f"  {m}")
    else:
        typer.echo("models: none reported")


@llm_app.command("bench")
def llm_bench(
    scorer_spec: Annotated[
        str | None,
        typer.Option(
            "--scorer",
            metavar="SPEC",
            help="local:<model>, openrouter:<slug>, jev:<slug> or anthropic:<model>. "
            "Default: scoring.screen_scorer.",
        ),
    ] = None,
    n: Annotated[int, typer.Option("--n", min=1, help="How many job groups to score.")] = 10,
    night_hours: Annotated[
        float, typer.Option(help="Hours available overnight, for the jobs-per-night projection.")
    ] = 8.0,
    jobs_per_request: Annotated[
        int | None,
        typer.Option(
            "--jobs-per-request",
            min=1,
            max=25,
            help="Pack this many jobs per request; reports seconds and cost per job too "
            "(at most 16 for chat scorers, 25 for jev:/decisions:).",
        ),
    ] = None,
) -> None:
    """Time a scorer on N ingested jobs. Writes no fit_score rows (specs/016)."""
    from jobhunter.config import load_settings
    from jobhunter.core import db
    from jobhunter.scoring.bench import run_bench
    from jobhunter.scoring.profile import ProfileError, load_profile_for
    from jobhunter.scoring.scorers import ScorerError, privacy_notice, scorer_from_string

    settings = load_settings()
    spec = scorer_spec or settings.scoring.screen_scorer
    # Without --scorer the bench calls the configured scorer for real: a use blocked by an
    # invalid models.toml must not run (specs/020).
    _check_scorer_override(settings, scorer_spec)
    try:
        profile = load_profile_for(settings)
        scorer = scorer_from_string(spec, scoring=settings.scoring)
    except (ProfileError, ScorerError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if notice := privacy_notice(spec, settings.scoring):
        typer.echo(notice, err=True)
    from jobhunter.scoring import decisions

    is_decisions = isinstance(scorer, decisions.DecisionsScorer)
    if jobs_per_request is not None and jobs_per_request > 16 and not is_decisions:
        typer.echo("--jobs-per-request is at most 16 for chat scorers", err=True)
        raise typer.Exit(2)
    conn = db.connect(settings.paths.db_path)
    _migrate(conn)
    try:
        if isinstance(scorer, decisions.DecisionsScorer):  # decisions.py: packed questions
            if jobs_per_request is not None:
                scorer.jobs_per_request = jobs_per_request
            report = decisions.run_bench(conn, scorer, profile, n=n, night_hours=night_hours)
        else:
            report = run_bench(
                conn,
                scorer,
                profile,
                n=n,
                night_hours=night_hours,
                jobs_per_request=jobs_per_request,
            )
    finally:
        conn.close()
    if report.attempted == 0:
        typer.echo("no prefiltered job groups to score; run jobhunter run first", err=True)
        raise typer.Exit(1)
    typer.echo(report.format())


if __name__ == "__main__":
    app()


@apply_app.command("draft")
def apply_draft(
    packet_id: Annotated[int, typer.Argument(help="The packet id (/packet/<id> in the console).")],
    letter: Annotated[bool, typer.Option("--letter", help="Draft the cover letter.")] = False,
    question: Annotated[
        str | None,
        typer.Option(
            "--question",
            help="Draft an answer to this custom question; '-' reads it from stdin.",
        ),
    ] = None,
    question_file: Annotated[
        Path | None,
        typer.Option("--question-file", help="Read the custom question from this file."),
    ] = None,
    story: Annotated[
        list[str] | None,
        typer.Option("--story", help="A story fact for a behavioral question (up to 3)."),
    ] = None,
    instruction: Annotated[
        str, typer.Option("--instruction", help='One-line instruction, e.g. "shorter".')
    ] = "",
    runner: Annotated[
        str | None,
        typer.Option("--runner", help="cli (subscription) or api; default: config apply.runner."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before a run that spends money.")
    ] = False,
) -> None:
    """Draft a targeted resume (default), a cover letter or a question answer for a packet.

    Writes the same packet_document versions as the console. The CLI runner uses your Claude
    subscription; the API runs only with --runner api, after showing its estimate. A failed
    subscription run never switches to the API by itself.
    """
    from jobhunter.apply import answers, documents, export, generator, labels, runner_state
    from jobhunter.scoring.profile import ProfileError, load_profile_for

    _host_only("apply draft")
    if question_file is not None:
        if question is not None:
            typer.echo("error: pick one of --question and --question-file", err=True)
            raise typer.Exit(2)
        try:
            question = question_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: cannot read {question_file}: {exc}", err=True)
            raise typer.Exit(2) from exc
    elif question == "-":
        question = sys.stdin.read()
    if question is not None:
        question = question.strip()
        if not question:
            typer.echo("error: the question is empty", err=True)
            raise typer.Exit(2)
    if letter and question:
        typer.echo("error: pick one of --letter and --question", err=True)
        raise typer.Exit(2)
    kind = "cover_letter" if letter else "question_draft" if question else "resume"
    settings = load_settings()
    for line in settings.models_notices:  # an invalid models.toml [apply] key fell back
        typer.echo(f"notice: {line}", err=True)
    run_on = runner or settings.apply.runner
    if run_on not in ("cli", "api"):
        typer.echo("error: --runner is cli or api", err=True)
        raise typer.Exit(2)
    try:
        profile = load_profile_for(settings)
    except ProfileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    conn = db.connect(settings.paths.db_path)
    _migrate(conn)
    now = datetime.now(UTC)
    try:
        if question is not None:
            if labels.never_store(question) is not None:
                typer.echo(labels.YOURS, err=True)
                raise typer.Exit(1)
            if labels.question_kind(question) == "numeric":
                typer.echo("A years-of-experience question gets resume evidence, never a number:")
                lines = documents.numbered_resume(profile.resume_text)
                for lid, text in generator.numeric_evidence(lines, question) or [("", "none")]:
                    typer.echo(f"  {lid} {text}".rstrip())
                return
            if story:
                facts = answers.clean_lines(story)
                answers.save_packet_answer(
                    conn, packet_id, answers.story_key(question), question, "\n".join(facts)
                )
        confirm_paid = False
        ctx = generator.load_context(conn, profile, packet_id)
        req = generator.build_request(
            conn, ctx, kind, question=question or "", instruction=instruction
        )
        est = generator.estimate(conn, settings, kind, req.chars, runner=run_on)
        left = generator.remaining_cap(conn, settings, now)
        if run_on == "api":
            typer.echo(
                f"API run: about ${est.usd:.2f} ({est.source}); "
                f"apply cap ${max(left, 0):.2f} left of ${settings.apply.daily_cap_usd:.2f} today"
            )
            if not yes and not typer.confirm("Run it? This spends API credit.", default=False):
                typer.echo("not run")
                raise typer.Exit(1)
        elif runner_state.load(settings.paths.data_dir).overage:
            typer.echo(
                f"Your subscription is on paid extra usage: this run is charged (about "
                f"${est.usd:.2f}) against the apply cap (${max(left, 0):.2f} left)."
            )
            if not yes and not typer.confirm("Run it anyway?", default=False):
                typer.echo("not run")
                raise typer.Exit(1)
            confirm_paid = True
        out = generator.generate(
            conn,
            settings,
            profile,
            packet_id,
            kind,
            runner=run_on,
            now=now,
            question=question or "",
            instruction=instruction,
            confirm_paid=confirm_paid,
        )
        # A new version is current now: files named for employers must not hold an older one.
        if (warn := export.safe_sync(conn, settings.paths.data_dir, packet_id)) is not None:
            typer.echo(warn, err=True)
    except generator.ApplyRefused as exc:
        typer.echo(f"not run: {exc.message}", err=True)
        if exc.offer_api:
            typer.echo(_api_hint(packet_id, letter, question), err=True)
        raise typer.Exit(1) from exc
    except generator.GenerateFailed as exc:
        typer.echo(f"failed, nothing saved: {exc.reason}", err=True)
        if exc.offer_api:
            typer.echo(_api_hint(packet_id, letter, question), err=True)
        raise typer.Exit(1) from exc
    except (answers.NeverStore, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    where = "subscription" if out.runner == "cli" else f"API, ${out.cost_usd:.3f}"
    check = "all lines checked" if out.ok else "has unsupported lines to fix or confirm"
    typer.echo(
        f"{kind.replace('_', ' ')} v{out.version} written ({where}); {check}; "
        f"entailment {out.entailment}. Review it at /packet/{packet_id}"
    )


@apply_app.command("stats")
def apply_stats(
    days: Annotated[
        int | None, typer.Option("--days", help="Only the last N days (default: all time).")
    ] = None,
) -> None:
    """How assisted apply is used: packets by status and board, ready packets on the four
    public ATSs (the phase 2 gate), documents, saved answers (counts only), packet spend and,
    once phase 1e has landed, captures. Writes no rows and spends nothing."""
    from jobhunter.apply import stats

    settings = load_settings()
    conn = db.connect(settings.paths.db_path)
    try:
        _migrate(conn)
        typer.echo(stats.compute(conn, datetime.now(UTC), days).format())
    finally:
        conn.close()


@app.command(name="version")
def version_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Package version, git commit, highest migration, and the database's schema (read-only).

    Never migrates and never writes; a database newer than this code is reported, not refused.
    """
    from jobhunter.ops import version as ver

    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    info = ver.collect(db_path, db.owning_data_dir(db_path, resolve_path(settings.paths.data_dir)))
    if as_json:
        typer.echo(json.dumps(info.as_dict(), indent=2))
        return
    for line in info.lines():
        typer.echo(line)


@db_app.command("migrate")
def db_migrate_cmd() -> None:
    """Apply pending migrations now, after a pre-migrate backup (backups/auto, newest 3 kept).

    The one way to migrate a container deployment (``jobhunter upgrade`` runs it); on a
    host-only install every command already does this on its own. Refuses a database newer
    than this code.
    """
    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    data_dir = db.owning_data_dir(db_path, resolve_path(settings.paths.data_dir))
    conn = db.connect(db_path)
    try:
        before = db.current_version(conn)
        applied = db.migrate(
            conn,
            data_dir=data_dir,
            explicit=True,
            on_backup=_say_backed_up,
            on_wait=_say_waiting,
        )
        after = db.current_version(conn)
    finally:
        conn.close()
    if not applied:
        typer.echo(f"schema up to date at {after} ({db_path})")
        return
    typer.echo(
        f"migrated {db_path} from {before} to {after}: applied {', '.join(map(str, applied))}"
    )


def _ctr_layout(settings):
    """Host side of the container mounts, from the host's own resolved paths (spec 018 D3)."""
    from jobhunter.config import config_file
    from jobhunter.ops import ctr

    return ctr.Layout(
        data_dir=resolve_path(settings.paths.data_dir),
        cache_dir=resolve_path(settings.paths.cache_dir),
        config_file=config_file(None).absolute(),
    )


def _source_checkout() -> Path:
    """The checkout this jobhunter runs from (src/jobhunter -> repo root)."""
    import jobhunter

    root = Path(jobhunter.__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file() or not (root / "Containerfile").is_file():
        typer.echo(
            f"error: jobhunter is not running from a source checkout ({root}); "
            "jobhunter upgrade builds the image from the checkout the host CLI runs from",
            err=True,
        )
        raise typer.Exit(1)
    return root


@app.command()
def upgrade(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print every step and command; run nothing.")
    ] = False,
    allow_dirty: Annotated[
        bool,
        typer.Option(
            "--allow-dirty",
            help="Build even with uncommitted changes in the image's inputs (tag <commit>-dirty).",
        ),
    ] = False,
    wait: Annotated[
        str,
        typer.Option(
            "--wait",
            metavar="DURATION",
            help="How long to wait for running jobhunter-ctr-* jobs before giving up "
            "(nothing changed).",
        ),
    ] = "30m",
) -> None:
    """Bring a container deployment up to this checkout (specs/018 Version skew, C15).

    Builds localhost/jobhunter:<commit>, stops the timers, waits for running jobs, takes the
    run lock, stops the console, backs up the database, retags :current, runs `jobhunter db
    migrate` in a one-off container, checks host and image versions match, then starts what
    was running. Any failure leaves the console and timers stopped and prints the rollback.
    Host only; on a host-only install (no deployment.toml) it is not needed.
    """
    from jobhunter.core.runlock import RunLock
    from jobhunter.ops import upgrade as up
    from jobhunter.ops.proc import SubprocessRunner

    _host_only("upgrade")
    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    data_dir = db.owning_data_dir(db_path, resolve_path(settings.paths.data_dir))
    if db.deployment_marker(data_dir) is None:
        typer.echo(
            f"not needed: no {data_dir / db.DEPLOYMENT_MARKER}, so this is a host-only install, "
            "where every command migrates on its own after a pre-migrate backup. jobhunter "
            "upgrade is for container deployments (after `jobhunter container preflight`)."
        )
        return
    ctx = up.Context(
        repo=_source_checkout(),
        db_path=db_path,
        data_dir=data_dir,
        layout=_ctr_layout(settings),
        runner=SubprocessRunner(),
        echo=typer.echo,
        lock=lambda: RunLock(db_path, "upgrade"),
        wait_s=_parse_wait(wait) or 0.0,
    )
    try:
        code = up.run(ctx, allow_dirty=allow_dirty, dry_run=dry_run)
    except up.UpgradeError as exc:
        typer.echo(f"error: {exc}; nothing was changed.", err=True)
        raise typer.Exit(1) from exc
    raise typer.Exit(code)


@app.command("ctr-script")
def ctr_script_cmd(
    write: Annotated[
        bool, typer.Option("--write", help="Install it (0755) instead of printing it.")
    ] = False,
    path: Annotated[
        Path | None,
        typer.Option(
            "--path", help="Where --write installs it. Default ~/.local/bin/jobhunter-ctr."
        ),
    ] = None,
) -> None:
    """Print or install the jobhunter-ctr wrapper (specs/018 One-off commands, C15).

    It runs one jobhunter command in a one-off container on localhost/jobhunter:current with
    the run unit's mounts; each secret of the jobhunter-ctr column is mounted when `podman
    secret exists` finds it at run time, so it needs no rerun after a secret change. Host only.
    """
    from jobhunter.ops import ctr

    _host_only("ctr-script")
    settings = load_settings()
    mounts = ctr.ctr_secret_mounts(settings)
    text = ctr.render_script(_ctr_layout(settings), mounts)
    if not write:
        typer.echo(text, nl=False)
        return
    try:
        dest = ctr.write_script(path or ctr.default_script_path(), text)
    except (ctr.CtrError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    names = ", ".join(m.secret for m in mounts)
    typer.echo(f"wrote {dest} (mounts whichever of these exist: {names})")


@db_app.command("relativize-paths")
def db_relativize_paths_cmd(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Back up the database, then make the changes listed."),
    ] = False,
) -> None:
    """Rewrite absolute file paths in the database relative to the data dir (specs/018 C14).

    Dry run by default: lists every absolute path and what it would become, writes nothing.
    Paths under the data dir (as configured or its realpath, so /home/<you>/... and
    /var/home/<you>/... alike) become relative; files outside it are copied into
    <data dir>/attachments/ (sha256 checked) and the row points at the copy; rows naming a
    missing file, or relative values that leave the data dir, are reported and left alone; an
    attachment row that would duplicate another of the same application is removed. --apply
    backs the database up into <data dir>/backups/ first. Host only.
    """
    from jobhunter.core import backup, dbpaths

    _host_only("db relativize-paths")
    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    data_dir = resolve_path(settings.paths.data_dir)
    conn = db.connect(db_path)
    try:
        _migrate(conn)
        plan = dbpaths.plan_relativize(conn, data_dir)
        typer.echo(f"data dir: {data_dir}")
        typer.echo(f"database: {db_path}")
        for change in plan.changes:
            typer.echo(f"  {change.describe()}")
        typer.echo(plan.summary)
        if not apply:
            if plan.actionable:
                typer.echo("dry run: nothing written. Run again with --apply to change them.")
            return
        if not plan.actionable:
            typer.echo("nothing to change.")
            raise typer.Exit(1 if plan.of(dbpaths.UNRESOLVED) else 0)
        with _job_lock(settings, "db relativize-paths", None):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            owner = db.owning_data_dir(db_path, data_dir)
            dest = owner / "backups" / f"jobhunter-{stamp}-pre-relativize-paths.db"
            try:
                backup.online_backup(conn, dest)
            except backup.BackupError as exc:
                typer.echo(f"error: {exc}; nothing changed.", err=True)
                raise typer.Exit(1) from exc
            typer.echo(f"backup: {dest}")
            plan = dbpaths.plan_relativize(conn, data_dir)  # under the lock, after the backup
            done = dbpaths.apply_relativize(conn, plan)
        for stored in done.copied:
            typer.echo(f"copied: {data_dir / stored}")
        for change in done.failed:
            typer.echo(f"  not changed: {change.where}: {change.note}", err=True)
        remaining = dbpaths.count_absolute_paths(conn, data_dir)
    finally:
        conn.close()
    typer.echo(
        f"rewrote {len(done.rewritten)} row(s), removed {len(done.removed)} duplicate(s), "
        f"copied {len(done.copied)} file(s); {remaining} path(s) still to fix."
    )
    if remaining:
        raise typer.Exit(1)


@db_app.command("backup")
def db_backup_cmd(
    auto: Annotated[
        bool,
        typer.Option(
            "--auto",
            help="Automatic backup into backups/auto/jobhunter-auto-<UTC>Z.db, then prune those "
            "to the newest of each of the last 7 days plus the newest of each of the last 4 "
            "ISO weeks. Run by the nightly backup unit.",
        ),
    ] = False,
    to: Annotated[
        Path | None,
        typer.Option(
            "--to",
            metavar="PATH",
            help="Write the backup here (a file that does not exist yet, or a directory).",
        ),
    ] = None,
) -> None:
    """Back up the database through SQLite's online backup API (safe while it is in use).

    Without --auto it writes a manual backup, backups/jobhunter-<UTC>Z.db (or --to PATH), which
    nothing ever prunes. Every backup is owner-only (0600) and passes integrity_check. Never
    migrates.
    """
    import sqlite3

    from jobhunter.core import backup

    if auto and to is not None:
        typer.echo("error: --auto and --to cannot be combined", err=True)
        raise typer.Exit(2)
    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    data_dir = db.owning_data_dir(db_path, resolve_path(settings.paths.data_dir))
    if not db_path.is_file():
        typer.echo(f"error: no database at {db_path}", err=True)
        raise typer.Exit(1)
    now = datetime.now(UTC)
    if to is not None:
        dest = to.expanduser()
        if dest.is_dir():
            dest = dest / backup.manual_name(now)
        if dest.exists() or dest.is_symlink():
            typer.echo(f"error: {dest} already exists; not overwriting it", err=True)
            raise typer.Exit(1)
    else:
        dest = backup.manual_dir(data_dir) / backup.manual_name(now)
    # A plain connection: no schema guard (backing up a database newer than this code is
    # harmless and may be exactly what is wanted) and no migration (an exempt command).
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    removed: list[Path] = []
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if auto:
            saved, removed = backup.auto_backup(conn, data_dir, now)
        else:
            saved = backup.online_backup(conn, dest)
    except (backup.BackupError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    size_mb = saved.stat().st_size / 1_000_000
    typer.echo(f"backed up {db_path} to {saved} ({size_mb:.1f} MB, integrity_check ok)")
    for path in removed:
        typer.echo(f"pruned {path}")
    leftovers = backup.leftover_temp_files(saved.parent)
    if leftovers:
        total = sum(p.stat().st_size for p in leftovers) / 1_000_000
        typer.echo(
            f"note: {len(leftovers)} temp file(s) from interrupted backups ({total:.0f} MB) in "
            f"{saved.parent}; nothing deletes them, remove them by hand: "
            + " ".join(p.name for p in leftovers),
            err=True,
        )


@db_app.command("restore")
def db_restore_cmd(
    path: Annotated[Path, typer.Argument(help="The backup file to restore.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Restore even though the console or another process seems to be using the "
            "database. They keep running against the restored data, and anything they write "
            "meanwhile may be lost or land on top of it.",
        ),
    ] = False,
) -> None:
    """Replace the live database with a backup (host only; stop the console and timers first).

    Holds the run lock, saves the current database to backups/jobhunter-<UTC>Z-pre-restore.db,
    writes the backup into the live database through SQLite's backup API (never a file copy or
    rename), then runs integrity_check and prints row counts before and after. Refuses a
    backup newer than this code; an older one is restored and migrated later as usual.
    """
    from jobhunter.core import runlock
    from jobhunter.ops import restore as rst

    _host_only("db restore")
    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    data_dir = db.owning_data_dir(db_path, resolve_path(settings.paths.data_dir))
    lock = runlock.RunLock(db_path, "db restore")
    try:
        lock.acquire(None)
    except runlock.LockHeld as exc:
        typer.echo(f"error: {exc}. Wait for it to finish; nothing was changed.", err=True)
        raise typer.Exit(1) from exc
    # Held exclusive for the whole restore: refused while any console holds it shared (also
    # one in another PID namespace, invisible to /proc), and no console starts meanwhile.
    console_lock = runlock.ConsoleLock(db_path, exclusive=True)
    try:
        busy = []
        try:
            console_lock.acquire()
        except runlock.ConsoleBusy as exc:
            busy.append(str(exc))
        busy += rst.busy_report(db_path)
        if busy:
            for line in busy:
                typer.echo(f"  {line}", err=True)
            if not force:
                typer.echo(
                    f"error: the database is in use. {rst.STOP_HINT}. Or pass --force. "
                    "Nothing was changed.",
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(
                "WARNING: --force: restoring while the database is in use. Running processes "
                "will see the restored data; anything they write meanwhile may be lost.",
                err=True,
            )
        for stale in rst.clean_stale_staging(data_dir):
            typer.echo(f"removed {stale} (left by an interrupted restore)", err=True)
        saved: list[Path] = []

        def said_safety(p: Path) -> None:
            saved.append(p)
            typer.echo(f"saved the current database to {p}")

        try:
            # Innermost lock (order: run, console, migrate; dd65f64): never write the live
            # database under another process's migration. Its holder waits on no lock, so this
            # bounded wait cannot deadlock.
            with db.migrate_lock(db_path, timeout=RESTORE_MIGRATE_WAIT_S, on_wait=_say_waiting):
                result = rst.restore(path, db_path, data_dir, on_safety=said_safety)
        except db.MigrationLockTimeout as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except rst.RestoreError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except BaseException:
            if saved:
                typer.echo(
                    f"error: the restore was interrupted after the current database was saved "
                    f"to {saved[0]}. The live database holds either the old or the restored "
                    "data (SQLite commits all or nothing); check it before restarting anything.",
                    err=True,
                )
            raise
    finally:
        console_lock.release()
        lock.release()
    typer.echo(
        f"restored {result.source} into {db_path} (schema {result.source_version}); "
        "integrity_check ok"
    )
    typer.echo("row counts (before -> after):")
    for line in rst.format_counts(result):
        typer.echo(line)
    for note in result.notes:
        typer.echo(note)


def _api_hint(packet_id: int, letter: bool, question: str | None) -> str:
    extra = " --letter" if letter else f" --question {json.dumps(question)}" if question else ""
    command = f"jobhunter apply draft {packet_id}{extra} --runner api"
    return f"To run it on the API instead (shows the cost first): {command}"


@secrets_app.command("status")
def secrets_status() -> None:
    """List each known secret as set (env), set (file PATH), set (.env PATH), invalid or unset.

    Never prints a value or a length, and always runs to the end.
    """
    report = _SECRETS_REPORT
    if report is None:  # invoked without the CLI callback
        report = secrets.startup(
            load_settings, load_env_files, lambda line: typer.echo(line, err=True)
        )
    width = max((len(s.name) for s in report.states), default=0)
    for state in report.states:
        typer.echo(f"{state.name:<{width}}  {state.describe()}")
    for note in secrets.env_file_warnings(report.env_files):
        typer.echo(note)


@app.command()
def doctor(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Every row, with what it unlocks and where.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="The checks as a JSON list (same rules, no secrets).")
    ] = False,
    group: Annotated[
        str | None,
        typer.Option("--group", help="Only one group: basics, profile, scoring, sources, ..."),
    ] = None,
    containers: Annotated[
        bool,
        typer.Option(
            "--containers",
            help="Also run `container preflight` as a dry run. Starts one container "
            "(up to 120 s) and probes podman; host only.",
        ),
    ] = False,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Also the free live checks: whether your keys are accepted, the local model "
            "and Gmail answer, and `claude` is logged in. Calls no model and spends nothing; "
            "prints what it will contact first.",
        ),
    ] = False,
    snapshot: Annotated[
        bool,
        typer.Option(
            "--snapshot",
            help="Also write what this shell (or `jobhunter-ctr`) sees to readiness.json, for "
            "the console; with --containers, preflight's results too. Never stands in for the "
            "nightly run.",
        ),
    ] = False,
) -> None:
    """Check that jobhunter is set up: the same checks as /setup, in the terminal.

    Exit 0 when every essential is ok, 1 when an essential is missing or broken (with --live,
    also when an essential's live check fails), 2 on a usage error. Recommended items never
    change the exit code. Free: no model call. Without --live, local only (no network); --live
    prints what it will contact before contacting it. Opens the database read-only and never
    migrates it (SQLite may create the -wal and -shm files beside a WAL database; they hold no
    new data). Runs even when the old layout,
    a pending schema or a broken config file stop other commands. Prints no key, address or
    contact. Writes nothing, except readiness.json with --snapshot.
    """
    from jobhunter.ops import doctor as doc

    if containers:
        _host_only("doctor --containers", "jobhunter doctor --containers")
    try:
        report = doc.gather(
            group=group,
            containers=containers,
            secrets_report=_SECRETS_REPORT,
            context_builder=build_preflight_context,
            snapshot=snapshot,
        )
    except doc.UsageError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for note in report.snapshot_notes:
        typer.echo(note, err=True)
    if live:
        planned = doc.live_plan(report)
        for line in doc.live_preamble(report, planned):
            typer.echo(line, err=as_json)
        doc.run_live(report, planned)
    if as_json:
        data = doc.to_json_data(report)
        if live:  # a separate key: plain --json stays the list of checks
            data = {"checks": data, "live": [r.as_dict() for r in report.live]}
        typer.echo(json.dumps(data, indent=2))
    else:
        for line in doc.render(report, verbose=verbose):
            typer.echo(line)
    raise typer.Exit(report.exit_code)


def build_preflight_context(unit_dir: Path | None = None, settings=None):
    """The ``preflight.Context`` for this host; shared by `container preflight` and
    `doctor --containers`. Reads files and the database read-only; writes nothing."""
    from jobhunter.config import config_file
    from jobhunter.core import dbpaths
    from jobhunter.ops import preflight
    from jobhunter.ops import version as ver
    from jobhunter.xdg import config_home

    settings = settings or load_settings()
    paths = settings.paths
    data_dir = resolve_path(paths.data_dir)
    units = (unit_dir.expanduser() if unit_dir else preflight.default_unit_dir()).absolute()
    db_path = resolve_path(paths.db_path)
    report = _SECRETS_REPORT
    abs_count, abs_error = dbpaths.absolute_path_count(db_path, data_dir)
    # The drop-in variable names must match the config file the units mount.
    mounted_path = Path.home() / preflight.CONFIG_SOURCE
    mounted, mounted_error = None, ""
    if mounted_path.is_file():
        try:
            mounted = load_settings(mounted_path)
        except Exception as exc:  # a broken file: reported as a FAIL line
            mounted_error = f"{mounted_path}: {type(exc).__name__}"
    else:
        mounted = load_settings(mounted_path)  # preflight creates it empty: code defaults
    ctx = preflight.Context(
        home=Path.home(),
        unit_dir=units,
        settings=settings,
        data_dir=data_dir,
        cache_dir=resolve_path(paths.cache_dir),
        db_path=db_path,
        profile_dir=resolve_path(paths.profile_dir),
        resume_path=resolve_path(paths.resume_path),
        config_file=config_file(None),
        gmail_token_file=config_home() / "gmail_token.json",
        env_files=list(report.env_files) if report else [],
        host_version=ver.collect(db_path, db.owning_data_dir(db_path, data_dir)),
        absolute_paths=abs_count,
        absolute_paths_error=abs_error,
        container_settings=mounted,
        container_settings_error=mounted_error,
        quadlet=preflight.QUADLET,
    )
    return ctx


@container_app.command("preflight")
def container_preflight_cmd(
    unit_dir: Annotated[
        Path | None,
        typer.Option(
            "--unit-dir",
            help="Quadlet unit directory (default ~/.config/containers/systemd). Drop-ins go "
            "in <unit>.container.d/ below it.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Write the deployment marker without asking (switches off automatic migration).",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check and list changes; write nothing.")
    ] = False,
    leave: Annotated[
        bool,
        typer.Option(
            "--leave",
            help="Go back to the host install: remove the drop-ins preflight wrote and the "
            "deployment marker (the container units must be stopped).",
        ),
    ] = False,
) -> None:
    """Check the host for the container deployment and write per-secret Quadlet drop-ins.

    Checks podman, the bind-mount sources (creating missing ones 0700), ownership, SELinux
    labels, host timers, absolute paths in the database, host versus image schema, .env
    modes and a leftover Gmail token file. Then writes one drop-in per existing podman secret
    per unit (least privilege, specs/018 matrix), removes the ones it wrote for secrets that
    are gone, and, after asking (or with --yes), writes <data dir>/deployment.toml. Reads
    secret names only, never values. Rerun after any podman secret change. Host only.
    """
    from jobhunter.ops import preflight

    _host_only("container preflight")
    settings = load_settings()
    paths = settings.paths
    data_dir = resolve_path(paths.data_dir)
    units = (unit_dir.expanduser() if unit_dir else preflight.default_unit_dir()).absolute()

    if leave:
        active = preflight.active_units()
        if active:
            typer.echo(
                f"error: stop the container units first: systemctl --user stop {' '.join(active)}",
                err=True,
            )
            raise typer.Exit(1)
        enabled = preflight.enabled_timers()
        if enabled:
            typer.echo(
                "error: container timers are still enabled and would start again at the next "
                f"login: systemctl --user disable --now {' '.join(enabled)}",
                err=True,
            )
            raise typer.Exit(1)
        if (active is None or enabled is None) and not yes:
            typer.echo(
                "error: systemctl cannot tell whether the container units are stopped. Stop "
                "them (systemctl --user stop jobhunter-console 'jobhunter-ctr-*'), then rerun "
                "with --yes.",
                err=True,
            )
            raise typer.Exit(1)
        for line in preflight.leave(units, data_dir, dry_run=dry_run):
            typer.echo(line)
        typer.echo(
            f"next: rm {units}/jobhunter-*.container (the console unit starts at login "
            "otherwise), then systemctl --user daemon-reload && jobhunter schedule install"
        )
        return

    ctx = build_preflight_context(units, settings)

    def confirm(marker: Path) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            return False
        return typer.confirm(
            f"Write {marker}? From then on no jobhunter command (host or container) migrates "
            "the database on its own: run `jobhunter upgrade` after each update, or "
            "`jobhunter container preflight --leave` to go back",
            default=False,
        )

    result = preflight.run(ctx, dry_run=dry_run, confirm_marker=confirm)
    for line in result.lines:
        typer.echo(line.render())
    for line in preflight.next_steps(result):
        typer.echo(line)
    if result.failed:
        raise typer.Exit(1)


# ─── jobhunter models (specs/020 M2) ────────────────────────────────────────

models_app = typer.Typer(
    help="Choose models: show, set and reset what models.toml holds (specs/020).",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")

QUEUED_HELP = (
    "Re-scores queued with no plan follow the screening model: 'new' runs them with the new "
    "model, 'keep' keeps them on the current one. Required when some are queued and stdin is "
    "not a terminal."
)


def _models_value_text(value: object) -> str:
    from jobhunter.config import shown_value

    if isinstance(value, bool):
        return "true" if value else "false"
    return shown_value(value) or "<hidden>"


def _models_problem_lines(layers) -> list[str]:
    """File-level problems and unknown keys (an invalid key shows on its own row)."""
    return [p.message for p in layers.problems if p.kind != "invalid"]


@models_app.command("show")
def models_show(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Each use: the value in effect, where it came from, any problem, and whether its
    credential is set (local checks only: nothing is contacted)."""
    from jobhunter import model_choices as mc
    from jobhunter.config import load_layers
    from jobhunter.scoring.scorers import credential_for

    layers = load_layers()
    merged = layers.merged
    blocked = merged.scoring.scorer_problems()
    rows = []
    for name, (section, key) in mc.NAMES.items():
        value = getattr(getattr(merged, section), key)
        problem = None
        if section == "scoring" and key in blocked:
            problem = blocked[key]
        elif key in layers.invalid.get(section, {}):
            problem = next(
                (p.message for p in layers.problems if p.key == f"{section}.{key}"), None
            )
        credential = None
        if section == "scoring" and key not in blocked:
            cred = credential_for(value, merged.scoring)
            credential = cred.status
            if cred.names and cred.status != "present":
                credential += f" (set {', '.join(cred.names)})"
        rows.append(
            {
                "use": name,
                "field": f"{section}.{key}",
                "value": None if (section == "scoring" and key in blocked) else value,
                "source": layers.source(section, key),
                "problem": problem,
                "credential": credential,
            }
        )
    info = {
        "file": str(layers.path),
        "exists": layers.exists,
        "digest": layers.digest,
        "broken": layers.broken,
        "warnings": _models_problem_lines(layers),
        "notices": list(layers.notices),
        "uses": rows,
    }
    if as_json:
        for row in rows:
            if row["value"] is not None and not isinstance(row["value"], bool):
                row["value"] = _models_value_text(row["value"])
        typer.echo(json.dumps(info, indent=2))
        return
    state = "missing (nothing set; config.toml and defaults apply)"
    if layers.exists:
        state = "BROKEN" if layers.broken else "present"
    typer.echo(f"models.toml: {layers.path} [{state}]")
    for line in info["warnings"]:
        typer.echo(f"  warning: {line}")
    for line in layers.notices:
        typer.echo(f"  notice: {line}")
    for row in rows:
        value = "BLOCKED" if row["value"] is None else _models_value_text(row["value"])
        line = f"{row['use']:<17} {value:<40} [{row['source']}]"
        if row["credential"]:
            line += f"  credential: {row['credential']}"
        typer.echo(line)
        if row["problem"]:
            typer.echo(f"{'':<17} problem: {row['problem']}")


def _models_db(settings):
    """The database for history and queued re-scores, or None when there is none yet."""
    path = resolve_path(settings.paths.db_path)
    if not path.exists():
        return None
    conn = db.connect(path)
    _migrate(conn)
    return conn


def _models_queued(conn, settings, old_spec: str | None, new_spec: str, queued: str | None):
    """Print the queued re-score line and settle new|keep. Returns "new", "keep" or None
    (nothing queued). Exits 2 when an answer is needed and cannot be asked."""
    from jobhunter import model_choices as mc
    from jobhunter.scoring.profile import ProfileError, load_profile_for

    if conn is None or not mc.unplanned_requests(conn):
        return None
    impact = None
    try:
        profile = load_profile_for(settings)
    except ProfileError:
        profile = None
    if profile is not None:
        impact = mc.queued_impact(
            conn, profile, settings.scoring, old_spec or "(blocked)", new_spec, datetime.now(UTC)
        )
    if impact is not None:
        typer.echo(impact.line)
    else:
        n = len(mc.unplanned_requests(conn))
        typer.echo(f"{n} queued re-score(s) follow the screening model (could not price them)")
    if queued is None:
        if not sys.stdin.isatty():
            typer.echo("error: re-scores are queued; pass --queued new or --queued keep", err=True)
            raise typer.Exit(2)
        keep = old_spec is not None and typer.confirm(
            f"Keep the queued re-scores on {old_spec}?", default=True
        )
        queued = "keep" if keep else "new"
    if queued == "keep" and old_spec is None:
        typer.echo(
            "error: the current screening model is blocked, so there is none to keep them on; "
            "use --queued new",
            err=True,
        )
        raise typer.Exit(2)
    return queued


def _models_write(layers, changes: dict, *, source: str, queued: str | None) -> None:
    from jobhunter import model_choices as mc

    settings = layers.merged
    conn = _models_db(settings)
    try:
        choice = None
        old_spec = None
        if "screen" in changes:
            blocked = "screen_scorer" in settings.scoring.scorer_problems()
            old_spec = None if blocked else settings.scoring.screen_scorer
            new_spec = changes["screen"]
            if new_spec is None:  # reset: what config.toml or the default gives
                new_spec = layers.base.scoring.screen_scorer
            if new_spec != old_spec:
                choice = _models_queued(conn, settings, old_spec, new_spec, queued)
        # "keep" goes first: once the file names the new model, a drain starting now must
        # already find those rows pinned to the old one. If the write then fails, the rows
        # carry the model still in effect, so nothing changes for them.
        if choice == "keep" and conn is not None and old_spec is not None:
            n = mc.keep_queued(conn, old_spec)
            typer.echo(f"kept {n} queued re-score(s) on {old_spec}")
        try:
            result = mc.write_changes(
                layers.path,
                changes,
                layers.digest,
                conn=conn,
                source=source,
                now=datetime.now(UTC),
            )
        except mc.ModelsFileError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()
    if not result.changed:
        typer.echo("no change")
        return
    for name, (_old, new) in result.changed.items():
        section, key = mc.NAMES[name]
        if new is None:
            typer.echo(f"reset {section}.{key}: config.toml's value or the default applies")
        else:
            typer.echo(f"saved {section}.{key} = {_models_value_text(new)} in {layers.path}")
    if "screen" in result.changed:
        typer.echo(
            "New jobs from the next run use it; scored jobs keep their scores. To re-screen "
            "them, run a confirmed re-score (jobhunter score --rescore recent)."
        )
    if "deep" in result.changed:
        typer.echo(
            "New deep runs use it; groups already deep-scored keep their reports "
            "(re-run one with jobhunter score --deep-group GID)."
        )
    if any(mc.USE_OF[n] in ("drafting", "check") for n in result.changed):
        typer.echo("The next draft uses it.")


@models_app.command("set")
def models_set(
    use: Annotated[
        str,
        typer.Argument(
            help="screen, deep, draft_cli, draft_api, check_cli, "
            "check_api, runner, effort or entailment_check."
        ),
    ],
    value: Annotated[str, typer.Argument(help="The scorer spec, model id or value.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Save anyway when the catalog would not offer it (dearer than the screening "
            "reservation, not in the cached OpenRouter list). Never overrides a value that "
            "cannot work.",
        ),
    ] = False,
    queued: Annotated[str | None, typer.Option("--queued", help=QUEUED_HELP)] = None,
) -> None:
    """Validate and write one choice to models.toml, as /models does. Spends nothing."""
    from jobhunter import model_choices as mc
    from jobhunter.config import load_layers

    if queued not in (None, "new", "keep"):
        typer.echo("error: --queued is new or keep", err=True)
        raise typer.Exit(2)
    try:
        mc.field_of(use)
        parsed = mc.parse_value(use, value)
    except mc.ModelsFileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    layers = load_layers()
    check = mc.check_save(use, parsed, layers.merged.scoring, settings=layers.merged)
    if check.error:
        typer.echo(f"refused: {check.error}", err=True)
        raise typer.Exit(2)
    if check.needs_force:
        for reason in check.needs_force:
            typer.echo(f"{'saving anyway' if force else 'refused'}: {reason}", err=True)
        if not force:
            typer.echo("Pass --force to save it anyway.", err=True)
            raise typer.Exit(2)
    _models_write(layers, {use: parsed}, source="cli", queued=queued)


@models_app.command("reset")
def models_reset(
    use: Annotated[
        str,
        typer.Argument(help="A use as for `models set`, or drafting, check or all."),
    ],
    queued: Annotated[str | None, typer.Option("--queued", help=QUEUED_HELP)] = None,
) -> None:
    """Remove a choice from models.toml: config.toml's value or the default applies."""
    from jobhunter import model_choices as mc
    from jobhunter.config import load_layers

    if queued not in (None, "new", "keep"):
        typer.echo("error: --queued is new or keep", err=True)
        raise typer.Exit(2)
    try:
        names = mc.expand(use)
    except mc.ModelsFileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    _models_write(load_layers(), dict.fromkeys(names), source="reset", queued=queued)


@models_app.command("reset-file")
def models_reset_file(
    force: Annotated[
        bool, typer.Option("--force", help="Empty the file even though it is valid TOML.")
    ] = False,
) -> None:
    """Replace a broken models.toml with an empty one; the old file is kept beside it."""
    from jobhunter import model_choices as mc
    from jobhunter.config import load_layers

    layers = load_layers()
    if not layers.exists:
        typer.echo(f"no {layers.path}; nothing to reset")
        return
    try:
        _digest, kept = mc.reset_file(
            layers.path, layers.digest, now=datetime.now(UTC), force=force
        )
    except mc.ModelsFileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"emptied {layers.path}" + (f"; the old file is {kept}" if kept else ""))


@models_app.command("catalog")
def models_catalog(
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="First fetch OpenRouter's public model list (contacts openrouter.ai; free, "
            "no key sent).",
        ),
    ] = False,
) -> None:
    """The options per use, with list prices. Local and free unless --refresh."""
    from jobhunter.scoring import catalog
    from jobhunter.scoring.scorers import compute_cost

    settings = load_settings()
    scoring = settings.scoring
    cache = None
    if refresh:
        typer.echo("fetching the public model list from openrouter.ai (free, no key sent)...")
        cache = catalog.refresh_openrouter_cache(scoring.openrouter)
        if cache is None:
            typer.echo("refresh failed; the old list is kept", err=True)
    if cache is None:
        cache = catalog.load_openrouter_cache(scoring.openrouter.catalog_cache)
    typer.echo(f"Anthropic prices checked {catalog.PRICES_CHECKED}")
    if cache is not None and cache.fetched_at:
        fetched = datetime.fromtimestamp(cache.fetched_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        typer.echo(f"OpenRouter list cached {fetched}")
    for use in catalog.USES:
        opts = catalog.options(use, scoring, cache=cache)
        typer.echo(f"\n{catalog.USE_LABELS[use]} ({use})")
        for opt in opts.options:
            price = ""
            if use == "screen":
                p = catalog.screen_list_price(opt.value, scoring, cache=cache)
                if p.usd is not None:
                    price = f"about ${p.usd * 1000:.2f} per 1,000 jobs ({p.source})"
            elif use == "deep" and opt.value.startswith("anthropic:"):
                usd = compute_cost(catalog.DEEP_TYPICAL_USAGE, opt.value, batch=False)
                price = f"about ${usd:.3f} a job (price table)"
            elif use in ("check_api", "check_cli"):
                est = catalog.check_estimate(opt.value)
                if est.usd is not None:
                    price = f"about ${est.usd:.4f} a check ({est.source})"
            tags = " [older]" if opt.older else ""
            tags += f" [{opt.note}]" if opt.note else ""
            typer.echo(f"  {opt.value:<45} {price}{tags}".rstrip())
        for free in opts.free_text:
            typer.echo(f"  or type {free.example}")
