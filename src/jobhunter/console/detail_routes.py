"""Routes for /job/{id}, /job/{id}/apply-button, /job/{id}/apply-link, /apply/{id},
/job/{id}/applied (specs/007, 015).

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
from jobhunter.console import apply_bg, capture_routes, detail
from jobhunter.core.models import ApplyLink, ApplyStatus
from jobhunter.pipeline import applylink
from jobhunter.pipeline.apply_rank import HostClasses
from jobhunter.pipeline.ats_rules import host_of, is_http_url
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

    # Network work for apply links runs off the request thread (bug e0d047b): a request
    # waits at most apply_bg.WAIT_S, then answers with the stored link.
    worker = apply_bg.ApplyLinkWorker(lambda: app.state.conn_factory(), ctx_factory)
    app.state.apply_link_worker = worker

    def walk(group_id: int, legacy: bool, at: datetime) -> apply_bg.Work:
        """Resolve a group with no row, or again for an older login-wall row (334d9eb).
        Returns whether it settled the link (else the worker backs the group off)."""

        def work(c: sqlite3.Connection, cf: applylink.CtxFactory) -> bool:
            if legacy:
                link = applylink.reresolve_legacy(c, group_id, now=at, ctx_factory=cf)
                return link is not None and not applylink.needs_reresolve(link)
            try:
                applylink.resolve_group(c, group_id, now=at, ctx_factory=cf)
            except ValueError:
                return False  # no usable URL: Open posting stays
            return True

        return work

    def reverify(group_id: int, at: datetime) -> apply_bg.Work:
        def work(c: sqlite3.Connection, cf: applylink.CtxFactory) -> None:
            applylink.reverify(c, group_id, now=at, ctx_factory=cf, max_age_hours=MAX_VERIFY_AGE_H)

        return work

    def freshen(group_id: int, link: ApplyLink | None, at: datetime) -> None:
        """A stale live link on show: re-verify it in the background, without waiting, so a
        click finds it fresh (bug e0d047b review F2c)."""
        if link is not None and applylink.reverify_due(link, at, MAX_VERIFY_AGE_H):
            worker.run(group_id, reverify(group_id, at), wait_s=0)

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
        extras = capture_routes.job_extras(request, conn, d, now())
        freshen(group_id, applylink.get_apply_link(conn, group_id), now())
        return templates.TemplateResponse(
            request,
            "job.html",
            {"title": d.job["title"], "active": "/inbox", "nav": nav, "d": d, **extras},
        )

    @app.get("/job/{group_id}/apply-button", response_class=HTMLResponse)
    def apply_button(request: Request, conn: Conn, group_id: int) -> HTMLResponse:
        require_job(conn, group_id)
        link = applylink.get_apply_link(conn, group_id)
        outcome: apply_bg.Outcome = "done"
        if link is None or applylink.needs_reresolve(link):
            # No row yet, or an older row that took a login wall for the posting (334d9eb).
            outcome = worker.run(group_id, walk(group_id, link is not None, now()), walk=True)
            link = applylink.get_apply_link(conn, group_id)
        else:
            freshen(group_id, link, now())
        btn = detail.apply_button(conn, group_id, now(), link, fetch_link=False)
        # Still resolving: the fragment asks again (hx-trigger=load) and shows the stored link.
        btn.pending = btn.pending and outcome == "pending"
        return templates.TemplateResponse(request, "_apply_button.html", {"b": btn})

    @app.get("/job/{group_id}/did-apply", response_class=HTMLResponse)
    def did_apply_banner(request: Request, conn: Conn, group_id: int) -> HTMLResponse:
        require_job(conn, group_id)
        prompt = detail.did_you_apply(conn, group_id)
        return templates.TemplateResponse(
            request,
            "_did_apply.html",
            {"prompt": prompt, "group_id": group_id},
        )

    @app.get("/apply/{group_id}")
    def apply(request: Request, conn: Conn, group_id: int) -> Response:
        job = require_job(conn, group_id)
        current = now()
        link = applylink.get_apply_link(conn, group_id)
        if link is None and not is_http_url(detail.posting_url(job)):
            # A posting pasted without a URL (specs/017): its url is a placeholder, never opened.
            raise HTTPException(404, "this posting has no URL to open")
        legacy = link is not None and applylink.needs_reresolve(link)
        if link is not None and (legacy or applylink.reverify_due(link, current, MAX_VERIFY_AGE_H)):
            # An older row that took a login wall for the posting (334d9eb) resolves again,
            # so Open application on a packet goes to the employer, not the sign-in page; a
            # stale live link is re-verified. A walk waits at most apply_bg.WAIT_S, a
            # re-verify (one host, two requests) apply_bg.REVERIFY_WAIT_S; then the work
            # carries on in the background (bug e0d047b): the click goes to the best link
            # already known instead of hanging on slow sites.
            if legacy:
                outcome = worker.run(group_id, walk(group_id, True, current), walk=True)
            else:
                outcome = worker.run(
                    group_id, reverify(group_id, current), wait_s=apply_bg.REVERIFY_WAIT_S
                )
            if outcome == "done":
                link = applylink.get_apply_link(conn, group_id)
        if link is not None and link.status == ApplyStatus.expired:
            original = link.final_url or link.start_url
            log_click(conn, group_id, original, "expired", current)
            # Only an ATS or the employer's site speaks for the job; a dead tracker or board
            # copy is just a link that stopped working (334d9eb).
            employer_page = HostClasses.load(conn, group_id).employer_or_ats(original)
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
                    "employer_page": employer_page,
                    "original_host": host_of(original),
                },
            )
        # live, or unresolved/blocked: hand over the best known link
        target = (link.final_url or link.start_url) if link else detail.posting_url(job)
        if link is not None and applylink.needs_reresolve(link):
            target = link.start_url  # an older row whose final URL is a login wall
        log_click(conn, group_id, target, "redirected", current)
        return RedirectResponse(target, status_code=302)

    def log_click(
        conn: sqlite3.Connection, group_id: int, url: str, outcome: str, at: datetime
    ) -> None:
        apply_bg.log_click_safely(conn, app.state.conn_factory, group_id, url, outcome, at)

    @app.post("/job/{group_id}/apply-link")
    async def set_apply_link(request: Request, conn: Conn, group_id: int) -> Response:
        """Use this apply link: the user's URL wins over any resolved one and is never replaced
        automatically; ``reset`` drops it so the group resolves again (bug 334d9eb). Same-origin
        like every console write (``app.same_origin_writes``)."""
        require_job(conn, group_id)
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        if (form.get("reset") or [""])[-1]:
            applylink.clear_user_link(conn, group_id)
        else:
            url = (form.get("url") or [""])[-1]
            try:
                applylink.set_user_link(conn, group_id, url, now())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/job/{group_id}", status_code=303)

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
        detail.answer_prompt(
            conn, group_id, choice, now(), request.app.state.settings.paths.data_dir
        )
        return HTMLResponse(f'<div class="did-apply done" id="did-apply">{messages[choice]}</div>')
