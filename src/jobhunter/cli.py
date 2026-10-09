"""Typer entrypoint (specs/002-architecture.md#process-model)."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from jobhunter.sources.registry import enabled_sources, load_registry

app = typer.Typer(
    help="jobhunter: sweep job banks, score fit, track applications.", no_args_is_help=True
)
sources_app = typer.Typer(help="Inspect and verify configured sources.", no_args_is_help=True)
mail_app = typer.Typer(help="Email alert ingest.", no_args_is_help=True)
applylinks_app = typer.Typer(help="Apply-link resolution.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(mail_app, name="mail")
app.add_typer(applylinks_app, name="applylinks")

NOT_IMPLEMENTED = "not implemented yet"


def _stub() -> None:
    typer.echo(NOT_IMPLEMENTED)


@app.command()
def run(
    stage: Annotated[str | None, typer.Option(help="Run only this pipeline stage.")] = None,
    state: Annotated[str | None, typer.Option(help="Restrict to one state/source.")] = None,
    since: Annotated[str | None, typer.Option(help="Override the watermark.")] = None,
    full: Annotated[bool, typer.Option("--full", help="Ignore watermarks.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan only.")] = False,
) -> None:
    """Run the ingest pipeline."""
    if dry_run:
        typer.echo("Run plan: no sources enabled")
        return
    _stub()


@app.command()
def score(
    submit: Annotated[
        bool, typer.Option("--submit", help="Submit a screen batch for eligible job groups.")
    ] = False,
    collect: Annotated[
        str | None, typer.Option("--collect", metavar="BATCH_ID", help="Collect a batch.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Most job groups to submit.", min=1)] = 500,
) -> None:
    """Submit or collect LLM screen batches (specs/006 Stage 2)."""
    if submit == (collect is not None):
        typer.echo("pass exactly one of --submit or --collect BATCH_ID", err=True)
        raise typer.Exit(2)

    from datetime import UTC, datetime

    import anthropic

    from jobhunter.config import load_settings
    from jobhunter.core import db
    from jobhunter.scoring import screen
    from jobhunter.scoring.profile import ProfileError, load_profile

    settings = load_settings()
    try:
        profile = load_profile(settings.paths.profile_dir, resume_path=settings.paths.resume_path)
    except ProfileError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(1) from exc
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    client = anthropic.Anthropic()
    now = datetime.now(UTC)
    scorer = settings.scoring.screen_scorer
    try:
        if submit:
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
        else:
            assert collect is not None
            result = screen.collect_batch(conn, client, collect, profile, now=now)
            typer.echo(json.dumps(result.as_dict(), indent=2))
    finally:
        conn.close()


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
def eval_() -> None:
    """Measure scorer quality against hand labels."""
    _stub()


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
def sources_verify() -> None:
    """Verify source URLs and adapters."""
    _stub()


@mail_app.command("setup")
def mail_setup() -> None:
    """Set up Gmail access."""
    _stub()


@mail_app.command("sync")
def mail_sync() -> None:
    """Ingest job-alert emails."""
    _stub()


@applylinks_app.command("unknown")
def applylinks_unknown() -> None:
    """List jobs whose apply link is unresolved."""
    _stub()


if __name__ == "__main__":
    app()
