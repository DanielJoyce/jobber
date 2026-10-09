"""Routes for /proposals: review mail-derived proposals (accept / dismiss, never automatic)."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.console import proposals as p
from jobhunter.console import tracking as t
from jobhunter.console.tracking_routes import Form


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals_page(request: Request, conn: Conn) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "proposals.html",
            {"title": "Proposals", "active": "/pipeline", "nav": nav, "items": p.pending(conn)},
        )

    def reply(request: Request) -> Response:
        if request.headers.get("hx-request") == "true":
            return HTMLResponse("")
        return RedirectResponse("/proposals", status_code=303)

    @app.post("/proposals/{pid}/accept")
    def accept(request: Request, conn: Conn, pid: int, f: Form) -> Response:
        try:
            p.accept(conn, pid, now(), f.get("employer"), f.get("title"))
        except t.TrackingError as exc:
            raise HTTPException(409, str(exc)) from exc
        return reply(request)

    @app.post("/proposals/{pid}/dismiss")
    def dismiss(request: Request, conn: Conn, pid: int) -> Response:
        try:
            p.dismiss(conn, pid, now())
        except t.TrackingError as exc:
            raise HTTPException(409, str(exc)) from exc
        return reply(request)
