"""Typer entrypoint (specs/002-architecture.md#process-model)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from jobhunter import container, secrets
from jobhunter.config import load_env_files, load_settings, resolve_path
from jobhunter.core import db
from jobhunter.pipeline import runner
from jobhunter.sources.registry import enabled_sources, load_registry, sync_sources_table

app = typer.Typer(
    help="jobhunter: sweep job banks, score fit, track applications.", no_args_is_help=True
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

NOT_IMPLEMENTED = "not implemented yet"

# What the last CLI start found for each known secret (specs/018 C2); `secrets status` prints it.
_SECRETS_REPORT: secrets.Report | None = None


def _host_only(command: str, host_command: str | None = None) -> None:
    """Exit with the host command when running in container mode (specs/018 C1)."""
    if container.is_container():
        typer.echo(container.refusal(command, host_command), err=True)
        raise typer.Exit(2)


# Commands that run while old-layout data is unmigrated: they report on it, move it, or do
# not touch user data at all (schedule only writes systemd units).
UNGUARDED_COMMANDS = {"paths", "migrate-paths", "init", "schedule", "secrets"}


@app.callback()
def _load_env(ctx: typer.Context) -> None:
    """Load .env secrets (e.g. USAJOBS_API_KEY) before any command runs, then refuse to run
    against a new database while the real one is still in the old layout."""
    global _SECRETS_REPORT
    _SECRETS_REPORT = secrets.startup(
        load_settings, load_env_files, lambda line: typer.echo(line, err=True)
    )
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
        raise typer.Exit(1)
    for note in status.warnings:
        typer.echo(note, err=True)


def _stub() -> None:
    typer.echo(NOT_IMPLEMENTED)


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
) -> None:
    """Run the ingest pipeline."""
    try:
        stages = runner.parse_stages(stage)
        since_dt = datetime.fromisoformat(since).replace(tzinfo=UTC) if since else None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    states = runner.parse_states(state)
    settings = load_settings()
    rows = load_registry()
    loader = runner.default_profile_loader(settings)

    if dry_run:
        db_path = resolve_path(settings.paths.db_path)
        conn = db.connect(db_path if db_path.is_file() else ":memory:")
        try:
            if not db_path.is_file():
                db.migrate(conn)
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

    db_path = resolve_path(settings.paths.db_path)
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
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
        )
    finally:
        conn.close()
    summary = runner.format_summary(report)
    if summary:
        typer.echo(summary)
    raise typer.Exit(report.exit_code)


@app.command()
def score(
    submit: Annotated[
        bool, typer.Option("--submit", help="Submit a screen batch for eligible job groups.")
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

    from datetime import UTC, datetime

    import anthropic

    from jobhunter.config import load_settings
    from jobhunter.core import db
    from jobhunter.scoring import screen
    from jobhunter.scoring.profile import ProfileError, load_profile
    from jobhunter.scoring.scorers import ScorerError, privacy_notice, scorer_from_string

    settings = load_settings()
    try:
        profile = load_profile(settings.paths.profile_dir, resume_path=settings.paths.resume_path)
    except ProfileError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if group is not None:
        if scorer_override is not None or jobs_per_request is not None:
            typer.echo("--group scores with scoring.screen_scorer; no other options", err=True)
            raise typer.Exit(2)
        _score_group(settings, profile, group, yes)
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
        _score_rescore(settings, profile, rescore, scorer_override, yes)
        return
    screening = submit or collect is not None
    if screening and (notice := privacy_notice(scorer, settings.scoring)):
        typer.echo(notice, err=True)
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    # The screen scorer may not be Anthropic; only build its client where it is needed.
    client = None if screening and not scorer.startswith("anthropic:") else anthropic.Anthropic()
    now = datetime.now(UTC)
    try:
        if deep is not None or deep_group is not None:
            from jobhunter.scoring import deep as stage3

            deep_scorer = settings.scoring.deep_scorer
            gids = [deep_group] if deep_group is not None else None
            if gids is None:
                assert deep is not None
                gids = stage3.shortlist(conn, profile, deep, scorer=deep_scorer)
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
                flag = " DISAGREES with screen" if res.disagreement else ""
                typer.echo(f"group {gid}: {res.status}{flag} {res.detail}".rstrip())
                if res.status == "capped":
                    break
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


def _score_group(settings, profile, group_id: int, yes: bool) -> None:
    """``score --group GID``: estimate, confirm, then score that one group (specs/017)."""
    from jobhunter.apply import score as group_score
    from jobhunter.scoring.scorers import privacy_notice

    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    now = datetime.now(UTC)
    try:
        est = group_score.estimate(conn, profile, settings.scoring, group_id, now)
        typer.echo(
            f"group {group_id}: score with {est.scorer}, estimated ${est.estimated_usd:.4f} "
            f"({'measured' if est.cost_source != 'default' else 'assumed'}); "
            f"spend cap remaining ${max(est.remaining_usd, 0):.2f}"
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
            out = group_score.score_now(
                conn, profile, settings.scoring, group_id, token=est.token, now=now
            )
        except group_score.ScoreRefused as exc:
            typer.echo(f"not scored: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"{out.status}: {out.detail}")
    finally:
        conn.close()


def _score_rescore(settings, profile, scope: str | None, scorer_override: str | None, yes: bool):
    """``score --rescore SCOPE`` (estimate, confirm, run) or ``--rescore-pending`` (drain)."""
    from jobhunter.scoring import rescore as rs
    from jobhunter.scoring.scorers import ScorerError, privacy_notice

    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    clock = lambda: datetime.now(UTC)  # noqa: E731
    spec = scorer_override or settings.scoring.screen_scorer
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
            plan = rs.make_plan(conn, profile, settings.scoring, scope, spec, now)
        except (rs.RescoreError, ScorerError) as exc:
            typer.echo(f"re-score: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(
            f"scope {scope}: {plan.total} groups with {spec} "
            f"({plan.rescored} of {plan.waiting} waiting were scored before); "
            f"estimated ${plan.estimated_usd:.2f} at ${plan.cost_per_job:.5f}/job "
            f"({plan.cost_source}); spend cap remaining ${max(plan.remaining_usd, 0):.2f}"
        )
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
        rid = rs.create_request(conn, profile, plan, now)
        try:
            res = rs.run_request(
                conn,
                rid,
                profile,
                settings.scoring,
                scorer=spec,
                now=clock,
                prefiltered=plan.prefilter,
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
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
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
            db.migrate(conn)
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
        db.migrate(conn)
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
    from jobhunter.config import config_file, config_file_source

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
        db.migrate(conn)
    finally:
        conn.close()
    secure_data(settings)
    if existed:
        typer.echo(f"already set up: {db_path} (schema up to date, permissions checked)")
        return
    typer.echo(f"created {db_path}")
    typer.echo(
        f"next: put your resume (.md) in {resolve_path(settings.paths.resume_path)} or upload "
        "it in `jobhunter console`, then set your preferences there."
    )


@app.command()
def console(
    host: Annotated[str | None, typer.Option(help="Bind host (default: config).")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port (default: config).")] = None,
    allow_remote: Annotated[
        bool, typer.Option("--allow-remote", help="Allow a non-loopback host (no auth!).")
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
    app_ = create_app(
        settings,
        allow_remote=allow_remote,
        ext_port=settings.console.public_port or bind_port,
    )
    uvicorn.run(app_, host=bind_host, port=bind_port)


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
    from jobhunter.scoring.profile import ProfileError, load_profile

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
        profile = load_profile(settings.paths.profile_dir, resume_path=settings.paths.resume_path)
    except ProfileError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(1) from exc
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
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
) -> None:
    """Probe every registry entry and diff against recorded values (specs/003 breakage detection).

    Meant to run weekly (a timer will schedule it). Fetches each entry URL and robots.txt through
    FetchContext (robots and rate limits apply, so it is slow by design), checks family signals,
    the robots.txt hash, and the shared JobLink bundle hash. Writes the source table's robots and
    verified columns, never registry.yaml. Exits 1 when any source drifted, broke or was blocked.
    """
    from jobhunter.sources import verify as vf

    settings = load_settings()
    conn = db.connect(resolve_path(settings.paths.db_path))
    db.migrate(conn)
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
) -> None:
    """Run the Gmail OAuth flow and store the refresh token (OS keyring, else a 0600 file)."""
    from jobhunter.config import load_settings
    from jobhunter.mail import auth

    _host_only("mail auth", "jobhunter mail auth --podman-secret jobhunter_gmail_token")
    settings = load_settings()
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
            typer.echo(setup.filters_xml(settings), nl=False)
            return
        setup.validate_address(settings)
        if dry_run and (auth.load_token() is None):
            # No credentials: show the plan without looking at the mailbox.
            typer.echo(f"would ensure label {settings.mail.label}")
            for crit in setup.wanted_filters(settings):
                typer.echo(f"would ensure filter {setup.describe(crit)}")
            return
        service = auth.build_service(settings)
        report = setup.setup_mailbox(service, settings, dry_run=dry_run)
    except (setup.MailSetupError, auth.MailAuthError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for line in report.lines():
        typer.echo(line)


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
        db.migrate(conn)
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
        db.migrate(conn)
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
        db.migrate(conn)
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
    from jobhunter.scoring.profile import ProfileError, load_profile
    from jobhunter.scoring.scorers import ScorerError, privacy_notice, scorer_from_string

    settings = load_settings()
    spec = scorer_spec or settings.scoring.screen_scorer
    try:
        profile = load_profile(settings.paths.profile_dir, resume_path=settings.paths.resume_path)
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
    db.migrate(conn)
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
        str | None, typer.Option("--question", help="Draft an answer to this custom question.")
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
    if letter and question:
        typer.echo("error: pick one of --letter and --question", err=True)
        raise typer.Exit(2)
    kind = "cover_letter" if letter else "question_draft" if question else "resume"
    settings = load_settings()
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
    db.migrate(conn)
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
        est = generator.estimate(conn, settings, kind, req.chars)
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
        db.migrate(conn)
        typer.echo(stats.compute(conn, datetime.now(UTC), days).format())
    finally:
        conn.close()


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
