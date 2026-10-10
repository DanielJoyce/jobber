"""Routes for /job/{id}, /job/{id}/apply-button, /apply/{id}, /job/{id}/applied (specs/007, 015).

Nothing here submits an application: /apply only redirects the browser to the employer.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.apply import paste
from jobhunter.console import detail
from jobhunter.core.models import ApplyStatus
from jobhunter.pipeline import applylink
from jobhunter.pipeline.ats_rules import is_http_url
from jobhunter.scoring.profile import Profile

MAX_VERIFY_AGE_H = 24


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    get_profile: Callable[[], Profile],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def ctx_factory(conn: sqlite3.Connection) -> applylink.CtxFactory:
        # Tests (or other embedders) may inject app.state.ctx_factory: url -> FetchContext.
        injected = getattr(app.state, "ctx_factory", None)
        return injected or applylink.default_ctx_factory(app.state.settings, conn)

    def require_job(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row:
        job = detail.group_job(conn, group_id)
        if job is None:
            raise HTTPException(404, "no such job group")
        return job

    @app.get("/job/{group_id}", response_class=HTMLResponse)
    def job_page(request: Request, conn: Conn, group_id: int) -> HTMLResponse:
        days = request.app.state.settings.scoring.employer_rejection_days
        d = detail.load_detail(conn, get_profile(), group_id, now(), rejection_days=days)
        if d is None:
            raise HTTPException(404, "no such job group")
        return templates.TemplateResponse(
            request,
            "job.html",
            {"title": d.job["title"], "active": "/inbox", "nav": nav, "d": d},
        )

    @app.get("/job/{group_id}/apply-button", response_class=HTMLResponse)
    def apply_button(request: Request, conn: Conn, group_id: int) -> HTMLResponse:
        require_job(conn, group_id)
        link = applylink.get_apply_link(conn, group_id)
        if link is None:
            try:
                link = applylink.resolve_group(
                    conn, group_id, now=now(), ctx_factory=ctx_factory(conn)
                )
            except ValueError:
                link = None
        btn = detail.apply_button(conn, group_id, now(), link, fetch_link=False)
        btn.pending = False
        return templates.TemplateResponse(request, "_apply_button.html", {"b": btn})

    @app.get("/apply/{group_id}")
    def apply(request: Request, conn: Conn, group_id: int) -> Response:
        job = require_job(conn, group_id)
        current = now()
        link = applylink.get_apply_link(conn, group_id)
        if link is None and not is_http_url(detail.posting_url(job)):
            # A posting pasted without a URL (specs/017): its url is a placeholder, never opened.
            raise HTTPException(404, "this posting has no URL to open")
        if link is not None and link.status == ApplyStatus.live:
            link = applylink.reverify(
                conn,
                group_id,
                now=current,
                ctx_factory=ctx_factory(conn),
                max_age_hours=MAX_VERIFY_AGE_H,
            )
        if link is not None and link.status == ApplyStatus.expired:
            original = link.final_url or link.start_url
            detail.log_click(conn, group_id, original, "expired", current)
            return templates.TemplateResponse(
                request,
                "apply_closed.html",
                {
                    "title": "Posting closed",
                    "active": "/inbox",
                    "nav": nav,
                    "group_id": group_id,
                    "job_title": job["title"],
                    "original": original,
                },
            )
        # live, or unresolved/blocked: hand over the best known link
        target = (link.final_url or link.start_url) if link else detail.posting_url(job)
        detail.log_click(conn, group_id, target, "redirected", current)
        return RedirectResponse(target, status_code=302)

    @app.post("/job/{group_id}/description")
    async def paste_description(request: Request, conn: Conn, group_id: int) -> Response:
        """Pasted posting text for a partial job: store it and queue a re-score (specs/012)."""
        job = require_job(conn, group_id)
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        text = (form.get("text") or [""])[-1]
        if not text.strip():
            raise HTTPException(422, "paste the posting's description text")
        if job["score_on_request"]:
            # A group scored only on request (specs/017): store, never queue a re-score.
            paste.store_posting_text(conn, group_id, text, now())
        else:
            detail.paste_description(conn, group_id, text, now())
        return RedirectResponse(f"/job/{group_id}", status_code=303)

    @app.post("/job/{group_id}/applied", response_class=HTMLResponse)
    def applied(request: Request, conn: Conn, group_id: int, choice: str) -> HTMLResponse:
        require_job(conn, group_id)
        messages = {
            "yes": "Marked as applied.",
            "not_yet": "Okay, keeping it at preparing.",
            "not_interested": "Dismissed as not interesting.",
        }
        if choice not in messages:
            raise HTTPException(422, "choice must be yes, not_yet or not_interested")
        detail.answer_prompt(conn, group_id, choice, now())
        return HTMLResponse(f'<div class="did-apply done" id="did-apply">{messages[choice]}</div>')
