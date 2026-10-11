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
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.apply import (
    answers,
    checklists,
    cli_runner,
    documents,
    export,
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
from jobhunter.core import pdf
from jobhunter.pipeline import applylink
from jobhunter.scoring.profile import Profile, ProfileConflict, ProfileError
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
    def new_packet(request: Request, url: str | None = None) -> HTMLResponse:
        # ?url= only pre-fills the form (the extension's offer on a page Chrome does not let
        # it read, specs/017 1e); nothing is fetched or written.
        fields = None
        if url and paste.is_http_url(url.strip()):
            fields = {"url": url.strip()[:2048]}
            fields["employer"] = paste.guess_employer_from_url(fields["url"]) or ""
        return new_page(request, fields)

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
            "runner": packet_docs.runner_status(
                conn, settings, now(), request_chars=request_sizes(conn, p)
            ),
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

    # ─── export (phase 1c) ──────────────────────────────────────────────────

    # Tests inject a fake renderer; None prints with headless Chromium (the browser extra).
    app.state.packet_pdf_renderer = None
    app.state.export_notes = {}

    def pdf_renderer(request: Request) -> export.PdfRenderer:
        return request.app.state.packet_pdf_renderer or pdf.render_pdf

    def export_ctx(
        request: Request,
        conn: sqlite3.Connection,
        p: packets.Packet,
        msg: tuple[bool, str] | None,
    ) -> dict[str, Any]:
        data_dir = request.app.state.settings.paths.data_dir
        slots: dict[str, Any] = {}
        for kind in export.KINDS:
            exp = export.current_export(conn, p.id, kind)
            slots[kind] = (
                None
                if exp is None
                else {
                    "exp": exp,
                    "refusal": export.packet_refusal(p.status) or export.refusal(exp),
                    "on_disk": (
                        {f: False for f in export.FORMATS}
                        if export.packet_refusal(p.status)
                        else export.available(data_dir, p.id, exp)
                    ),
                    "named_version": export.named_version(data_dir, p.id, exp),
                }
            )
        # The result of the last Export click (it redirects, so it is kept here once).
        notes: dict[int, tuple[bool, str]] = request.app.state.export_notes
        return {"export": slots, "export_msg": msg or notes.pop(p.id, None)}

    def request_sizes(conn: sqlite3.Connection, p: packets.Packet) -> dict[str, int]:
        """This packet's real request sizes, so a button's estimate is for what it sends."""
        profile, _error = load_console_profile(app.state.settings)
        if profile is None or not profile.resume_text.strip():
            return {}
        try:
            ctx = generator.load_context(conn, profile, p.id)
            sizes = {
                k: generator.build_request(conn, ctx, k).chars for k in ("resume", "cover_letter")
            }
            sizes["question_draft"] = generator.build_request(
                conn, ctx, "question_draft", question="x" * 200
            ).chars
            return sizes
        except generator.ApplyRefused:
            return {}

    # ─── answers, checklist, already applied (phase 1d) ─────────────────────

    def answers_ctx(
        request: Request,
        conn: sqlite3.Connection,
        p: packets.Packet,
        *,
        message: str | None = None,
        ok: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Saved answers, reuse offers, /prefs answers and the board checklist. Runs nothing
        and sends nothing: these are shown to copy, never given to a model."""
        loaded = answers.load_answers(Path(request.app.state.settings.paths.profile_dir))
        saved = answers.packet_answers(conn, p.id)
        drafts = review.question_drafts(conn, p.id)
        questions: dict[str, str] = {}
        for v in drafts:
            questions.setdefault(v.question_key, v.doc.get("question") or v.question_key)
        for s in saved:
            questions.setdefault(s.field_key.removeprefix(answers.QUESTION_PREFIX), s.question)
        mine = {s.field_key: s.value for s in saved}
        reuse = []
        for key, text in questions.items():
            fk = answers.QUESTION_PREFIX + key
            earlier = [
                e for e in answers.earlier_answers(conn, p.id, fk) if e.value != mine.get(fk)
            ]
            if earlier:
                reuse.append({"question": text, "earlier": earlier})
        return {
            "saved_answers": saved,
            "hidden_answers": answers.hidden_answers(conn, p.id),
            "draft_saves": [(v, v.question_key in {k[2:] for k in mine}) for v in drafts],
            "reuse": reuse,
            "prefs_answers": loaded.answers,
            "prefs_offers": answers.prefs_offers(loaded.answers, set(questions)),
            "prefs_link_fields": answers.LINK_FIELDS,
            "prefs_text_fields": answers.TEXT_FIELDS,
            "answers_warnings": loaded.warnings,
            "answers_message": message,
            "answers_ok": ok,
            "checklist": checklists.for_packet(conn, p.id),
            "evidence": evidence,
            "applied_before": packets.already_applied(conn, p),
        }

    def render_packet(
        request: Request,
        conn: sqlite3.Connection,
        p: packets.Packet,
        *,
        status: int = 200,
        score_message: str | None = None,
        score_ok: str | None = None,
        export_msg: tuple[bool, str] | None = None,
        answers_extra: dict[str, Any] | None = None,
        **docs: Any,
    ) -> HTMLResponse:
        p = require_packet(conn, p.id)  # re-read: status and pointers may have moved
        warn = export.safe_sync(conn, request.app.state.settings.paths.data_dir, p.id)
        ctx = score_ctx(request, conn, p, score_message, score_ok)
        ctx.update(docs_ctx(request, conn, p, **docs))
        ctx.update(export_ctx(request, conn, p, export_msg or ((False, warn) if warn else None)))
        ctx.update(answers_ctx(request, conn, p, **(answers_extra or {})))
        return templates.TemplateResponse(
            request,
            "packet.html",
            {"title": f"Packet: {p.title}", "active": "/pipeline", "nav": nav, **ctx},
            status_code=status,
        )

    def back(
        request: Request, conn: sqlite3.Connection, packet_id: int, anchor: str
    ) -> RedirectResponse:
        # The version may just have changed: the files to attach must follow it.
        warn = export.safe_sync(conn, request.app.state.settings.paths.data_dir, packet_id)
        if warn:
            request.app.state.export_notes[packet_id] = (False, warn)
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
            if kind == "cover_letter" and "notes" in form:
                # Notes typed beside Add cover letter go with it (saved first, as N1-N3).
                lines = answers.clean_lines(form.get("notes", ""))
                answers.save_packet_answer(
                    conn, p.id, answers.NOTES_KEY, "Employer notes", "\n".join(lines)
                )
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
        return back(request, conn, packet_id, anchor)

    @app.post("/packet/{packet_id}/base")
    def packet_base(request: Request, conn: Conn, packet_id: int) -> Response:
        """Use base resume: a free version that is the resume itself."""
        p = require_packet(conn, packet_id)
        try:
            generator.use_base_resume(conn, apply_profile(request), p.id, now=now())
        except generator.ApplyRefused as exc:
            return render_packet(request, conn, p, status=409, doc_message=exc.message)
        return back(request, conn, packet_id, "resume")

    @app.post("/packet/{packet_id}/doc/{doc_id}/save")
    def packet_doc_save(request: Request, conn: Conn, form: Form, packet_id: int, doc_id: int):
        p = require_packet(conn, packet_id)
        try:
            new_id = review.save_edit(conn, p.id, doc_id, form, now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        v = review.get_version(conn, new_id)
        anchor = {"resume": "resume", "cover_letter": "letter"}.get(v.kind if v else "", "drafts")
        return back(request, conn, packet_id, anchor)

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
        return back(request, conn, packet_id, anchor)

    @app.post("/packet/{packet_id}/doc/{doc_id}/restore")
    def packet_doc_restore(request: Request, conn: Conn, form: Form, packet_id: int, doc_id: int):
        p = require_packet(conn, packet_id)
        try:
            review.restore(conn, p.id, doc_id, form.get("line", ""), now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        return back(request, conn, packet_id, "resume")

    @app.post("/packet/{packet_id}/ready")
    def packet_ready(request: Request, conn: Conn, packet_id: int) -> Response:
        p = require_packet(conn, packet_id)
        try:
            review.mark_ready(conn, p.id, now())
        except review.ReviewError as exc:
            return render_packet(request, conn, p, status=409, doc_message=str(exc))
        return back(request, conn, packet_id, "documents")

    @app.post("/packet/{packet_id}/export")
    def packet_export(request: Request, conn: Conn, form: Form, packet_id: int) -> Response:
        """Write the current resume or cover letter as md, txt, html and (if possible) PDF."""
        p = require_packet(conn, packet_id)
        kind = form.get("kind", "")
        try:
            if (why := export.packet_refusal(p.status)) is not None:
                raise export.ExportError(why)
            exp = export.current_export(conn, p.id, kind)
            if exp is None:
                raise export.ExportError("There is no such document to export yet.")
            data_dir = request.app.state.settings.paths.data_dir
            written = export.write_export(conn, data_dir, p.id, exp, pdf_renderer(request))
        except export.ExportError as exc:
            return render_packet(request, conn, p, status=409, export_msg=(False, str(exc)))
        label = "cover letter" if kind == "cover_letter" else "resume"
        if written.pdf_error:
            text = (
                f"Saved the {label} (v{exp.version.version}) as Markdown, text and HTML. "
                f"No PDF: {written.pdf_error}"
            )
            request.app.state.export_notes[p.id] = (False, text)
        else:
            text = f"Saved the {label} (v{exp.version.version}) as PDF, text, Markdown and HTML."
            request.app.state.export_notes[p.id] = (True, text)
        return back(request, conn, p.id, "export")

    @app.get("/packet/{packet_id}/export/{slug}/{fmt}")
    def packet_export_file(
        request: Request, conn: Conn, packet_id: int, slug: str, fmt: str
    ) -> Response:
        """Download an exported file of the current version (the HTML opens inline to print)."""
        p = require_packet(conn, packet_id)
        kind = export.SLUGS.get(slug)
        if kind is None or fmt not in export.FORMATS:
            raise HTTPException(404, "no such export")
        exp = export.current_export(conn, p.id, kind)
        if exp is None or export.refusal(exp) or export.packet_refusal(p.status):
            raise HTTPException(404, "no such export")
        data_dir = request.app.state.settings.paths.data_dir
        path = export.versioned_path(data_dir, p.id, exp, fmt)
        if not path.is_file() or not export.exported(data_dir, p.id, exp):
            raise HTTPException(404, "not exported yet: press Export on the packet page")
        return FileResponse(
            path,
            media_type=export.MEDIA[fmt],
            filename=exp.download_name(fmt),
            content_disposition_type="inline" if fmt == "html" else "attachment",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
            },
        )

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
        return back(request, conn, packet_id, "letter")

    @app.post("/packet/{packet_id}/answers")
    def packet_save_answer(request: Request, conn: Conn, form: Form, packet_id: int) -> Response:
        """Save as answer: a question draft's text (``doc_id``) or your own (``value``), kept on
        this packet only. A never-store question is refused before anything is read."""
        p = require_packet(conn, packet_id)
        question, value, source = form.get("question", ""), form.get("value", ""), "user"
        if form.get("doc_id"):
            v = review.get_version(conn, int(form["doc_id"]) if form["doc_id"].isdigit() else 0)
            if v is None or v.packet_id != p.id or v.kind != "question_draft":
                raise HTTPException(404, "no such draft on this packet")
            question, value, source = v.doc.get("question") or v.question_key, v.body_md, "draft"
        try:
            answers.save_question_answer(conn, p.id, question, value, source)
        except (answers.NeverStore, ValueError) as exc:
            extra = {"message": str(exc)}
            return render_packet(request, conn, p, status=409, answers_extra=extra)
        return RedirectResponse(f"/packet/{packet_id}?answer=saved#answers", status_code=303)

    @app.post("/packet/{packet_id}/answers/{answer_id}/promote")
    def packet_promote_answer(
        request: Request, conn: Conn, packet_id: int, answer_id: int
    ) -> Response:
        """Promote to /prefs: copy a saved answer into Application answers (answers.custom),
        through the same model validation and round-trip write as the /prefs save."""
        p = require_packet(conn, packet_id)
        row = answers.get_answer(conn, answer_id)
        if row is None or row.packet_id != p.id:
            raise HTTPException(404, "no such saved answer on this packet")
        profile_dir = Path(request.app.state.settings.paths.profile_dir).expanduser()
        try:
            answers.promote(conn, profile_dir, answer_id)
        except ProfileConflict:
            message = "The preferences file changed while saving; nothing was written. Try again."
        except (answers.NeverStore, answers.AnswersRefused, ProfileError, ValueError) as exc:
            message = f"Not promoted: {exc}"
        else:
            return RedirectResponse(f"/packet/{packet_id}?answer=promoted#answers", 303)
        return render_packet(request, conn, p, status=409, answers_extra={"message": message})

    @app.post("/packet/{packet_id}/evidence", response_class=HTMLResponse)
    def packet_evidence(request: Request, conn: Conn, form: Form, packet_id: int):
        """A federal self-assessment statement: the resume lines that may be relevant. Never a
        level, never a model call (specs/017 "Checklists")."""
        p = require_packet(conn, packet_id)
        statement = " ".join(form.get("statement", "").split())[:1000]
        profile, _error = load_console_profile(request.app.state.settings)
        lines = documents.numbered_resume(profile.resume_text) if profile else {}
        evidence = {
            "statement": statement,
            "lines": checklists.relevant_lines(lines, statement) if statement else [],
        }
        return render_packet(request, conn, p, answers_extra={"evidence": evidence})

    def safe_next(target: str) -> str:
        ok = target.startswith("/") and not target.startswith("//") and "\\" not in target
        return target if ok else "/costs"

    @app.post("/apply/runner/clear-overage")
    def runner_clear_overage(request: Request, form: Form) -> Response:
        """The user says the subscription is back within its plan: lift the paid-usage hold.
        Its own action, never a side effect of Turn back on or of a later call."""
        runner_state.clear_overage(request.app.state.settings.paths.data_dir)
        return RedirectResponse(safe_next(form.get("next", "")), status_code=303)

    @app.post("/apply/runner/on")
    def runner_on(request: Request, form: Form) -> Response:
        """Turn back on: clears the CLI runner's off state. A paid-usage hold stays."""
        runner_state.turn_on(request.app.state.settings.paths.data_dir)
        cli_runner.reset_auth_cache()
        return RedirectResponse(safe_next(form.get("next", "")), status_code=303)

    @app.get("/packet/{packet_id}", response_class=HTMLResponse)
    def packet_page(
        request: Request,
        conn: Conn,
        packet_id: int,
        resume: int | None = None,
        letter: int | None = None,
        answer: str | None = None,
    ) -> HTMLResponse:
        p = require_packet(conn, packet_id)
        ok = {
            "saved": "Saved on this packet. Promote it to /prefs to offer it on every packet.",
            "promoted": "Promoted: it is now in Application answers on /prefs.",
        }.get(answer or "")
        return render_packet(
            request,
            conn,
            p,
            view={"resume": resume, "cover_letter": letter},
            answers_extra={"ok": ok},
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
        return render_packet(request, conn, p, score_message=message, score_ok=ok)
