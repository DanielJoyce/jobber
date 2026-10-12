"""Routes for /setup (specs/019 "/setup", "Writes from the page", "Email alerts, step by step").

``GET /setup`` renders ``readiness.run_checks`` grouped by section. Three writes, none of
which takes a secret: ``POST /setup/starter-prefs``, ``POST /setup/resume`` and
``POST /setup/dismiss``. The live checks (S6) and the help
links (S11e) are other tickets. No check value is ever rendered here: the rows are the fixed
wording ``readiness`` builds.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local annotations.
import contextlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from jobhunter import readiness as rd
from jobhunter.config import Settings, resolve_path
from jobhunter.console import help as helpdocs
from jobhunter.console import prefs_graceful as gr
from jobhunter.console.prefs_routes import _WRITE_LOCK
from jobhunter.mail import auth
from jobhunter.scoring.profile import (
    PREFERENCES_FILE,
    ProfileError,
    _atomic_private_write,
    save_profile_changes,
    write_preferences_data,
)
from jobhunter.xdg import mkdir_private

DISMISSED_FILE = "setup-dismissed.json"
# Rows only the CLI shows (019 "/setup": rows 1-3 are doctor-only).
DOCTOR_ONLY = frozenset({"config.file", "db.schema"})
# The live "Test (free)" button is S6; until it lands the row points at the command only.
TEST_BUTTON_TEXT = re.compile(r"use Test \(free\),? or (?:run )?(`[^`]+`)")
CLIENT_SECRET = "jobhunter_google_client"
GMAIL_HELP = "/help/email-alerts#step-1-google-client-json"

# A step is blocked by the earlier steps it needs: `mail auth` needs the client JSON, and
# `mail setup` refuses the placeholder address and needs the token (019 "Email alerts").
STEP_NEEDS = {3: (1,), 4: (2, 3), 5: (2,), 6: (2,), 7: (2,)}

COMMAND_PREFIXES = (
    "[ -e", "read -rs", "jobhunter", "systemctl", "podman", "chmod", "install -m", "uv ",
)  # fmt: skip

STATUS_ICON = {
    rd.OK: "✓",
    rd.MISSING: "✕",
    rd.BROKEN: "✕",
    rd.OPTIONAL_STATUS: "○",
    rd.UNKNOWN: "?",
}


@dataclass
class Row:
    check: rd.Check
    icon: str
    word: str
    quiet: bool  # collapses to one line
    waiting_on: int = 0
    restart: str = ""  # the restart note, for rows read at process start
    cloud_steps: list[str] = field(default_factory=list)
    help: str = ""  # "/help/<topic>#<anchor>", the row's help link (S11e)
    help_title: str = ""  # the topic's title, for the link text
    dismissible: bool = False
    upload_error: str = ""
    hidden: bool = False
    detail: str = ""


@dataclass
class Group:
    name: str
    rows: list[Row]

    @property
    def quiet(self) -> bool:
        return all(r.quiet for r in self.rows)


def dismissed_path(settings: Settings) -> Path:
    return resolve_path(settings.paths.data_dir) / DISMISSED_FILE


def can_dismiss(check: rd.Check) -> bool:
    """Recommended rows, and any non-essential row that is ok but due; never an essential."""
    if check.level == rd.ESSENTIAL:
        return False
    return check.level == rd.RECOMMENDED or check.due


def cloud_steps(settings: Settings, mode: str, home: Path | None) -> list[str]:
    """Steps 1-4 are ``mail/auth.py`` ``CLOUD_STEPS`` verbatim; step 5 and the last line name
    the mode's target (the host path, or a podman secret in containers)."""
    head = auth.CLOUD_STEPS.partition("  5. Save it")[0]
    steps: list[str] = []
    for raw in head.splitlines()[1:]:  # drop the "To create..." intro line
        ln = raw.strip()
        if len(ln) > 2 and ln[0].isdigit() and ln[1] == ".":
            steps.append(ln)
        elif steps and ln:
            steps[-1] += " " + ln
    target = rd.display_path(auth.client_secrets_path(settings), mode, home)
    if mode == rd.HOST:
        steps.append(
            f"5. Save it as {target} (or point {auth.SECRETS_ENV_VAR} at it, or set "
            "mail.client_secrets_path in config.toml)."
        )
        steps.append("Then run: jobhunter mail auth")
    else:
        steps.append(
            "5. Keep the downloaded file somewhere private, then run: "
            f"podman secret create {CLIENT_SECRET} <file>   "
            "(then jobhunter container preflight and a console restart)."
        )
        steps.append("Then run: jobhunter-ctr mail auth")
    return steps


