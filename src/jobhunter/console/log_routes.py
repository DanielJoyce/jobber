"""``GET /log``: run history, and the Today card's data (specs/019 Addendum C, bug be1d232).

Read-only. ``/activity`` is the nav band fragment, so the page is ``/log``. The run lock is
only looked at (``runlock.probe``), never taken.
"""

import contextlib
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.config import resolve_path
from jobhunter.console import activity_log as al
from jobhunter.console.rescore_routes import local_time
from jobhunter.core import runlock
from jobhunter.scoring.profile import Profile, ProfileError


def lock_held(request: Request) -> bool:
    held = False
    with contextlib.suppress(OSError):
        held, _ = runlock.probe(resolve_path(request.app.state.settings.paths.db_path))
    return held


def today_card(
    request: Request,
    conn: sqlite3.Connection,
    moment: datetime,
    get_profile: Callable[[], Profile],
) -> dict[str, Any]:
    """Context for the Today dashboard's last-24-hours card. Never raises."""
    try:
        fv: str | None = get_profile().filter_version
    except (ProfileError, OSError, ValueError):
        fv = None
    held = lock_held(request)
    card = al.since_yesterday(conn, moment, fv)
    card["last_run"] = al.last_run(conn, held)
    return card


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    templates.env.filters["localtime"] = local_time

    @app.get("/log", response_class=HTMLResponse)
    def log_page(request: Request, conn: Conn, kind: str = "all") -> HTMLResponse:
        kind = kind if kind in al.KINDS else "all"
        held = lock_held(request)
        response = templates.TemplateResponse(
            request,
            "log.html",
            {
                "title": "Log",
                "active": "/log",
                "nav": nav,
                "kind": kind,
                "kinds": al.KINDS,
                "events": al.events(conn, kind, lock_held=held),
                "limit": al.TOTAL,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response
