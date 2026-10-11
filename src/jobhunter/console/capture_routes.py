"""Where captured jobs show, and the console halves of Capture (specs/017 phase 1e).

- ``/captured``: pasted and captured postings, newest first, scored or not (a captured group
  has no score and no application until the user acts, so the inbox does not show it yet).
- ``/job/{id}/score``: **Score this group now** for a group scored only on request, the same
  confirmed, estimate-tokened paid action as on the packet page.
- ``/job/{id}/link``: **Link them** with a group that is surely the same posting.
- ``/prefs/ext/pair``: **Pair browser extension**, a one-time code shown once.

All POSTs here are ordinary console writes behind ``same_origin_writes``.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.apply import capture, export, ext_pairing
from jobhunter.apply import score as group_score
from jobhunter.console import detail
from jobhunter.console.inbox_routes import load_console_profile
from jobhunter.console.tracking_routes import Form
from jobhunter.core import db
from jobhunter.core.manual_sources import PASTE_MANUAL
from jobhunter.pipeline.ats_rules import host_of, is_http_url
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import ScorerError, privacy_notice

CAPTURED_LIMIT = 500


def score_panel(
    request: Request,
    conn: sqlite3.Connection,
    d: detail.Detail,
    now: datetime,
    message: str | None = None,
    ok: str | None = None,
) -> dict[str, Any]:
    """Context for ``_group_score.html`` (a group scored only on request, not yet scored)."""
    settings = request.app.state.settings
    profile, profile_error = load_console_profile(settings)
    est = notice = None
    if profile is not None and not d.scored:
        est = group_score.estimate(conn, profile, settings.scoring, d.group_id, now)
        try:
            notice = privacy_notice(est.scorer, settings.scoring)
        except ScorerError as exc:
            message = message or str(exc)
    return {
        "d": d,
        "est": est,
        "notice": notice,
        "profile_error": profile_error,
        "score_message": message,
        "score_ok": ok,
    }


def job_extras(
    request: Request, conn: sqlite3.Connection, d: detail.Detail, now: datetime
) -> dict[str, Any]:
    """What ``/job/{id}`` adds for a group scored only on request: Score now and Link them."""
    if not d.score_on_request:
        return {"group_score": None, "same_job": []}
    return {
        "group_score": score_panel(request, conn, d, now) if not d.scored else None,
        "same_job": capture.same_job_candidates(conn, d.group_id),
    }


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    get_profile: Callable[[], Profile],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def require_detail(request: Request, conn: sqlite3.Connection, gid: int) -> detail.Detail:
        days = request.app.state.settings.scoring.employer_rejection_days
        d = detail.load_detail(conn, get_profile(), gid, now(), rejection_days=days)
        if d is None:
            raise HTTPException(404, "no such job group")
        return d

    @app.get("/captured", response_class=HTMLResponse)
    def captured(request: Request, conn: Conn) -> HTMLResponse:
        rows = conn.execute(
            "SELECT g.id, j.title, j.employer, j.agency_raw, j.url, j.first_seen_at, "
            "g.score_on_request FROM job_group g JOIN job j ON j.id = g.canonical_job_id "
            "WHERE EXISTS (SELECT 1 FROM job p WHERE p.job_group_id = g.id "
            "AND p.source_key = ?) ORDER BY j.first_seen_at DESC, g.id DESC LIMIT ?",
            (PASTE_MANUAL, CAPTURED_LIMIT),
        ).fetchall()
        profile = get_profile()
        items = []
        for r in rows:
            d = detail.load_detail(conn, profile, r["id"], now())
            url = r["url"] or ""
            items.append(
                {
                    "group_id": r["id"],
                    "title": r["title"],
                    "employer": r["employer"] or r["agency_raw"] or "employer not stated",
                    "host": host_of(url) if is_http_url(url) else "pasted text",
                    "captured": (r["first_seen_at"] or "")[:10],
                    "bucket": d.bucket if d is not None and d.scored else None,
                }
            )
        return templates.TemplateResponse(
            request,
            "captured.html",
            {"title": "Captured", "active": "/inbox", "nav": nav, "items": items},
        )

    @app.post("/job/{group_id}/score")
    def job_score(request: Request, conn: Conn, form: Form, group_id: int) -> Response:
        """Score this group now from the job page (specs/017 1e): confirmed, paid, one group.

        A plain ``def``: FastAPI runs it in the threadpool, so a synchronous scorer (up to its
        180-second timeout) never blocks the console's event loop.
        """
        d = require_detail(request, conn, group_id)
        if not d.score_on_request:
            raise HTTPException(409, "this posting is scored by the nightly run")
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
                    group_id,
                    token=str(form.get("token") or ""),
                    now=now(),
                    scorer_factory=request.app.state.packet_scorer_factory,
                    client_factory=request.app.state.packet_client_factory,
                )
                ok = out.detail
            except group_score.ScoreRefused as exc:
                message = str(exc)
        if request.headers.get("hx-request") == "true":
            d = require_detail(request, conn, group_id)
            ctx = score_panel(request, conn, d, now(), message, ok)
            return templates.TemplateResponse(request, "_group_score.html", ctx)
        return RedirectResponse(f"/job/{group_id}", status_code=303)

    @app.post("/job/{group_id}/link")
    def job_link(request: Request, conn: Conn, form: Form, group_id: int) -> Response:
        """**Link them** on the job page: only with a group that is surely the same posting."""
        try:
            other = int(str(form.get("other") or ""))
        except ValueError as exc:
            raise HTTPException(422, "which posting to link?") from exc
        if other not in {c.group_id for c in capture.same_job_candidates(conn, group_id)}:
            raise HTTPException(409, "those two are not known to be the same posting")
        try:
            with db.transaction(conn):
                kept = capture.link_same_job(conn, group_id, other, now())
        except capture.Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        # A link can abandon a packet: its named copies go now (019561b (3)).
        export.sync_abandoned(conn, request.app.state.settings.paths.data_dir)
        return RedirectResponse(f"/job/{kept}", status_code=303)

    @app.post("/prefs/ext/pair", response_class=HTMLResponse)
    def pair_code(request: Request) -> HTMLResponse:
        settings = request.app.state.settings
        code, expires = ext_pairing.new_code(ext_pairing.state_path(settings.paths.data_dir), now())
        return templates.TemplateResponse(
            request,
            "_ext_pair.html",
            {"code": code, "expires": expires.astimezone().strftime("%H:%M")},
        )
