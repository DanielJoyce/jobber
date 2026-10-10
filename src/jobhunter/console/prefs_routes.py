"""Routes for /prefs (specs/014): page, live preview, save, revert, state picker cycle."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.console import prefs as pf
from jobhunter.console import prefs_graceful as gr
from jobhunter.core import db, geo
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
]


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
    ) -> HTMLResponse:
        settings = request.app.state.settings
        profile = st.profile
        view = view or pf.view_from_data(profile_data(profile))
        errors = dict(errors or {})
        if st.errors and not errors:
            errors = pf.errors_for_form(ProfileValidationError(st.errors))
        known = set(view) | {"weights"}
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
        profile = state_of(request).profile
        ctx: dict[str, Any] = {"errors": {}, "changes": {}, "conflict": False}
        ctx["conflict"] = pf.form_value(form, "mtime_ns") != str(mtime_of(request))
        after, changes, errors = submitted(form, profile)
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
        if errors:
            return render_page(
                request, conn, st, view=view, errors=errors, mtime_ns=expected, status=422
            )
        if mtime_of(request) != expected:
            return render_page(
                request, conn, st, view=view, conflict=True, mtime_ns=expected, status=409
            )
        write_preferences_data(profile_dir(request), data, backup_stamp=stamp())
        after = state_of(request)
        pf.save_snapshot(conn, after.profile, now())
        conn.commit()
        return RedirectResponse(
            "/prefs?created=1" if st.kind == "missing" else "/prefs?repaired=1", status_code=303
        )

    @app.post("/prefs/save", response_class=HTMLResponse)
    async def save(request: Request, conn: Conn) -> Response:
        form = pf.parse_form(await request.body())
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
        if errors:
            return render_page(
                request, conn, st, view=view, errors=errors, mtime_ns=expected, status=422
            )
        if not changes:
            return RedirectResponse("/prefs?saved=0", status_code=303)
        try:
            new = pf.apply_changes(
                conn,
                profile_dir(request),
                changes,
                expected_mtime_ns=expected,
                source="ui",
                now=now(),
                require_resume=st.kind == "ok",
            )
        except ProfileConflict:
            return render_page(
                request, conn, st, view=view, conflict=True, mtime_ns=expected, status=409
            )
        except ProfileValidationError as exc:
            return render_page(
                request,
                conn,
                st,
                view=view,
                errors=pf.errors_for_form(exc),
                mtime_ns=expected,
                status=422,
            )
        except ProfileError as exc:
            return render_page(
                request,
                conn,
                st,
                view=view,
                errors={"profile": str(exc)},
                mtime_ns=expected,
                status=422,
            )
        scope = pf.form_value(form, "rescore", "none")
        queued = ""
        if any(pf.is_paid(p) for p in changes) and pf.record_rescore(conn, scope, new, now()):
            queued = f"&rescore={scope}"
        return RedirectResponse(f"/prefs?saved={len(changes)}{queued}", status_code=303)

    @app.post("/prefs/raw", response_class=HTMLResponse)
    async def save_raw(request: Request, conn: Conn) -> Response:
        """Advanced: replace a broken preferences.yaml with hand-typed YAML (old one kept)."""
        form = pf.parse_form(await request.body())
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
        write_preferences_text(profile_dir(request), text, backup_stamp=stamp())
        pf.save_snapshot(conn, state_of(request).profile, now())
        conn.commit()
        return RedirectResponse("/prefs?repaired=1", status_code=303)

    @app.post("/prefs/resume", response_class=HTMLResponse)
    async def upload_resume(
        request: Request, conn: Conn, file: Annotated[UploadFile | None, File()] = None
    ) -> Response:
        before = state_of(request)
        if file is None or not file.filename:
            return render_page(
                request, conn, before, upload_error="Choose a .md or .txt file first.", status=422
            )
        data = await file.read(gr.MAX_RESUME_BYTES + 1)
        try:
            text = gr.check_upload(file.filename, data)
        except gr.UploadError as exc:
            return render_page(request, conn, before, upload_error=str(exc), status=exc.status)
        valid_before = before.kind in ("ok", "resume")
        if valid_before:
            pf.detect_file_edits(conn, before.profile, now())
        old_hash = gr.sha256_text(before.profile.resume_text)
        target = gr.resume_target(request.app.state.settings, before.profile)
        dest = gr.store_resume(target, file.filename, text, now())
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
        if code not in geo.TILE_GRID:
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
