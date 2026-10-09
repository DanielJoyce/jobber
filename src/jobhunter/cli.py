"""Typer entrypoint (specs/002-architecture.md#process-model)."""

from __future__ import annotations

from typing import Annotated

import typer

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
def sources_list() -> None:
    """List configured sources."""
    _stub()


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
