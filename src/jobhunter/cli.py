"""Typer entrypoint (specs/002-architecture.md#process-model)."""

from __future__ import annotations

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
def score() -> None:
    """Submit or collect LLM scoring batches."""
    _stub()


@app.command()
def console() -> None:
    """Start the local web console."""
    _stub()


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
