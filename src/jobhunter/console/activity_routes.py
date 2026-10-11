"""Persistent "work in progress" indicator for the shared nav (bug edb9ba6).

``GET /activity`` returns a small fragment that base.html loads with htmx on every page. It
shows a running or queued re-score (progress, link to /prefs#sec-rescore) and a pipeline run
holding the run lock, then a short done/failed line for a re-score that just ended. It is
cheap (two indexed queries and one tiny file read) and read-only apart from what
``rescore.running_request`` already does for a stale heartbeat (marks it failed, once). It
never takes the run lock: ``runlock.probe`` only looks.
"""

import contextlib
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.config import resolve_path
from jobhunter.core import runlock
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore as rs

RECENT_DONE = timedelta(minutes=10)  # a finished re-score is announced for this long
FAST_S = 3
SLOW_S = 60
_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9 _.:/-]{1,40}$")


def short(text: str | None, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request, conn: Conn, dismissed: str = "") -> HTMLResponse:
        moment = now()
        running = pending = finished = None
        try:
            running = rs.running_request(conn, moment)  # stale heartbeats handled as there
            pending = conn.execute(
                "SELECT id FROM rescore_request WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if running is None:
                finished = conn.execute(
                    "SELECT * FROM rescore_request WHERE status IN ('done', 'failed') "
                    "AND done_at >= ? ORDER BY done_at DESC, id DESC LIMIT 1",
                    (to_iso(moment - RECENT_DONE),),
                ).fetchone()
        except sqlite3.Error:
            pass  # a busy database must not break every page; show nothing this time
        if finished is not None and str(finished["id"]) == dismissed:
            finished = None
        held, holder = False, None
        with contextlib.suppress(OSError):
            held, holder = runlock.probe(resolve_path(request.app.state.settings.paths.db_path))
        # Our own re-score holds the run lock too; do not say it twice.
        run_label = None
        if held and running is None:
            command = holder.command if holder else None
            run_label = command if command and _SAFE_COMMAND.match(command) else None
        active = running is not None or (held and running is None)
        response = templates.TemplateResponse(
            request,
            "_activity.html",
            {
                "running": running,
                "pending": pending,
                "finished": finished,
                "run_held": held and running is None,
                "run_label": run_label,
                "every": FAST_S if active else SLOW_S,
                "short": short,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response