def help_title(path: str) -> str:
    topic = path.partition("#")[0]
    entry = helpdocs.TOPIC_BY_SLUG.get(topic)
    return entry.title if entry else ""


def build_groups(
    checks: list[rd.Check],
    settings: Settings,
    mode: str,
    home: Path | None,
    *,
    errors: dict[str, str] | None = None,
    dismissed: dict[str, Any] | None = None,
) -> list[Group]:
    by_step = {c.step: c for c in checks if c.step}
    rows_by_group: dict[str, list[Row]] = {g: [] for g in rd.GROUPS}
    for c in checks:
        if c.id in DOCTOR_ONLY and not (c.id == "config.file" and c.status == rd.BROKEN):
            continue  # a broken config.toml shows: a running console can see one (019 B)
        failing = c.status in rd.FAILING
        quiet = (c.status in (rd.OK, rd.OPTIONAL_STATUS)) and not c.due
        due_ok = c.due and c.status == rd.OK
        effective = "due" if c.due else c.status
        # A hidden row stays hidden only while its status is the one that was hidden.
        hidden = (
            not quiet
            and can_dismiss(c)
            and isinstance((dismissed or {}).get(c.id), dict)
            and dismissed[c.id].get("status") == effective
        )
        word = "due soon" if due_ok else c.status
        row = Row(
            check=c,
            icon="!" if due_ok else STATUS_ICON.get(c.status, "?"),
            word=f"hidden ({word})" if hidden else word,
            quiet=quiet or hidden,
            restart=(
                rd.RESTART_COMMAND[mode].replace("`", "")
                if c.reads_at_start and not quiet and not hidden
                else ""
            ),
            dismissible=can_dismiss(c) and (failing or c.due) and not hidden,
            upload_error=(errors or {}).get(c.id, ""),
            hidden=hidden,
            detail=TEST_BUTTON_TEXT.sub(r"run \1", c.detail).replace("`", ""),
            help="/help/" + rd.help_for(c.id, mode),
            help_title=help_title(rd.help_for(c.id, mode)),
        )
        if c.group == rd.EMAIL and c.step > 1 and not quiet:
            for need in STEP_NEEDS.get(c.step, ()):
                dep = by_step.get(need)
                if dep is not None and dep.status in rd.FAILING:
                    row.waiting_on = need
                    break
        if c.id == "mail.client" and failing:
            row.cloud_steps = cloud_steps(settings, mode, home)
        rows_by_group.setdefault(c.group, []).append(row)
    return [Group(g, rows_by_group[g]) for g in rd.GROUPS if rows_by_group.get(g)]


def read_dismissed(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("dismissed") if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}


