"""Routes for /inbox (specs/007): the page plus HTMX label/undo fragments."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.config import Settings
from jobhunter.console import inbox
from jobhunter.scoring.profile import Profile, ProfileError, load_profile_for


def load_console_profile(settings: Settings) -> tuple[Profile | None, str | None]:
    """Load the profile per request; (None, message) when missing or invalid."""
    try:
        return load_profile_for(settings), None
    except ProfileError as exc:
        return None, str(exc)


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(
        request: Request,
        conn: Conn,
        state: str | None = None,
        bucket: str | None = None,
    ) -> HTMLResponse:
        profile, error = load_console_profile(request.app.state.settings)
        state = (state or "").strip().upper() or None
        bucket = (bucket or "").strip().upper() or None
        data = None
        if profile is not None:
            data = inbox.inbox_items(conn, profile, state=state, bucket=bucket)
        return templates.TemplateResponse(
            request,
            "inbox.html",
            {
                "title": "Inbox",
                "active": "/inbox",
                "nav": nav,
                "data": data,
                "profile_error": error,
                "state": state,
                "bucket": bucket,
                "titles": inbox.BUCKET_TITLES,
                "all_buckets": inbox.BUCKETS,
            },
        )

    @app.post("/inbox/{group_id}/label", response_class=HTMLResponse)
    def label(request: Request, conn: Conn, group_id: int, label: str) -> HTMLResponse:
        if label not in ("interesting", "not_interesting"):
            raise HTTPException(422, "label must be interesting or not_interesting")
        if not inbox.group_exists(conn, group_id):
            raise HTTPException(404, "no such job group")
        title = inbox.group_title(conn, group_id)
        inbox.set_label(conn, group_id, label)
        return templates.TemplateResponse(
            request,
            "_inbox_triaged.html",
            {"group_id": group_id, "label": label, "job_title": title},
        )

    @app.post("/inbox/{group_id}/undo", response_class=HTMLResponse)
    def undo(request: Request, conn: Conn, group_id: int) -> HTMLResponse:
        if not inbox.group_exists(conn, group_id):
            raise HTTPException(404, "no such job group")
        inbox.undo_label(conn, group_id)
        profile, _ = load_console_profile(request.app.state.settings)
        item = inbox.inbox_item(conn, profile, group_id) if profile else None
        if item is None:
            return HTMLResponse(f'<div class="row triaged" id="row-{group_id}">Undone.</div>')
        return templates.TemplateResponse(request, "_inbox_row.html", {"i": item})
