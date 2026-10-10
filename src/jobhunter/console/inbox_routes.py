"""Routes for /inbox (specs/007): the page plus HTMX label/undo fragments."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.config import Settings
from jobhunter.console import inbox
from jobhunter.core.bucketnames import parse_letters
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
        triaged: str | None = None,
    ) -> HTMLResponse:
        profile, error = load_console_profile(request.app.state.settings)
        state = (state or "").strip().upper() or None
        picked = parse_letters(bucket)  # ?bucket=B or ?bucket=A,B
        bucket = ",".join(picked) or None
        data = None
        if profile is not None:
            days = request.app.state.settings.scoring.employer_rejection_days
            data = inbox.inbox_items(
                conn,
                profile,
                state=state,
                bucket=bucket,
                triaged=triaged in ("1", "true"),
                rejection_days=days,
            )
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
                "picked": picked,
                "titles": inbox.BUCKET_TITLES,
                "all_buckets": inbox.BUCKETS,
            },
        )

    def bulk_ids(conn: sqlite3.Connection, group_id: list[int]) -> list[int]:
        ids = list(dict.fromkeys(group_id))  # de-duplicate, keep order
        if not ids:
            raise HTTPException(422, "select at least one job")
        if len(ids) > inbox.BULK_MAX:
            raise HTTPException(422, f"at most {inbox.BULK_MAX} jobs per bulk action")
        missing = set(ids) - inbox.existing_group_ids(conn, ids)
        if missing:
            raise HTTPException(404, f"no such job group: {sorted(missing)[0]}")
        return ids

    def refuse_triaged(conn: sqlite3.Connection, ids: list[int]) -> None:
        # Already shortlisted or dismissed: relabelling then undoing would delete
        # the label and its application (the triaged=1 lists show such rows read-only).
        done = inbox.triaged_group_ids(conn, ids)
        if done:
            raise HTTPException(409, f"already triaged: {sorted(done)[0]}")

    # Declared before the /inbox/{group_id}/... routes so "bulk" is never parsed as an id.
    @app.post("/inbox/bulk", response_class=HTMLResponse)
    def bulk_label(
        request: Request,
        conn: Conn,
        label: Annotated[str, Form()],
        group_id: Annotated[list[int] | None, Form()] = None,
    ) -> HTMLResponse:
        if label not in inbox.LABELS:
            raise HTTPException(422, "label must be interesting or not_interesting")
        ids = bulk_ids(conn, group_id or [])
        refuse_triaged(conn, ids)
        triaged = [{"group_id": g, "job_title": inbox.group_title(conn, g)} for g in ids]
        inbox.set_labels(conn, ids, label)
        return templates.TemplateResponse(
            request,
            "_inbox_bulk.html",
            {"triaged": triaged, "label": label, "ids": ids, "undo": False, "restored": []},
        )

    @app.post("/inbox/bulk/undo", response_class=HTMLResponse)
    def bulk_undo(
        request: Request,
        conn: Conn,
        group_id: Annotated[list[int] | None, Form()] = None,
    ) -> HTMLResponse:
        ids = bulk_ids(conn, group_id or [])
        inbox.undo_labels(conn, ids)
        profile, _ = load_console_profile(request.app.state.settings)
        restored = []
        for g in ids:
            item = inbox.inbox_item(conn, profile, g) if profile else None
            restored.append({"group_id": g, "item": item})
        return templates.TemplateResponse(
            request,
            "_inbox_bulk.html",
            {"triaged": [], "label": None, "ids": ids, "undo": True, "restored": restored},
        )

    @app.post("/inbox/{group_id}/label", response_class=HTMLResponse)
    def label(request: Request, conn: Conn, group_id: int, label: str) -> HTMLResponse:
        if label not in ("interesting", "not_interesting"):
            raise HTTPException(422, "label must be interesting or not_interesting")
        if not inbox.group_exists(conn, group_id):
            raise HTTPException(404, "no such job group")
        refuse_triaged(conn, [group_id])
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
