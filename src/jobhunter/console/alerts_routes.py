"""Routes for /alerts: the email-alert subscription checklist (specs/012 "Subscribing")."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.console import alerts as a
from jobhunter.console.inbox_routes import load_console_profile
from jobhunter.scoring.profile import Profile
from jobhunter.sources.registry import load_registry


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def checklist(request: Request, conn: sqlite3.Connection) -> a.Checklist:
        # Tests may inject app.state.registry_loader to avoid depending on the packaged registry.
        loader = getattr(app.state, "registry_loader", load_registry)
        profile, _ = load_console_profile(request.app.state.settings)
        return a.build_checklist(
            conn,
            loader(),
            request.app.state.settings.mail,
            profile or Profile(),
            now(),
        )

    def body_context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
        _, error = load_console_profile(request.app.state.settings)
        return {
            "data": checklist(request, conn),
            "profile_error": error,
            "alerts_placeholder": a.PLACEHOLDER_ADDRESS,
            "setup_doc": a.SETUP_DOC,
        }

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, conn: Conn) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "alerts.html",
            {"title": "Alerts", "active": "/alerts", "nav": nav, **body_context(request, conn)},
        )

    def act(
        request: Request, conn: sqlite3.Connection, key: str, change: Callable[[], None]
    ) -> Response:
        loader = getattr(app.state, "registry_loader", load_registry)
        if key not in {r.key for r in a.checklist_rows(loader())}:
            raise HTTPException(404, f"no alert checklist row {key!r}")
        change()
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(
                request, "_alerts_body.html", {"nav": nav, **body_context(request, conn)}
            )
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/{key}/subscribe")
    def subscribe(request: Request, conn: Conn, key: str) -> Response:
        return act(request, conn, key, lambda: a.mark_subscribed(conn, key, now()))

    @app.post("/alerts/{key}/unsubscribe")
    def unsubscribe(request: Request, conn: Conn, key: str) -> Response:
        return act(request, conn, key, lambda: a.mark_unsubscribed(conn, key, now()))

    @app.post("/alerts/{key}/reject-plus")
    def reject_plus(request: Request, conn: Conn, key: str) -> Response:
        # The domain is the one shown on the row (newest alert sender, else the entry host).
        row = next((r for r in checklist(request, conn).rows if r.key == key), None)
        if row is None:
            raise HTTPException(404, f"no alert checklist row {key!r}")
        return act(request, conn, key, lambda: a.reject_plus(conn, key, row.domain, now()))