def banner_view(checks: list[rd.Check], dismissed: dict[str, Any]) -> dict[str, Any] | None:
    """What the banner shows: failing essentials (never hideable) and, only when none fail,
    the recommended rows that are missing, broken or due and not hidden at that status.
    Titles only; nothing here is a value."""
    essentials = [c for c in checks if c.level == rd.ESSENTIAL and c.status in rd.FAILING]
    recommended = []
    for c in checks:
        if c.level != rd.RECOMMENDED or not (c.status in rd.FAILING or c.due):
            continue
        effective = "due" if c.due else c.status
        entry = dismissed.get(c.id)
        if isinstance(entry, dict) and entry.get("status") == effective:
            continue
        recommended.append(c)
    if essentials:
        return {"essentials": essentials, "recommended": [], "ids": []}
    if recommended:
        return {"essentials": [], "recommended": recommended, "ids": [c.id for c in recommended]}
    return None


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    started = now()
    templates.env.globals["COMMAND_PREFIXES"] = COMMAND_PREFIXES

    def settings_of(request: Request) -> Settings:
        return request.app.state.settings

    def mode_of(request: Request) -> str:
        override = getattr(request.app.state, "setup_mode", None)
        return override or rd.detect_mode(settings=settings_of(request))

    def home_of(request: Request) -> Path | None:
        return getattr(request.app.state, "setup_home", None)

    def compute(request: Request) -> list[rd.Check]:
        return rd.run_checks(
            settings_of(request),
            secrets_report=getattr(request.app.state, "secrets_report", None),
            now=now(),
            mode=mode_of(request),
            registry_rows=getattr(request.app.state, "registry_rows", None),
            environ=getattr(request.app.state, "setup_environ", None),
            home=home_of(request),
        )

    def config_row(request: Request) -> rd.Check | None:
        """Row 1 as broken while the watched config.toml does not load; None otherwise."""
        from jobhunter.console import config_routes

        state = config_routes.current_state(request)
        if not state.errors:
            return None
        n = len(state.errors)
        return rd.Check(
            "config.file", rd.BASICS, rd.ESSENTIAL, rd.BROKEN, False, "Settings file",
            f"config.toml has {n} error{'' if n == 1 else 's'}; the console keeps the "
            "settings it has. Check it with `jobhunter doctor`.",
            "Every command and the console",
            rd.Fix("route", "/config/changes"), "config.toml", reads_at_start=True,
        )  # fmt: skip

    def view_of(request: Request, errors: dict[str, str] | None = None) -> dict[str, Any]:
        """Groups plus the summary numbers. Essentials are judged on every check, including
        the doctor-only rows the page does not list."""
        checks = compute(request)
        broken = config_row(request)
        if broken is not None:
            # Row 1 is doctor-only because a console cannot start with a broken file, but a
            # running one can now see one (019 Addendum B "Validating").
            checks = [broken if c.id == "config.file" else c for c in checks]
        mode = mode_of(request)
        groups = build_groups(
            checks,
            settings_of(request),
            mode,
            home_of(request),
            errors=errors,
            dismissed=read_dismissed(dismissed_path(settings_of(request))),
        )
        return {
            "groups": groups,
            "mode": mode,
            "started": getattr(request.app.state, "settings_read_at", started),
            "boot": started,  # secret and env rows are only ever read at start
            "applied": getattr(request.app.state, "settings_applied", False),
            "essentials_ok": rd.essentials_ok(checks),
            "todo_count": sum(1 for g in groups for r in g.rows if not r.quiet),
        }

    def banner_of(request: Request) -> dict[str, Any] | None:
        """The /, /inbox banner (019 "Banner"): cheap checks only, no value rendered."""
        try:
            checks = rd.run_checks(
                settings_of(request),
                secrets_report=getattr(request.app.state, "secrets_report", None),
                now=now(),
                mode=mode_of(request),
                registry_rows=getattr(request.app.state, "registry_rows", None),
                environ=getattr(request.app.state, "setup_environ", None),
                home=home_of(request),
                cheap_only=True,
            )
            dismissed = read_dismissed(dismissed_path(settings_of(request)))
        except Exception:  # a broken check must never take down Today or the inbox
            return None
        return banner_view(checks, dismissed)

    def first_run_redirect(request: Request, conn: sqlite3.Connection) -> Response | None:
        """303 to /setup while an essential fails and the database holds no jobs."""
        banner = banner_of(request)
        if banner is None or not banner["essentials"]:
            return None
        try:
            if conn.execute("SELECT 1 FROM job LIMIT 1").fetchone() is not None:
                return None
        except sqlite3.Error:
            return None
        return RedirectResponse("/setup", status_code=303)

    templates.env.globals["setup_banner"] = lambda request: banner_of(request)
    app.state.setup_first_run = first_run_redirect

    def page(
        request: Request,
        *,
        show_all: bool = False,
        errors: dict[str, str] | None = None,
        flash: str = "",
        status: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "title": "Setup",
                "active": "/setup",
                "nav": nav,
                "show_all": show_all,
                "flash": flash,
                "oob": False,
                **view_of(request, errors),
            },
            status_code=status,
        )

    @app.get("/setup/gmail")
    def setup_gmail() -> RedirectResponse:
        # The one Gmail how-to is the help page (specs/019 Addendum A, S11e).
        return RedirectResponse(GMAIL_HELP, status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, conn: Conn, all: int = 0, done: str = "") -> HTMLResponse:
        flash = {"dismissed": "Hidden. It comes back if its status changes."}.get(done, "")
        return page(request, show_all=bool(all), flash=flash)

    def profile_dir(request: Request) -> Path:
        return Path(settings_of(request).paths.profile_dir).expanduser()

    @app.post("/setup/starter-prefs")
    def starter_prefs(request: Request, conn: Conn) -> Response:
        pdir = profile_dir(request)
        with _WRITE_LOCK:
            if (pdir / PREFERENCES_FILE).exists():
                raise HTTPException(409, "preferences.yaml already exists; edit it on /prefs")
            write_preferences_data(pdir, gr.SKELETON, backup_stamp=now().strftime("%Y%m%dT%H%M%S"))
        return RedirectResponse("/prefs?created=1", status_code=303)

    def resume_row_response(request: Request, error: str, status: int) -> Response:
        errors = {"profile.resume": error} if error else None
        if request.headers.get("hx-request") == "true":
            view = view_of(request, errors)
            row = next(r for g in view["groups"] for r in g.rows if r.check.id == "profile.resume")
            return templates.TemplateResponse(
                request,
                "_setup_resume_swap.html",
                {"r": row, "show_all": True, "oob": True, **view},
                status_code=status,
            )
        if error:
            return page(request, errors=errors, status=status)
        return RedirectResponse("/setup", status_code=303)

    @app.post("/setup/resume")
    async def upload_resume(
        request: Request, file: Annotated[UploadFile | None, File()] = None
    ) -> Response:
        name = file.filename if file is not None else None
        data = await file.read(gr.MAX_RESUME_BYTES + 1) if file is not None and name else b""
        return await run_in_threadpool(upload_work, request, name, data)

    def upload_work(request: Request, filename: str | None, data: bytes) -> Response:
        if not filename:
            return resume_row_response(request, "Choose a .md or .txt file first.", 422)
        try:
            text = gr.check_upload(filename, data)
        except gr.UploadError as exc:
            return resume_row_response(request, str(exc), exc.status)
        settings = settings_of(request)
        mode = mode_of(request)
        pdir = resolve_path(settings.paths.profile_dir)
        data_dir = resolve_path(settings.paths.data_dir)
        with _WRITE_LOCK:
            before = gr.lenient_state(settings)
            yaml_value = before.profile.resume_path
            unusable = bool(yaml_value) and mode in (rd.CONTAINER, rd.HOST_MANAGING)
            if unusable:
                # An absolute or outside-the-data-dir path never resolves in a container
                # (019 "Modes"): store under the configured resume folder and re-point.
                absolute = Path(yaml_value).expanduser().is_absolute()
                inside = not absolute and rd._within(
                    Path(os.path.normpath(pdir / yaml_value)), data_dir
                )
                unusable = not inside
            if unusable:
                target = resolve_path(settings.paths.resume_path)
            else:
                target = gr.resume_target(settings, before.profile)
            dest = gr.store_resume(target, filename, text, now())
            after = gr.lenient_state(settings)
            if unusable or (after.kind == "resume" and not after.profile.resume_path):
                # Point preferences.yaml at the stored file, relative to the profile folder.
                prefs_file = pdir / PREFERENCES_FILE
                with contextlib.suppress(OSError, ProfileError):  # file stored; row says the rest
                    save_profile_changes(
                        pdir,
                        {"resume_path": os.path.relpath(dest, pdir)},
                        expected_mtime_ns=prefs_file.stat().st_mtime_ns,
                        require_resume=False,
                    )
        return resume_row_response(request, "", 200)

    @app.post("/setup/dismiss")
    async def dismiss(request: Request) -> Response:
        form = await request.form()
        ids = [str(v) for v in form.getlist("check_id")]
        if not ids:
            raise HTTPException(422, "no check_id given")
        if any(i not in rd.CHECK_IDS for i in ids):
            raise HTTPException(422, "unknown check id")
        checks = {c.id: c for c in await run_in_threadpool(compute, request)}
        for i in ids:
            c = checks.get(i)
            if c is None or not can_dismiss(c):
                raise HTTPException(422, "this row cannot be hidden")
        path = dismissed_path(settings_of(request))
        stamp = now().isoformat()
        with _WRITE_LOCK:
            current = read_dismissed(path)
            for i in ids:
                current[i] = {"status": "due" if checks[i].due else checks[i].status, "at": stamp}
            mkdir_private(path.parent)
            _atomic_private_write(path, json.dumps({"dismissed": current}, sort_keys=True))
        back = str(form.get("next") or "")
        if back in ("/", "/inbox"):  # the banner's Hide returns to the page it was on
            return RedirectResponse(back, status_code=303)
        return RedirectResponse("/setup?done=dismissed", status_code=303)
