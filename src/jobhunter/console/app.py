"""FastAPI console app factory (specs/007-console-and-tracking.md)."""

from __future__ import annotations

import ipaddress
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunter.config import Settings
from jobhunter.core import db

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

ConnFactory = Callable[[], sqlite3.Connection]

# (path, nav label or None when not in the nav, page heading)
PAGES: list[tuple[str, str | None, str]] = [
    ("/", "Today", "Today"),
    ("/inbox", "Inbox", "Inbox"),
    ("/pipeline", "Pipeline", "Pipeline"),
    ("/followups", "Follow-ups", "Follow-ups"),
    ("/sources", "Sources", "Sources"),
    ("/rejected", None, "Rejected"),
    ("/search", "Search", "Search"),
    ("/costs", "Costs", "Costs"),
    ("/prefs", "Prefs", "Preferences"),
    ("/alerts", None, "Alerts"),
]
NAV = [(path, label) for path, label, _ in PAGES if label]


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_host(host: str, allow_remote: bool = False) -> None:
    """Refuse non-loopback bind hosts unless explicitly allowed (the console has no auth)."""
    if not allow_remote and not is_loopback(host):
        raise ValueError(
            f"refusing to bind the console (no auth) to non-loopback host {host!r}; "
            "pass --allow-remote to override"
        )


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request DB connection, closed after the response."""
    conn = request.app.state.conn_factory()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def create_app(settings: Settings, conn_factory: ConnFactory | None = None) -> FastAPI:
    """Build the console. ``conn_factory`` defaults to opening ``settings.paths.db_path``."""
    factory: ConnFactory = conn_factory or (lambda: db.connect(settings.paths.db_path))

    boot = factory()
    try:
        db.migrate(boot)
    finally:
        boot.close()

    app = FastAPI(title="jobhunter console", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.conn_factory = factory

    def render(request: Request, title: str, active: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            {"title": title, "active": active, "nav": NAV},
        )

    @app.get("/healthz")
    def healthz(conn: Conn) -> dict[str, object]:
        return {"ok": True, "schema_version": db.current_version(conn)}

    def add_page(path: str, title: str) -> None:
        def handler(request: Request) -> HTMLResponse:
            return render(request, title, path)

        app.add_api_route(path, handler, methods=["GET"], response_class=HTMLResponse)

    for path, _, title in PAGES:
        add_page(path, title)

    @app.get("/job/{group_id}", response_class=HTMLResponse)
    def job(request: Request, group_id: str) -> HTMLResponse:
        return render(request, f"Job {group_id}", "/inbox")

    return app
