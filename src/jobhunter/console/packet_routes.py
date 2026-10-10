"""Assisted apply, phase 1a (specs/017): Prepare, New packet, the packet page, Score now.

Nothing here submits an application or generates a document. The only paid action is
**Score this group now**: the page shows the Stage 2 estimate, the button asks to confirm, and
the form posts that estimate's token back; it runs only when the estimate built at click time is
the same one. Registered before ``detail_routes`` so ``/apply/new`` is not read as
``/apply/{group_id}``.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.apply import packets, paste
from jobhunter.apply import score as group_score
from jobhunter.console import detail
from jobhunter.console.inbox_routes import load_console_profile
from jobhunter.console.tracking_routes import Form
from jobhunter.pipeline import applylink
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import ScorerError, privacy_notice

NEW_FIELDS = ("url", "text", "employer", "title")


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    get_profile: Callable[[], Profile],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    # Tests inject fakes; None builds the real scorer / Anthropic client at click time.
    app.state.packet_scorer_factory = None
    app.state.packet_client_factory = None

    def ctx_factory(conn: sqlite3.Connection) -> applylink.CtxFactory:
        injected = getattr(app.state, "ctx_factory", None)
        return injected or applylink.default_ctx_factory(app.state.settings, conn)

    # ─── New packet ─────────────────────────────────────────────────────────

    def new_page(
        request: Request,
        fields: dict[str, str] | None = None,
        *,
        message: str | None = None,
        note: str | None = None,
        dups: list[paste.Duplicate] | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "new_packet.html",
            {
                "title": "New packet",
                "active": "/inbox",
                "nav": nav,
                "f": {k: (fields or {}).get(k, "") for k in NEW_FIELDS},
                "message": message,
                "note": note,
                "dups": dups or [],
                "can_force": bool(dups) and not any(d.by_url for d in dups or []),
            },
            status_code=status,
        )

    @app.get("/apply/new", response_class=HTMLResponse)
    def new_packet(request: Request) -> HTMLResponse:
        return new_page(request)

    @app.post("/apply/new/fetch", response_class=HTMLResponse)
    def new_packet_fetch(request: Request, conn: Conn, form: Form) -> HTMLResponse:
        """Fetch posting text: through FetchContext, so the host's robots.txt decides."""
        f = {k: form.get(k, "") for k in NEW_FIELDS}
        if not f["url"].strip():
            return new_page(request, f, message="Enter the posting URL to fetch it.", status=422)
        try:
            got = paste.fetch_posting(ctx_factory(conn), f["url"])
        except paste.PasteError as exc:
            return new_page(request, f, message=str(exc))
        f["text"] = f["text"].strip() or got.text
        f["title"] = f["title"].strip() or (got.title or "")
        f["employer"] = f["employer"].strip() or (got.employer or "")
        return new_page(
            request,
            f,
            note="Fetched. Check the text, employer and title, then create the packet.",
        )

    @app.post("/apply/new")
    def new_packet_create(request: Request, conn: Conn, form: Form) -> Response:
        f = {k: form.get(k, "") for k in NEW_FIELDS}
        url = f["url"].strip() or None
        try:
            key_url = paste.direct_board_url(url) if url else None
        except paste.PasteError as exc:
            return new_page(request, f, message=str(exc), status=422)
        if not f["employer"].strip() and key_url:
            f["employer"] = paste.guess_employer_from_url(key_url) or ""
        if not (f["employer"].strip() and f["title"].strip()):
            return new_page(request, f, message="Employer and title are both required.", status=422)
        if not url and not f["text"].strip():
            return new_page(
                request, f, message="Give the posting URL or paste its text (or both).", status=422
            )
        dups = paste.find_duplicates(conn, key_url, f["employer"], f["title"])
        forced = form.get("force") == "1" and not any(d.by_url for d in dups)
        if dups and not forced:
            return new_page(
                request,
                f,
                message="You already have this posting. Prepare that one instead?",
                dups=dups,
                status=409,
            )
        try:
            _gid, pid = paste.create_pasted_packet(
                conn,
                url=url,
                text=f["text"],
                employer=f["employer"],
                title=f["title"],
                now=now(),
            )
        except paste.PasteError as exc:
            return new_page(request, f, message=str(exc), status=422)
        return RedirectResponse(f"/packet/{pid}", status_code=303)

    # ─── Prepare ────────────────────────────────────────────────────────────

    @app.post("/job/{group_id}/prepare")
    def prepare(conn: Conn, group_id: int) -> Response:
        """Open (or reuse) this job's packet: shortlist, application at 'preparing'."""
        try:
            pid = packets.prepare(conn, group_id, now())
        except KeyError as exc:
            raise HTTPException(404, "no such job group") from exc
        return RedirectResponse(f"/packet/{pid}", status_code=303)

    # ─── packet page ────────────────────────────────────────────────────────

    def require_packet(conn: sqlite3.Connection, packet_id: int) -> packets.Packet:
        p = packets.get_packet(conn, packet_id)
        if p is None:
            raise HTTPException(404, "no such packet")
        return p

    def score_ctx(
        request: Request,
        conn: sqlite3.Connection,
        p: packets.Packet,
        message: str | None = None,
        ok: str | None = None,
    ) -> dict[str, Any]:
        settings = request.app.state.settings
        days = settings.scoring.employer_rejection_days
        d = detail.load_detail(conn, get_profile(), p.group_id, now(), rejection_days=days)
        profile, profile_error = load_console_profile(settings)
        est = notice = None
        if profile is not None and d is not None and not d.scored:
            est = group_score.estimate(conn, profile, settings.scoring, p.group_id, now())
            try:
                notice = privacy_notice(est.scorer, settings.scoring)
            except ScorerError as exc:
                message = message or str(exc)
        return {
            "p": p,
            "d": d,
            "est": est,
            "notice": notice,
            "profile_error": profile_error,
            "score_message": message,
            "score_ok": ok,
        }

    @app.get("/packet/{packet_id}", response_class=HTMLResponse)
    def packet_page(request: Request, conn: Conn, packet_id: int) -> HTMLResponse:
        p = require_packet(conn, packet_id)
        ctx = score_ctx(request, conn, p)
        return templates.TemplateResponse(
            request,
            "packet.html",
            {"title": f"Packet: {p.title}", "active": "/pipeline", "nav": nav, **ctx},
        )

    @app.post("/packet/{packet_id}/posting")
    def packet_posting(conn: Conn, form: Form, packet_id: int) -> Response:
        """Paste the posting text for a group that has none (no re-score is queued)."""
        p = require_packet(conn, packet_id)
        try:
            paste.store_posting_text(conn, p.group_id, form.get("text", ""), now())
        except paste.PasteError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/packet/{packet_id}", status_code=303)

    @app.post("/packet/{packet_id}/score")
    def packet_score(request: Request, conn: Conn, form: Form, packet_id: int) -> Response:
        """Score this group now: an explicit, confirmed paid action (one Stage 2 screen)."""
        p = require_packet(conn, packet_id)
        settings = request.app.state.settings
        profile, error = load_console_profile(settings)
        message = ok = None
        if profile is None:
            message = f"Fix the preferences file first: {error}"
        else:
            try:
                out = group_score.score_now(
                    conn,
                    profile,
                    settings.scoring,
                    p.group_id,
                    token=form.get("token"),
                    now=now(),
                    scorer_factory=request.app.state.packet_scorer_factory,
                    client_factory=request.app.state.packet_client_factory,
                )
                ok = out.detail
            except group_score.ScoreRefused as exc:
                message = str(exc)
        ctx = score_ctx(request, conn, p, message, ok)
        if request.headers.get("hx-request") == "true":
            return templates.TemplateResponse(request, "_packet_score.html", ctx)
        if message is None:
            return RedirectResponse(f"/packet/{packet_id}", status_code=303)
        return templates.TemplateResponse(
            request,
            "packet.html",
            {"title": f"Packet: {p.title}", "active": "/pipeline", "nav": nav, **ctx},
        )
