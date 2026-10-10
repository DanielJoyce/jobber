"""Routes for /rejections: rejections FROM EMPLOYERS (core/rejections, specs/007).

Not ``/rejected``, which lists jobs *we* filtered out. Rows come from ``jobhunter mail match``
(source 'email') or the form here (source 'manual'). A rejected posting is never scored and is
left out of the inbox; other roles at the same employer get a flag (specs/006). An email read
from a loose word only is 'pending' and does nothing until confirmed here.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.console.tracking_routes import Form
from jobhunter.core import rejections


def _day(text: str, now: datetime) -> str | None:
    """``YYYY-MM-DD`` from the form as an ISO timestamp (noon UTC); blank means now."""
    text = (text or "").strip()
    if not text:
        return now.astimezone(UTC).isoformat()
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(hour=12, tzinfo=UTC).isoformat()


def _validate(conn: sqlite3.Connection, f: dict[str, str]) -> tuple[str | None, int | None]:
    """(error message or None, job group id or None)."""
    if not (f.get("employer") or "").strip():
        return "Employer is required.", None
    raw = (f.get("group_id") or "").strip()
    if not raw:
        return None, None
    try:
        gid = int(raw)
    except ValueError:
        return "Job id must be a number.", None
    if not conn.execute("SELECT 1 FROM job_group WHERE id = ?", (gid,)).fetchone():
        return f"No job {gid}.", None
    return None, gid


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def page(
        request: Request,
        conn: sqlite3.Connection,
        error: str | None = None,
        form: dict[str, str] | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "employer_rejections.html",
            {
                "title": "Employer rejections",
                "active": "/rejections",
                "nav": nav,
                "items": rejections.listing(conn),
                "pending": rejections.pending_count(conn),
                "error": error,
                "form": form or {},
                "today": now().date().isoformat(),
            },
            status_code=422 if error else 200,
        )

    @app.get("/rejections", response_class=HTMLResponse)
    def rejections_page(request: Request, conn: Conn) -> HTMLResponse:
        return page(request, conn)

    @app.post("/rejections")
    def add(request: Request, conn: Conn, f: Form) -> Response:
        error, group_id = _validate(conn, f)
        when = _day(f.get("date", ""), now())
        if error is None and when is None:
            error = "Date must be YYYY-MM-DD."
        if error is not None or when is None:
            return page(request, conn, error, f)
        app_row = None
        if group_id is not None:
            app_row = conn.execute(
                "SELECT id FROM application WHERE job_group_id = ?", (group_id,)
            ).fetchone()
        rejections.record(
            conn,
            received_at=when,
            employer=f.get("employer"),
            title=f.get("title"),
            source="manual",
            now=now(),
            job_group_id=group_id,
            application_id=app_row["id"] if app_row else None,
        )
        return RedirectResponse("/rejections", status_code=303)

    @app.post("/rejections/{rid}/confirm")
    def confirm(conn: Conn, rid: int) -> Response:
        if not rejections.confirm(conn, rid):
            raise HTTPException(404, "no such rejection")
        return RedirectResponse("/rejections", status_code=303)

    @app.post("/rejections/{rid}/delete")
    def delete(conn: Conn, rid: int) -> Response:
        cur = conn.execute("DELETE FROM rejection WHERE id = ?", (rid,))
        if not cur.rowcount:
            raise HTTPException(404, "no such rejection")
        return RedirectResponse("/rejections", status_code=303)
