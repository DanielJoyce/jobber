"""Routes for /pipeline and /followups (specs/007): kanban, drawer edits, follow-up actions."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.console import proposals as props
from jobhunter.console import tracking as t


async def form_body(request: Request) -> dict[str, str]:
    """Parse an urlencoded body without python-multipart (plain forms and htmx both send it)."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


Form = Annotated[dict[str, str], Depends(form_body)]


def _neighbour(status: str, step: int) -> str | None:
    if status not in t.BOARD_STATUSES:
        return None
    i = t.BOARD_STATUSES.index(status) + step
    return t.BOARD_STATUSES[i] if 0 <= i < len(t.BOARD_STATUSES) else None


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    templates.env.globals["board_statuses"] = t.BOARD_STATUSES
    templates.env.globals["status_neighbour"] = _neighbour
    templates.env.globals["lane_of"] = t.lane_of

    def page_ctx(title: str, active: str, **extra: Any) -> dict[str, Any]:
        return {"title": title, "active": active, "nav": nav, **extra}

    def require_app(conn: sqlite3.Connection, app_id: int) -> None:
        if not t.application_exists(conn, app_id):
            raise HTTPException(404, "no such application")

    def is_htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    def drawer_ctx(
        conn: sqlite3.Connection, app_id: int, error: str | None = None, oob: bool = False
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        if oob:
            board = t.pipeline(conn, now())
            counts = {**{s: len(v) for s, v in board.columns.items()}, "closed": len(board.closed)}
        return {
            "counts": counts,
            "c": t.card(conn, app_id, now()),
            "a": conn.execute("SELECT * FROM application WHERE id = ?", (app_id,)).fetchone(),
            "events": t.events(conn, app_id),
            "contacts": t.contacts(conn, app_id),
            "attachments": t.attachments(conn, app_id),
            "statuses": t.ALL_STATUSES,
            "kinds": t.ATTACHMENT_KINDS,
            "error": error,
            "oob_card": "true" if oob else None,
            "stale_days": t.STALE_DAYS,
        }

    def drawer_response(
        request: Request, conn: sqlite3.Connection, app_id: int, error: str | None = None
    ) -> Response:
        """After a drawer edit: htmx gets the fresh drawer; plain forms are redirected back."""
        if not is_htmx(request):
            return RedirectResponse(f"/pipeline/{app_id}", status_code=303)
        return templates.TemplateResponse(
            request, "_drawer.html", drawer_ctx(conn, app_id, error, oob=True)
        )

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline_page(request: Request, conn: Conn) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "pipeline.html",
            page_ctx(
                "Pipeline",
                "/pipeline",
                board=t.pipeline(conn, now()),
                detail=False,
                proposals=props.pending(conn, 3),
                proposal_count=props.pending_count(conn),
            ),
        )

    @app.get("/pipeline/{app_id}", response_class=HTMLResponse)
    def pipeline_detail(request: Request, conn: Conn, app_id: int) -> HTMLResponse:
        require_app(conn, app_id)
        ctx = drawer_ctx(conn, app_id)
        if is_htmx(request):
            return templates.TemplateResponse(request, "_drawer.html", ctx)
        return templates.TemplateResponse(
            request,
            "pipeline.html",
            page_ctx(
                "Pipeline",
                "/pipeline",
                board=t.pipeline(conn, now()),
                detail=True,
                proposals=props.pending(conn, 3),
                proposal_count=props.pending_count(conn),
                **ctx,
            ),
        )

    @app.post("/pipeline/{app_id}/move", response_class=HTMLResponse)
    def move(request: Request, conn: Conn, app_id: int, to: str) -> Response:
        """Append an event for the target status; htmx gets the card in its new column."""
        require_app(conn, app_id)
        if to not in t.ALL_STATUSES:
            raise HTTPException(422, "unknown status")
        before = t.card(conn, app_id, now())
        assert before is not None
        moved = before.status != to
        if moved:
            t.add_event(conn, app_id, to, f"moved from {before.status}", now())
        if not is_htmx(request):
            return RedirectResponse("/pipeline", status_code=303)
        if not moved:
            return HTMLResponse("")
        board = t.pipeline(conn, now())
        counts = {**{s: len(v) for s, v in board.columns.items()}, "closed": len(board.closed)}
        return templates.TemplateResponse(
            request,
            "_move_oob.html",
            {
                "c": t.card(conn, app_id, now()),
                "old_lane": t.lane_of(before.status),
                "lane": t.lane_of(to),
                "counts": counts,
            },
        )

    @app.post("/pipeline/{app_id}/event")
    def add_event(request: Request, conn: Conn, app_id: int, f: Form) -> Response:
        require_app(conn, app_id)
        error = None
        try:
            current = t.card(conn, app_id, now())
            assert current is not None
            status = f.get("status") or current.status
            t.add_event(conn, app_id, status, f.get("note"), f.get("at") or now())
        except t.TrackingError as exc:
            error = str(exc)
        return drawer_response(request, conn, app_id, error)

    @app.post("/pipeline/{app_id}/details")
    def details(request: Request, conn: Conn, app_id: int, f: Form) -> Response:
        require_app(conn, app_id)
        t.update_details(conn, app_id, f.get("resume_version"), f.get("external_ref"))
        return drawer_response(request, conn, app_id)

    @app.post("/pipeline/{app_id}/next-action")
    def next_action(request: Request, conn: Conn, app_id: int, f: Form) -> Response:
        require_app(conn, app_id)
        error = None
        try:
            t.set_next_action(conn, app_id, f.get("next_action"), f.get("next_action_at"))
        except t.TrackingError as exc:
            error = str(exc)
        return drawer_response(request, conn, app_id, error)

    @app.post("/pipeline/{app_id}/contacts")
    def contact_add(request: Request, conn: Conn, app_id: int, f: Form) -> Response:
        require_app(conn, app_id)
        error = None
        try:
            t.add_contact(conn, app_id, **{k: f.get(k) for k in t.CONTACT_FIELDS})
        except t.TrackingError as exc:
            error = str(exc)
        return drawer_response(request, conn, app_id, error)

    @app.post("/pipeline/{app_id}/contacts/{contact_id}/update")
    def contact_update(
        request: Request, conn: Conn, app_id: int, contact_id: int, f: Form
    ) -> Response:
        require_app(conn, app_id)
        error = None
        try:
            t.update_contact(conn, contact_id, **{k: f.get(k) for k in t.CONTACT_FIELDS})
        except t.TrackingError as exc:
            error = str(exc)
        return drawer_response(request, conn, app_id, error)

    @app.post("/pipeline/{app_id}/contacts/{contact_id}/delete")
    def contact_delete(request: Request, conn: Conn, app_id: int, contact_id: int) -> Response:
        require_app(conn, app_id)
        t.delete_contact(conn, contact_id)
        return drawer_response(request, conn, app_id)

    @app.post("/pipeline/{app_id}/attachments")
    def attachment_add(request: Request, conn: Conn, app_id: int, f: Form) -> Response:
        require_app(conn, app_id)
        error = None
        try:
            t.add_attachment(conn, app_id, f.get("kind") or "other", f.get("path", ""), now())
        except t.TrackingError as exc:
            error = str(exc)
        return drawer_response(request, conn, app_id, error)

    @app.post("/pipeline/{app_id}/attachments/{attachment_id}/delete")
    def attachment_delete(
        request: Request, conn: Conn, app_id: int, attachment_id: int
    ) -> Response:
        require_app(conn, app_id)
        t.delete_attachment(conn, attachment_id)
        return drawer_response(request, conn, app_id)

    # --- follow-ups ---

    @app.get("/followups", response_class=HTMLResponse)
    def followups_page(request: Request, conn: Conn) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "followups.html",
            page_ctx("Follow-ups", "/followups", items=t.followups(conn, now())),
        )

    def followup_reply(request: Request) -> Response:
        if is_htmx(request):
            return HTMLResponse("")
        return RedirectResponse("/followups", status_code=303)

    @app.post("/followups/{app_id}/done")
    def fu_done(request: Request, conn: Conn, app_id: int) -> Response:
        require_app(conn, app_id)
        t.done(conn, app_id)
        return followup_reply(request)

    @app.post("/followups/{app_id}/snooze")
    def fu_snooze(request: Request, conn: Conn, app_id: int) -> Response:
        require_app(conn, app_id)
        t.snooze(conn, app_id, now())
        return followup_reply(request)

    @app.post("/followups/{app_id}/no-response")
    def fu_no_response(request: Request, conn: Conn, app_id: int) -> Response:
        require_app(conn, app_id)
        t.add_event(conn, app_id, "no_response", "marked from follow-ups", now())
        t.done(conn, app_id)
        return followup_reply(request)
