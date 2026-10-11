"""Routes for /prefs (specs/014): page, live preview, save, revert, state picker cycle."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from jobhunter.apply import answers as ans
from jobhunter.console import prefs as pf
from jobhunter.console import prefs_answers as pa
from jobhunter.console import prefs_graceful as gr
from jobhunter.core import db
from jobhunter.scoring import buckets as bk
from jobhunter.scoring.profile import (
    PREFERENCES_FILE,
    Profile,
    ProfileConflict,
    ProfileError,
    ProfileValidationError,
    profile_data,
    save_profile_changes,
    validate_profile_data,
    write_preferences_data,
    write_preferences_text,
)

SECTIONS = [
    ("pay", "Pay"),
    ("where", "Where"),
    ("what", "What"),
    ("weights", "Weights"),
    ("buckets", "Buckets"),
    ("queries", "Search queries"),
    ("spend", "Spend"),
    ("recency", "Recency"),
    ("narrative", "Narrative"),
    ("resume", "Resume"),
    ("answers", "Application answers"),
]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=list)


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    templates.env.globals["signed"] = pf.signed
    templates.env.globals["bucket_title"] = pf.bucket_title
    templates.env.filters["pref_value"] = pf.display_value

    def profile_dir(request: Request) -> Path:
        return Path(request.app.state.settings.paths.profile_dir).expanduser()

    def mtime_of(request: Request) -> int:
        try:
            return (profile_dir(request) / PREFERENCES_FILE).stat().st_mtime_ns
        except OSError:
            return 0

    def state_of(request: Request, text: str | None = None) -> gr.PrefsState:
        return gr.lenient_state(request.app.state.settings, text)

    def stamp() -> str:
        return now().strftime("%Y%m%dT%H%M%S")

    def render_page(
        request: Request,
        conn: sqlite3.Connection,
        st: gr.PrefsState,
        *,
        view: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        conflict: bool = False,
        flash: str | None = None,
        mtime_ns: int | None = None,
        status: int = 200,
        raw_text: str | None = None,
        upload: dict[str, Any] | None = None,
        upload_error: str | None = None,
        answers_form: pa.AnswersForm | None = None,
        section_result: tuple[str, str] | None = None,
    ) -> HTMLResponse:
        settings = request.app.state.settings
        profile = st.profile
        view = view or pf.view_from_data(profile_data(profile))
        errors = dict(errors or {})
        if st.errors and not errors:
            errors = pf.errors_for_form(ProfileValidationError(st.errors))
        # Application answers: loaded on their own, never through Profile (specs/017).
        loaded = ans.load_answers(profile_dir(request))
        ans_view = answers_form.view if answers_form is not None else pa.view_of(loaded.answers)
        file_view = pf.view_from_data(profile_data(profile))
        ans_file_view = pa.view_of(loaded.answers)
        unsaved = pf.dirty_sections(
            view,
            file_view,
            answers_form is not None and _json(ans_view) != _json(ans_file_view),
        )
        known = set(view) | {"weights"} | set(ans_view)
        known |= {k for k in errors if k.startswith("ans.")}
        ctx = {
            "title": "Preferences",
            "active": "/prefs",
            "nav": nav,
            "v": view,
            "errors": errors,
            "other_errors": {k: m for k, m in errors.items() if k not in known},
            "conflict": conflict,
            "flash": flash,
            "mtime_ns": mtime_ns if mtime_ns is not None else mtime_of(request),
            "sections": SECTIONS,
            "picker": pf.picker(
                pf.state_codes(view["state_ranking"]), pf.state_codes(view["states_excluded"])
            ),
            "states_allowed": profile.hard.states_allowed,
            "periods": pf.PERIODS,
            "employment_choices": pf.EMPLOYMENT_CHOICES,
            "credential_choices": sorted(
                set(pf.CREDENTIAL_CHOICES) | set(profile.hard.requires_i_lack)
            ),
            "weight_keys": pf.WEIGHT_KEYS,
            "threshold_keys": list(bk.DEFAULT_THRESHOLDS),
            "threshold_groups": pf.threshold_groups(list(bk.DEFAULT_THRESHOLDS)),
            "defaults": bk.DEFAULT_THRESHOLDS,
            "bucket_help": pf.BUCKET_HELP,
            "bucket_rules": pf.BUCKET_RULES,
            "bucket_intro": pf.BUCKET_INTRO,
            "bucket_note": pf.BUCKET_NOTE,
            "spend": settings.scoring,
            "resume": pf.resume_info(conn, profile),
            "history": pf.history(conn),
            "rescore_panel": request.app.state.rescore_panel_context(request, conn),
            "source_file": str(st.prefs_file),
            "st": st,
            "raw_text": raw_text if raw_text is not None else (st.raw_text or ""),
            "upload": upload,
            "upload_error": upload_error,
            "max_upload_mb": gr.MAX_RESUME_BYTES // (1024 * 1024),
            "ansv": ans_view,
            "ans_warnings": loaded.warnings,
            "ans_link_fields": ans.LINK_FIELDS,
            "ans_text_fields": ans.TEXT_FIELDS,
            "ans_work_auth_label": ans.WORK_AUTH_LABEL,
            "ans_work_auth_choices": pa.WORK_AUTH_CHOICES,
            "unsaved_sections": unsaved,
            "sec_for": section_result[0] if section_result else None,
            "sec_html": section_result[1] if section_result else "",
        }
        return templates.TemplateResponse(request, "prefs.html", ctx, status_code=status)

    def submitted(form: pf.Form, profile: Profile) -> tuple[Profile | None, dict, dict]:
        """(after profile or None, changes, errors keyed by form field)."""
        before = profile_data(profile)
        after_data, errors = pf.form_to_data(form, before)
        if errors:
            return None, {}, errors
        try:
            after = validate_profile_data(after_data, base=profile)
        except ProfileValidationError as exc:
            return None, {}, pf.errors_for_form(exc)
        return after, pf.diff(before, profile_data(after)), {}

    @app.get("/prefs", response_class=HTMLResponse)
    def prefs_page(
        request: Request,
        conn: Conn,
        saved: int | None = None,
        rescore: str | None = None,
        reverted: int | None = None,
        created: int | None = None,
        repaired: int | None = None,
    ) -> HTMLResponse:
        st = state_of(request)
        if st.kind in ("ok", "resume"):
            pf.detect_file_edits(conn, st.profile, now())
        flash = None
        if created is not None:
            flash = "Created profile/preferences.yaml. Now upload a resume if you haven't."
        elif repaired is not None:
            flash = "Saved. The previous file was kept next to it as a timestamped .bak."
        elif saved is not None:
            flash = "No changes to save." if saved == 0 else f"Saved {saved} change(s)."
            if rescore in ("recent", "all"):
                flash += " Re-score queued; run it from Re-score now below."
        elif reverted is not None:
            flash = f"Reverted change #{reverted}."
        return render_page(request, conn, st, flash=flash)

    @app.post("/prefs/preview", response_class=HTMLResponse)
    async def preview(request: Request, conn: Conn) -> HTMLResponse:
        form = pf.parse_form(await request.body())
        # The bucket and prefilter counts are synchronous DB and CPU work: off the event loop,
        # so a state click (or any page) is not queued behind a running preview (501fee8).
        return await run_in_threadpool(preview_work, request, conn, form)

    def preview_work(request: Request, conn: sqlite3.Connection, form: pf.Form) -> HTMLResponse:
        profile = state_of(request).profile
        ctx: dict[str, Any] = {"errors": {}, "changes": {}, "conflict": False}
        ctx["conflict"] = pf.form_value(form, "mtime_ns") != str(mtime_of(request))
        after, changes, errors = submitted(form, profile)
        af = pa.parse(form)
        if af.present:
            # Answers are free and score nothing: the preview only counts them.
            errors = {**errors, **af.errors}
            if af.answers is not None:
                old = ans.load_answers(profile_dir(request)).answers
                ctx["answer_changes"] = len(ans.answer_writes(af.answers, old))
        ctx.update(errors=errors, changes=changes)
        if after is None or not changes:
            return templates.TemplateResponse(request, "_prefs_preview.html", ctx)
        free = {p: c for p, c in changes.items() if not pf.is_paid(p)}
        paid = {p: c for p, c in changes.items() if pf.is_paid(p)}
        ctx.update(free=free, paid=paid, days=pf.PREVIEW_DAYS)
        if free:
            result = bk.preview(conn, profile, after, days=pf.PREVIEW_DAYS)
            ctx["buckets"] = result
            ctx["bucket_lines"] = [
                (b, result["before"][b], result["after"][b], result["delta"][b])
                for b in result["before"]
                if result["delta"][b]
            ]
            ctx["into_ab"] = [m for m in result["moves"] if pf.moves_up_into_ab(m)]
            ctx["prefilter"] = pf.prefilter_delta(
                conn, profile, after, days=pf.PREVIEW_DAYS, now=now()
            )
        if paid:
            ctx["estimate"] = pf.rescore_estimate(conn, after, now())
            choice = pf.form_value(form, "rescore", "none")
            ctx["rescore"] = choice if choice in pf.RESCORE_SCOPES else "none"
        return templates.TemplateResponse(request, "_prefs_preview.html", ctx)

    def save_whole_file(
        request: Request, conn: sqlite3.Connection, st: gr.PrefsState, form: pf.Form
    ) -> Response:
        """Create the file (none yet) or rewrite an unreadable one, keeping a timestamped .bak."""
        view = pf.view_from_form(form, pf.view_from_data(profile_data(st.profile)))
        try:
            expected = int(pf.form_value(form, "mtime_ns"))
        except ValueError:
            expected = -1
        data, errors = pf.form_to_data(form, profile_data(st.profile))
        if not errors:
            try:
                validate_profile_data(data, base=st.profile)
            except ProfileValidationError as exc:
                errors = pf.errors_for_form(exc)
        af = pa.parse(form)
        errors = {**errors, **af.errors}
        if errors:
            return render_page(
                request,
                conn,
                st,
                view=view,
                errors=errors,
                mtime_ns=expected,
                status=422,
                answers_form=af,
            )
        if mtime_of(request) != expected:
            return render_page(
                request,
                conn,
                st,
                view=view,
                conflict=True,
                mtime_ns=expected,
                status=409,
                answers_form=af,
            )
        separate = None
        if af.answers is not None:
            separate = {"answers": af.answers.model_dump(mode="json")}
        write_preferences_data(profile_dir(request), data, backup_stamp=stamp(), separate=separate)
        after = state_of(request)
        pf.save_snapshot(conn, after.profile, now())
        conn.commit()
        return RedirectResponse(
            "/prefs?created=1" if st.kind == "missing" else "/prefs?repaired=1", status_code=303
        )

    @app.post("/prefs/save", response_class=HTMLResponse)
    async def save(request: Request, conn: Conn) -> Response:
        form = pf.parse_form(await request.body())
        return await run_in_threadpool(save_work, request, conn, form)

    def save_work(request: Request, conn: sqlite3.Connection, form: pf.Form) -> Response:
        st = state_of(request)
        if st.writes_whole_file:
            return save_whole_file(request, conn, st, form)
        profile = st.profile
        pf.detect_file_edits(conn, profile, now())
        fallback = pf.view_from_data(profile_data(profile))
        view = pf.view_from_form(form, fallback)
        try:
            expected = int(pf.form_value(form, "mtime_ns"))
        except ValueError:
            expected = -1
        _, changes, errors = submitted(form, profile)
        # Application answers: validated by their own model (never-store first); a refusal
        # writes nothing at all, profile changes included.
        af = pa.parse(form)
        errors = {**errors, **af.errors}
        if errors:
            return render_page(
                request,
                conn,
                st,
                view=view,
                errors=errors,
                mtime_ns=expected,
                status=422,
                answers_form=af,
            )
        answer_writes: dict[str, Any] = {}
        if af.answers is not None:
            loaded = ans.load_answers(profile_dir(request))
            # Entries the lenient load ignored stay in the file (950d5eb (5)).
            answer_writes = ans.answer_writes(af.answers, loaded.answers, loaded.ignored_custom)
            if loaded.custom_unmanaged and "answers.custom" in answer_writes:
                # Writing the form's list would delete the hand-written entries (950d5eb (5)).
                return render_page(
                    request,
                    conn,
                    st,
                    errors={
                        "answers": "answers.custom in preferences.yaml is not a list, so "
                        "custom answers were not saved; fix it in the raw editor first."
                    },
                    status=422,
                    view=view,
                    mtime_ns=expected,
                    answers_form=af,
                )
        if not changes and not answer_writes:
            return RedirectResponse("/prefs?saved=0", status_code=303)
        rerender = {"view": view, "mtime_ns": expected, "answers_form": af}
        try:
            new = pf.apply_changes(
                conn,
                profile_dir(request),
                changes,
                expected_mtime_ns=expected,
                source="ui",
                now=now(),
                require_resume=st.kind == "ok",
                extra_writes=answer_writes,
            )
        except ProfileConflict:
            return render_page(request, conn, st, conflict=True, status=409, **rerender)
        except ProfileValidationError as exc:
            return render_page(
                request, conn, st, errors=pf.errors_for_form(exc), status=422, **rerender
            )
        except ProfileError as exc:
            return render_page(
                request, conn, st, errors={"profile": str(exc)}, status=422, **rerender
            )
        scope = pf.form_value(form, "rescore", "none")
        queued = ""
        if any(pf.is_paid(p) for p in changes) and pf.record_rescore(conn, scope, new, now()):
            queued = f"&rescore={scope}"
        saved = len(changes) + len(answer_writes)
        return RedirectResponse(f"/prefs?saved={saved}{queued}", status_code=303)

    @app.post("/prefs/section/{section}", response_class=HTMLResponse)
    async def save_section(request: Request, conn: Conn, section: str) -> Response:
        """Save one section's fields only, over the file as it is now (b25ec96)."""
        if section not in pf.SAVEABLE_SECTIONS:
            raise HTTPException(404, "no such section")
        form = pf.parse_form(await request.body())
        return await run_in_threadpool(save_section_work, request, conn, section, form)

    def section_response(
        request: Request,
        conn: sqlite3.Connection,
        section: str,
        result: dict[str, Any],
        *,
        status: int = 200,
        af: pa.AnswersForm | None = None,
        mtime_ns: int | None = None,
        saved_now: bool = False,
    ) -> Response:
        """The status block for ``section`` (swapped in place); on a save, also the new
        mtime, the re-score panel and the events that re-baseline the page."""
        result["section"] = section
        result["mtime_ns"] = mtime_of(request) if saved_now else None
        if saved_now:
            result["rescore_panel"] = request.app.state.rescore_panel_context(request, conn)
        tpl = templates.get_template("_prefs_section_result.html")
        main = tpl.render(request=request, part="main", **result)
        oob = tpl.render(request=request, part="oob", **result)
        if section == pf.ANSWERS_SECTION:
            # The answers section is re-rendered whole: its rows (a saved custom answer
            # becomes a row of its own) and its inline errors come from the server.
            st = state_of(request)
            page = render_page(
                request,
                conn,
                st,
                view=pf.view_from_data(profile_data(st.profile)),
                errors={k: m for k, m in result.get("errors", {}).items() if k.startswith("ans.")},
                mtime_ns=mtime_ns,
                answers_form=None if saved_now else af,
                section_result=(section, main),
            )
            body = page.body.decode()
            start = body.index('<section class="panel" id="sec-answers"')
            end = body.index("</section>", start) + len("</section>")
            response = HTMLResponse(body[start:end] + oob, status_code=status)
            response.headers["HX-Retarget"] = "#sec-answers"
            response.headers["HX-Reswap"] = "outerHTML"
        else:
            response = HTMLResponse(main + oob, status_code=status)
        if saved_now:
            response.headers["HX-Trigger-After-Swap"] = json.dumps(
                {"prefs-changed": "", "prefs-saved": {"section": section}}
            )
        return response

    def save_section_work(
        request: Request, conn: sqlite3.Connection, section: str, form: pf.Form
    ) -> Response:
        st = state_of(request)
        if st.writes_whole_file:
            return section_response(
                request,
                conn,
                section,
                {"kind": "whole", "errors": {}},
                status=409,
            )
        profile = st.profile
        pf.detect_file_edits(conn, profile, now())
        try:
            expected = int(pf.form_value(form, "mtime_ns"))
        except ValueError:
            expected = -1
        af = pa.parse(form) if section == pf.ANSWERS_SECTION else None
        before = profile_data(profile)
        after: Profile | None = None
        changes: dict[str, Any] = {}
        answer_writes: dict[str, Any] = {}
        errors: dict[str, str] = {}
        if af is None:
            data, errors = pf.section_to_data(section, form, before)
            if not errors:
                try:
                    after = validate_profile_data(data, base=profile)
                    changes = pf.diff(before, profile_data(after))
                except ProfileValidationError as exc:
                    errors = pf.errors_for_form(exc)
        else:
            errors = dict(af.errors)
            if not af.present:
                errors["answers"] = "The answers fields were not sent."
            elif af.answers is not None and not errors:
                loaded = ans.load_answers(profile_dir(request))
                answer_writes = ans.answer_writes(af.answers, loaded.answers, loaded.ignored_custom)
                if loaded.custom_unmanaged and "answers.custom" in answer_writes:
                    errors["answers"] = (
                        "answers.custom in preferences.yaml is not a list, so custom answers "
                        "were not saved; fix it in the raw editor first."
                    )
        fail = {"errors": errors}
        if errors:
            return section_response(
                request,
                conn,
                section,
                {"kind": "errors", **fail},
                status=422,
                af=af,
                mtime_ns=expected,
            )
        if mtime_of(request) != expected:
            return section_response(
                request,
                conn,
                section,
                {"kind": "conflict", **fail},
                status=409,
                af=af,
                mtime_ns=expected,
            )
        if not changes and not answer_writes:
            return section_response(
                request, conn, section, {"kind": "nochange", **fail}, af=af, mtime_ns=expected
            )
        paid = any(pf.is_paid(p) for p in changes)
        if paid and pf.form_value(form, "confirm") != "1":
            # The cost and re-score choice come before the write, as with the full Save.
            estimate = pf.rescore_estimate(conn, after or profile, now())
            return section_response(
                request,
                conn,
                section,
                {"kind": "confirm", "estimate": estimate, "changes": len(changes), **fail},
                af=af,
                mtime_ns=expected,
            )
        try:
            new = pf.apply_changes(
                conn,
                profile_dir(request),
                changes,
                expected_mtime_ns=expected,
                source="ui",
                now=now(),
                require_resume=st.kind == "ok",
                extra_writes=answer_writes,
            )
        except ProfileConflict:
            return section_response(
                request,
                conn,
                section,
                {"kind": "conflict", **fail},
                status=409,
                af=af,
                mtime_ns=expected,
            )
        except ProfileValidationError as exc:
            return section_response(
                request,
                conn,
                section,
                {"kind": "errors", "errors": pf.errors_for_form(exc)},
                status=422,
                af=af,
                mtime_ns=expected,
            )
        except ProfileError as exc:
            return section_response(
                request,
                conn,
                section,
                {"kind": "errors", "errors": {"profile": str(exc)}},
                status=422,
                af=af,
                mtime_ns=expected,
            )
        scope = pf.form_value(form, "section_rescore", "none")
        queued = paid and pf.record_rescore(conn, scope, new, now())
        return section_response(
            request,
            conn,
            section,
            {
                "kind": "saved",
                "count": len(changes) + len(answer_writes),
                "queued": scope if queued else "",
                "errors": {},
            },
            af=af,
            saved_now=True,
            mtime_ns=mtime_of(request),
        )

    @app.post("/prefs/raw", response_class=HTMLResponse)
    async def save_raw(request: Request, conn: Conn) -> Response:
        """Advanced: replace a broken preferences.yaml with hand-typed YAML (old one kept)."""
        form = pf.parse_form(await request.body())
        return await run_in_threadpool(save_raw_work, request, conn, form)

    def save_raw_work(request: Request, conn: sqlite3.Connection, form: pf.Form) -> Response:
        text = pf.form_value(form, "raw_yaml").replace("\r\n", "\n")
        try:
            expected = int(pf.form_value(form, "mtime_ns"))
        except ValueError:
            expected = -1
        st = state_of(request, text)
        if mtime_of(request) != expected:
            return render_page(
                request, conn, st, conflict=True, mtime_ns=expected, status=409, raw_text=text
            )
        if st.kind != "ok":
            return render_page(request, conn, st, mtime_ns=expected, status=422, raw_text=text)
        refused = ans.refused_in_text(text)
        if refused:
            # The raw editor is a writer of answers: too: a never-store entry is refused here,
            # not just dropped on the next load (specs/017 "Never-store list").
            # Rendered over the file on disk (still broken), so the raw editor keeps the text.
            return render_page(
                request,
                conn,
                state_of(request),
                mtime_ns=expected,
                status=422,
                raw_text=text,
                errors={"answers": " ".join(refused)},
            )
        write_preferences_text(profile_dir(request), text, backup_stamp=stamp())
        pf.save_snapshot(conn, state_of(request).profile, now())
        conn.commit()
        return RedirectResponse("/prefs?repaired=1", status_code=303)

    @app.post("/prefs/resume", response_class=HTMLResponse)
    async def upload_resume(
        request: Request, conn: Conn, file: Annotated[UploadFile | None, File()] = None
    ) -> Response:
        name = file.filename if file is not None else None
        data = await file.read(gr.MAX_RESUME_BYTES + 1) if file is not None and name else b""
        return await run_in_threadpool(upload_resume_work, request, conn, name, data)

    def upload_resume_work(
        request: Request, conn: sqlite3.Connection, filename: str | None, data: bytes
    ) -> Response:
        before = state_of(request)
        if not filename:
            return render_page(
                request, conn, before, upload_error="Choose a .md or .txt file first.", status=422
            )
        try:
            text = gr.check_upload(filename, data)
        except gr.UploadError as exc:
            return render_page(request, conn, before, upload_error=str(exc), status=exc.status)
        valid_before = before.kind in ("ok", "resume")
        if valid_before:
            pf.detect_file_edits(conn, before.profile, now())
        old_hash = gr.sha256_text(before.profile.resume_text)
        target = gr.resume_target(request.app.state.settings, before.profile)
        dest = gr.store_resume(target, filename, text, now())
        after = state_of(request)
        if after.kind == "resume" and not after.profile.resume_path:
            # The loader still can't find it: point preferences.yaml at the new location.
            save_profile_changes(
                profile_dir(request),
                {"resume_path": str(target)},
                expected_mtime_ns=mtime_of(request),
                require_resume=False,
            )
            after = state_of(request)
        new_hash = gr.sha256_text(text)
        if valid_before and after.kind == "ok" and new_hash != old_hash:
            with db.transaction(conn):
                pf.record_changes(
                    conn, {pf.RESUME_PATH: (old_hash, new_hash)}, after.profile, "ui", now()
                )
                pf.save_snapshot(conn, after.profile, now())
        result = {
            "name": dest.name,
            "path": str(dest),
            "sha256": new_hash,
            "estimate": pf.rescore_estimate(conn, after.profile, now()),
        }
        return render_page(request, conn, after, upload=result)

    @app.post("/prefs/revert/{change_id}", response_class=HTMLResponse)
    def revert(request: Request, conn: Conn, change_id: int) -> Response:
        try:
            pf.revert_change(conn, profile_dir(request), change_id, now())
        except KeyError as exc:
            raise HTTPException(404, "no such change") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ProfileValidationError as exc:
            return render_page(
                request, conn, state_of(request), errors=pf.errors_for_form(exc), status=422
            )
        except ProfileError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/prefs?reverted={change_id}", status_code=303)

    @app.post("/prefs/state/{code}", response_class=HTMLResponse)
    async def cycle_state(request: Request, code: str) -> HTMLResponse:
        code = code.upper()
        if code not in pf.PICKER_TILES:
            raise HTTPException(404, "not a state on the picker")
        form = pf.parse_form(await request.body())
        ranking = pf.state_codes(pf.form_value(form, "state_ranking"))
        excluded = [
            c for c in pf.state_codes(pf.form_value(form, "states_excluded")) if c not in ranking
        ]
        ranking, excluded = pf.cycle_state(ranking, excluded, code)
        response = templates.TemplateResponse(
            request, "_prefs_states.html", {"picker": pf.picker(ranking, excluded)}
        )
        response.headers["HX-Trigger"] = "prefs-changed"
        return response
