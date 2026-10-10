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

from jobhunter.apply import (
    answers,
    cli_runner,
    documents,
    generator,
    labels,
    packets,
    paste,
    review,
    runner_state,
)
from jobhunter.apply import score as group_score
from jobhunter.console import detail, packet_docs
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
    # The drafting API client (Generate on the API runner); tests inject a fake.
    app.state.packet_api_client_factory = None

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

    # ─── documents (phase 1b) ───────────────────────────────────────────────

    def docs_ctx(
        request: Request,
        conn: sqlite3.Connection,
        p: packets.Packet,
        *,
        view: dict[str, int | None] | None = None,
        gen: dict[str, Any] | None = None,
        doc_message: str | None = None,
        numeric: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Everything the documents part of the packet page needs. Runs nothing."""
        settings = request.app.state.settings
        view = view or {}
        out: dict[str, Any] = {
            "runner": packet_docs.runner_status(conn, settings, now()),
            "gen": gen,
            "doc_message": doc_message,
            "numeric": numeric,
            "notes": answers.employer_notes(conn, p.id),
            "blockers": review.ready_blockers(conn, p.id),
            "drafts": [(v, packet_docs.draft_view(v)) for v in review.question_drafts(conn, p.id)],
        }
        for kind, viewer in (
            ("resume", packet_docs.resume_view),
            ("cover_letter", packet_docs.letter_view),
        ):
            current_id = generator.current_doc_id(conn, p.id, kind)
            all_versions = review.versions(conn, p.id, kind)
            shown_id = view.get(kind) or current_id
            shown = next((v for v in all_versions if v.id == shown_id), None)
            out[kind] = {
                "current_id": current_id,
                "versions": all_versions,
                "shown": shown,
                "is_current": shown is not None and shown.id == current_id,
                "view": viewer(shown) if shown is not None else None,
            }
        return out

    def render_packet(
        request: Request,
        conn: sqlite3.Connection,
        p: packets.Packet,
        *,
        status: int = 200,
        score_message: str | None = None,
        score_ok: str | None = None,
        **docs: Any,
    ) -> HTMLResponse:
        p = require_packet(conn, p.id)  # re-read: status and pointers may have moved
        ctx = score_ctx(request, conn, p, score_message, score_ok)
        ctx.update(docs_ctx(request, conn, p, **docs))
        return templates.TemplateResponse(
            request,
            "packet.html",
            {"title": f"Packet: {p.title}", "active": "/pipeline", "nav": nav, **ctx},
            status_code=status,
        )

    def back(packet_id: int, anchor: str) -> RedirectResponse:
        return RedirectResponse(f"/packet/{packet_id}#{anchor}", status_code=303)

    def apply_profile(request: Request) -> Profile:
        profile, error = load_console_profile(request.app.state.settings)
        if profile is None:
            raise generator.ApplyRefused(f"fix the preferences file first: {error}")
        return profile

    @app.post("/packet/{packet_id}/generate")
    def packet_generate(request: Request, conn: Conn, form: Form, packet_id: int) -> Response:
        """Generate a resume, cover letter or question draft on the runner the click names.

        ``runner=api`` comes only from a button that shows the API estimate; a CLI failure
        re-renders the page with that button and never calls the API by itself."""
        p = require_packet(conn, packet_id)
        kind = form.get("kind", "resume")
        runner = form.get("runner", "")
        question = form.get("question", "")
        anchor = {"resume": "resume", "cover_letter": "letter"}.get(kind, "drafts")
        gen = {
            "kind": kind,
            "runner": runner,
            "question": question,
            "facts": form.get("facts", ""),
            "instruction": form.get("instruction", ""),
        }
        numeric = None
        try:
            if runner not in ("cli", "api"):
                raise generator.ApplyRefused("pick a runner")
            profile = apply_profile(request)
            if kind == "question_draft":
                if labels.never_store(question) is not None:
                    raise generator.ApplyRefused(labels.YOURS)
                if labels.question_kind(question) == "numeric":
                    numeric = {
                        "question": question,
                        "lines": generator.numeric_evidence(
                            documents.numbered_resume(profile.resume_text), question
                        ),
                    }
                    return render_packet(request, conn, p, numeric=numeric)
                facts = answers.clean_lines(form.get("facts", ""))
                if facts:
                    answers.save_packet_answer(
                        conn, p.id, answers.story_key(question), question, "\n".join(facts)
                    )
            generator.generate(
                conn,
                request.app.state.settings,
                profile,
                p.id,
                kind,
                runner=runner,
                now=now(),
                question=question,
                instruction=form.get("instruction", ""),
                confirm_paid=form.get("confirm_paid") == "1",
                client_factory=request.app.state.packet_api_client_factory,
            )
        except generator.ApplyRefused as exc:
            gen.update(message=exc.message, offer_api=exc.offer_api, paid=exc.needs_paid_confirm)
            return render_packet(request, conn, p, status=409, gen=gen)
        except generator.GenerateFailed as exc:
            gen.update(message=exc.reason, offer_api=exc.offer_api, paid=False, failed=True)
            return render_packet(request, conn, p, status=409, gen=gen)
        except (answers.NeverStore, ValueError) as exc:
            gen.update(message=str(exc), offer_api=False, paid=False)
            return render_packet(request, conn, p, status=409, gen=gen)
        return back(packet_id, anchor)

    @app.post("/packet/{packet_id}/base")
    def packet_base(request: Request, conn: Conn, packet_id: int) -> Response:
        """Use base resume: a free version that is the resume itself."""
        p = require_packet(conn, packet_id)
        try:
            generator.use_base_resume(conn, apply_profile(request), p.id, now=now())
        except generator.ApplyRefused as exc:
            return render_packet(request, conn, p, status=409, doc_message=exc.message)
        return back(packet_id, "resume")

    @app.post("/packet/{packet_id}/doc/{doc_id}/save")
    def packet_doc_save(request: Request, conn: Conn, form: Form, packet_id: int, doc_id: int):
        p = require_packet(conn, packet_id)
        try:
            new_id = review.save_edit(conn, p.id, doc_id, form, now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        v = review.get_version(conn, new_id)
        anchor = {"resume": "resume", "cover_letter": "letter"}.get(v.kind if v else "", "drafts")
        return back(packet_id, anchor)

    @app.post("/packet/{packet_id}/doc/{doc_id}/confirm")
    def packet_doc_confirm(request: Request, conn: Conn, form: Form, packet_id: int, doc_id: int):
        """This is true, keep it: one unsupported line of the current version."""
        p = require_packet(conn, packet_id)
        try:
            review.confirm(conn, p.id, doc_id, form.get("ckey", ""), now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        v = review.get_version(conn, doc_id)
        anchor = {"resume": "resume", "cover_letter": "letter"}.get(v.kind if v else "", "drafts")
        return back(packet_id, anchor)

    @app.post("/packet/{packet_id}/doc/{doc_id}/restore")
    def packet_doc_restore(request: Request, conn: Conn, form: Form, packet_id: int, doc_id: int):
        p = require_packet(conn, packet_id)
        try:
            review.restore(conn, p.id, doc_id, form.get("line", ""), now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        return back(packet_id, "resume")

    @app.post("/packet/{packet_id}/ready")
    def packet_ready(request: Request, conn: Conn, packet_id: int) -> Response:
        p = require_packet(conn, packet_id)
        try:
            review.mark_ready(conn, p.id, now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        return back(packet_id, "documents")

    @app.post("/packet/{packet_id}/notes")
    def packet_notes(request: Request, conn: Conn, form: Form, packet_id: int) -> Response:
        """Employer notes, one to three lines in your words (N1-N3)."""
        p = require_packet(conn, packet_id)
        try:
            lines = answers.clean_lines(form.get("notes", ""))
            answers.save_packet_answer(
                conn, p.id, answers.NOTES_KEY, "Employer notes", "\n".join(lines)
            )
        except (answers.NeverStore, ValueError) as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        return back(packet_id, "letter")

    @app.post("/apply/runner/on")
    def runner_on(request: Request, form: Form) -> Response:
        """Turn back on: clears the CLI runner's off state and the paid-usage hold."""
        runner_state.turn_on(request.app.state.settings.paths.data_dir)
        cli_runner.reset_auth_cache()
        target = form.get("next", "")
        safe = target.startswith("/") and not target.startswith("//") and "\\" not in target
        return RedirectResponse(target if safe else "/costs", status_code=303)

    @app.get("/packet/{packet_id}", response_class=HTMLResponse)
    def packet_page(
        request: Request,
        conn: Conn,
        packet_id: int,
        resume: int | None = None,
        letter: int | None = None,
    ) -> HTMLResponse:
        p = require_packet(conn, packet_id)
        return render_packet(request, conn, p, view={"resume": resume, "cover_letter": letter})

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
        return render_packet(request, conn, p, score_message=message, score_ok=ok)
