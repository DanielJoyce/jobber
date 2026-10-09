"""Typer entrypoint (specs/002-architecture.md#process-model)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

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

NOT_IMPLEMENTED = "not implemented yet"


@app.callback()
def _load_env() -> None:
    """Load .env secrets (e.g. USAJOBS_API_KEY) before any command runs."""
    load_env_files()


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
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
) -> None:
    """Screen batches (specs/006 Stage 2) or the Opus deep pass (Stage 3)."""
    modes = [submit, collect is not None, collect_pending, deep is not None, deep_group is not None]
    if sum(modes) != 1:
        typer.echo(
            "pass exactly one of --submit, --collect, --collect-pending, --deep or --deep-group",
            err=True,
        )
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
    if scorer_override is not None and not (submit or collect is not None):
        typer.echo("--scorer applies to --submit and --collect only", err=True)
        raise typer.Exit(2)
    scorer = scorer_override or settings.scoring.screen_scorer
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
                        remaining_usd=lambda: screen.remaining_daily_budget(
                            conn, settings.scoring.daily_cap_usd, now
                        ),
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
                sync = screen.score_sync(
                    conn,
                    scorer_from_string(scorer, scoring=settings.scoring),
                    profile,
                    limit=limit,
                    now=now,
                    remaining_usd=lambda: screen.remaining_daily_budget(
                        conn, settings.scoring.daily_cap_usd, now
                    ),
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
                remaining_usd=lambda: screen.remaining_daily_budget(
                    conn, settings.scoring.daily_cap_usd, now
                ),
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
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
) -> None:
    """Run dedupe passes by hand."""
    if not apply_url:
        typer.echo("nothing to do: pass --apply-url", err=True)
        raise typer.Exit(2)
    from jobhunter.pipeline.dedupe_url import merge_by_apply_url

    settings = load_settings()
    db_path = resolve_path(settings.paths.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        res = merge_by_apply_url(conn, now=datetime.now(UTC))
    finally:
        conn.close()
    typer.echo(", ".join(f"{k}={v}" for k, v in vars(res).items()))


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
        check_host(bind_host, allow_remote)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    uvicorn.run(create_app(settings), host=bind_host, port=bind_port)


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
def mail_auth() -> None:
    """Run the Gmail OAuth flow and store the refresh token in the OS keyring."""
    from jobhunter.config import load_settings
    from jobhunter.mail import auth

    settings = load_settings()
    try:
        auth.authenticate(settings)
    except auth.MailAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Gmail authorised; token stored in the OS keyring (service jobhunter, key gmail).")


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
    typer.echo(f"token in keyring: {'yes' if has_token else 'no'}")
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
    """Propose application events/records from recent mail (never applied automatically)."""
    from jobhunter.config import load_settings, resolve_path
    from jobhunter.core import db
    from jobhunter.mail import auth, match

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
    except auth.MailAuthError as exc:
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
    verb = "would store" if dry_run else "stored"
    n = len(result.proposals) if dry_run else result.stored
    typer.echo(
        f"scanned {result.scanned} new messages ({result.already_seen} seen before); "
        f"{verb} {n} proposals"
    )


@mail_app.command("sync")
def mail_sync(
    limit: Annotated[int, typer.Option(help="Most alert messages to read this run.")] = 200,
) -> None:
    """Ingest job-alert emails: list, resolve (robots permitting), normalize, dedupe."""
    from jobhunter.config import load_settings
    from jobhunter.mail import auth, sync
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
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
    except (auth.MailAuthError, sync.MailSyncError) as exc:
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

    try:
        typer.echo(schedule.status())
    except schedule.ScheduleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Disable the timers and remove the unit files."""
    from jobhunter.ops import schedule

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
            help="local:<model>, openrouter:<slug> or anthropic:<model>. "
            "Default: scoring.screen_scorer.",
        ),
    ] = None,
    n: Annotated[int, typer.Option("--n", min=1, help="How many job groups to score.")] = 10,
    night_hours: Annotated[
        float, typer.Option(help="Hours available overnight, for the jobs-per-night projection.")
    ] = 8.0,
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
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    try:
        report = run_bench(conn, scorer, profile, n=n, night_hours=night_hours)
    finally:
        conn.close()
    if report.attempted == 0:
        typer.echo("no prefiltered job groups to score; run jobhunter run first", err=True)
        raise typer.Exit(1)
    typer.echo(report.format())


if __name__ == "__main__":
    app()
