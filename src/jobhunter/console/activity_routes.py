"""Persistent "work in progress" indicator for the shared nav (bug edb9ba6).

``GET /activity`` returns a small fragment that base.html loads with htmx on every page. It
shows a running or queued re-score (progress, link to /prefs#sec-rescore) and a pipeline run
holding the run lock, then a short done/failed line for a re-score that just ended. It is
cheap (two indexed queries and one tiny file read) and never takes the run lock:
``runlock.probe`` only looks.

It may write, in two narrow cases: ``rescore.running_request`` marks a stale heartbeat failed
(once), and the achievements gate (specs/021 "Unlock notice") runs ``evaluate``, which writes
new unlocks. The gate keeps a fingerprint in process and evaluates only when it changed; an
unchanged poll costs one ``SELECT`` and takes no lock. When it does write, it waits at most
250 ms for the lock (``BEGIN IMMEDIATE``) and, on any ``sqlite3.Error`` (a pipeline run or
re-score holding the write lock), renders from the stored unlocks; the next poll retries. A
poll never waits for another tab's evaluation (``wait=False``).

The achievement notice shows the newest unseen unlock in a visible category while notices are
on, with "+N more" for the rest. Its dismiss button posts exactly the ids it shows to
``POST /achievements/seen``.
"""

import contextlib
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.achievements import catalog, engine
from jobhunter.config import resolve_path
from jobhunter.core import runlock
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore as rs

RECENT_DONE = timedelta(minutes=10)  # a finished re-score is announced for this long
FAST_S = 3
SLOW_S = 60
_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9 _.:/-]{1,40}$")

logger = logging.getLogger(__name__)


def achievement_notice(
    conn: sqlite3.Connection, gate: engine.Gate, moment: datetime
) -> dict[str, Any] | None:
    """Evaluate when something changed, then describe the unseen unlocks to show (or None).

    Never raises ``sqlite3.Error``: a busy database means the stored unlocks are shown, or
    nothing if even the read fails."""
    try:
        settings = engine.read_settings(conn)
    except sqlite3.Error:
        return None
    gate.enabled = settings.enabled  # keep the nav's cached flag honest (CLI changes)
    if not settings.enabled:
        return None
    try:
        gate.refresh(conn, moment, wait=False)
    except sqlite3.Error as exc:  # render what is stored; the next poll retries
        logger.info("activity: achievements evaluate skipped: %s", exc)
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    if not settings.notify:
        return None
    try:
        rows = engine.unseen(conn, settings)
    except sqlite3.Error:
        return None
    if not rows:
        return None
    top = rows[0]
    a = catalog.BY_ID[top.achievement_id]
    return {
        "id": top.id,
        "ids": ",".join(str(u.id) for u in rows),
        "name": a.name,
        "tier": top.tier if a.tiers > 1 else None,
        "more": len(rows) - 1,
    }


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
        notice = achievement_notice(conn, request.app.state.achievements, moment)
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
                "ach": notice,
                "short": short,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response
