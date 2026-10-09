"""Routes for /prefs (specs/014): page, live preview, save, revert, state picker cycle."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.console import prefs as pf
from jobhunter.core import geo
from jobhunter.scoring import buckets as bk
from jobhunter.scoring.profile import (
    Profile,
    ProfileConflict,
    ProfileError,
    ProfileValidationError,
    load_profile,
    preferences_mtime_ns,
    profile_data,
    validate_profile_data,
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
    templates.env.filters["pref_value"] = pf.display_value

    def profile_dir(request: Request) -> Path:
        return Path(request.app.state.settings.paths.profile_dir).expanduser()

    def render_page(
        request: Request,
        conn: sqlite3.Connection,
        profile: Profile,
        *,
        view: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        conflict: bool = False,
        flash: str | None = None,
        mtime_ns: int | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        settings = request.app.state.settings
        view = view or pf.view_from_data(profile_data(profile))
        errors = errors or {}
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
            "mtime_ns": mtime_ns
            if mtime_ns is not None
            else preferences_mtime_ns(profile_dir(request)),
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
            "defaults": bk.DEFAULT_THRESHOLDS,
            "spend": settings.scoring,
            "resume": pf.resume_info(conn, profile),
            "history": pf.history(conn),
            "pending": pf.pending_rescores(conn),
            "source_file": str(profile.source_file) if profile.source_file else "",
        }
        return templates.TemplateResponse(request, "prefs.html", ctx, status_code=status)

    def load_or_error(request: Request) -> tuple[Profile | None, HTMLResponse | None]:
        try:
            return load_profile(profile_dir(request)), None
        except ProfileError as exc:
            page = templates.TemplateResponse(
                request,
                "prefs_error.html",
                {"title": "Preferences", "active": "/prefs", "nav": nav, "error": str(exc)},
                status_code=200 if request.method == "GET" else 422,
            )
            return None, page

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
    ) -> HTMLResponse:
        profile, error_page = load_or_error(request)
        if profile is None:
            assert error_page is not None
            return error_page
        pf.detect_file_edits(conn, profile, now())
        flash = None
        if saved is not None:
            flash = "No changes to save." if saved == 0 else f"Saved {saved} change(s)."
            if rescore in ("recent", "all"):
                flash += " Re-score queued for the next scoring run."
        elif reverted is not None:
            flash = f"Reverted change #{reverted}."
        return render_page(request, conn, profile, flash=flash)

    @app.post("/prefs/preview", response_class=HTMLResponse)
    async def preview(request: Request, conn: Conn) -> HTMLResponse:
        form = pf.parse_form(await request.body())
        profile, _ = load_or_error(request)
        ctx: dict[str, Any] = {"errors": {}, "changes": {}, "conflict": False}
        if profile is None:
            ctx["errors"] = {"profile": "the profile file could not be loaded"}
            return templates.TemplateResponse(request, "_prefs_preview.html", ctx)
        ctx["conflict"] = pf.form_value(form, "mtime_ns") != str(
            preferences_mtime_ns(profile_dir(request))
        )
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

    @app.post("/prefs/save", response_class=HTMLResponse)
    async def save(request: Request, conn: Conn) -> Response:
        form = pf.parse_form(await request.body())
        profile, error_page = load_or_error(request)
        if profile is None:
            assert error_page is not None
            return error_page
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
                request, conn, profile, view=view, errors=errors, mtime_ns=expected, status=422
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
            )
        except ProfileConflict:
            return render_page(
                request, conn, profile, view=view, conflict=True, mtime_ns=expected, status=409
            )
        except ProfileValidationError as exc:
            return render_page(
                request,
                conn,
                profile,
                view=view,
                errors=pf.errors_for_form(exc),
                mtime_ns=expected,
                status=422,
            )
        except ProfileError as exc:
            return render_page(
                request,
                conn,
                profile,
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

    @app.post("/prefs/revert/{change_id}", response_class=HTMLResponse)
    def revert(request: Request, conn: Conn, change_id: int) -> Response:
        try:
            pf.revert_change(conn, profile_dir(request), change_id, now())
        except KeyError as exc:
            raise HTTPException(404, "no such change") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ProfileValidationError as exc:
            profile, error_page = load_or_error(request)
            if profile is None:
                assert error_page is not None
                return error_page
            return render_page(request, conn, profile, errors=pf.errors_for_form(exc), status=422)
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
