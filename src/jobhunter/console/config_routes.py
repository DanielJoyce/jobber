"""Config watch routes (specs/019 Addendum B, S12a and S12b): the notice,
``GET /config/changes``, ``POST /config/apply``, ``POST /config/restart`` and
``POST /config/dismiss``.

The check itself runs inside ``GET /activity`` (``notice_context``), throttled in
``jobhunter.config_watch``. Apply swaps ``app.state.settings`` once, under
``app.state.settings_lock``, with every restart-class field kept at its running value.

Restart (S12b) checks the files a new process would read in a child process, takes the work
registry (refused with 409 while a re-score, Score now or draft runs), answers 202 with a page
that polls ``/healthz`` for the new process, and then asks uvicorn to stop; the ``console``
command re-execs or exits 42 (``console/restart.py``). Only a console started by ``jobhunter
console`` can restart itself (``app.state.restart_shutdown``); a container whose unit has not
opted in (``JOBHUNTER_RESTART_EXIT``) cannot, and gets the command instead.

Who may press: every route goes through the app's middleware (loopback ``Host``, same-origin
writes). Apply and Restart also need a loopback **peer**, even under ``--allow-remote``; a LAN
browser sees the command instead of the button. Dismiss needs only the middleware.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local annotations.
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from jobhunter import config_watch as cw
from jobhunter import container
from jobhunter import readiness as rd
from jobhunter.console import restart as rs

logger = logging.getLogger(__name__)

APPLIED_SHOWN_FOR = timedelta(minutes=10)
CTR_DOCTOR = "jobhunter-ctr doctor"
UNCHECKED_CONFIRM = (
    "Restart without checking the new file? If it has errors, the console will not come back "
    f"until you fix it; check it with {CTR_DOCTOR}."
)


def peer_is_loopback(request: Request) -> bool:
    """The TCP peer, not the Host header: a LAN client under ``--allow-remote`` is refused."""
    from jobhunter.console.app import is_loopback  # app imports this module

    host = request.client.host if request.client else ""
    return bool(host) and is_loopback(host)


def watcher_of(request: Request) -> cw.ConfigWatcher | None:
    return getattr(request.app.state, "config_watch", None)


def mode_of(request: Request) -> str:
    override = getattr(request.app.state, "setup_mode", None)
    return override or rd.detect_mode(settings=request.app.state.settings)


def restart_command(request: Request) -> str:
    return rd.RESTART_COMMAND[mode_of(request)].replace("`", "")


def can_restart(request: Request) -> bool:
    """This console can restart itself, and this browser may ask it to."""
    return request.app.state.restart_shutdown is not None and peer_is_loopback(request)


def safe_next(value: str | None) -> str:
    """A local path to come back to after the restart; anything else is ``/``."""
    value = value or ""
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def code_note(request: Request) -> str:
    """Says so when the code on disk is not the code this console started with: a restart
    runs the new code (and does not run ``uv sync``)."""
    started = request.app.state.started_commit
    if not started:
        return ""
    from jobhunter.ops import version

    now_commit = version.build_commit()[0]
    if not now_commit or now_commit == started:
        return ""
    return (
        f"The code on disk ({now_commit[:10]}) differs from the running console "
        f"({started[:10]}): a restart runs the new code. Run uv sync first if its "
        "dependencies changed."
    )


def current_state(request: Request, *, force: bool = False) -> cw.State:
    """The watch state against the running settings; EMPTY when the console is not watching.
    A failure is logged by type only (its text could carry a value) and shows nothing."""
    watch = watcher_of(request)
    if watch is None:
        return cw.EMPTY
    lock = getattr(request.app.state, "settings_lock", None)
    if not force and lock is not None:
        # A save, Load or Apply holds the lock between writing models.toml and assigning the
        # running settings: a check now would show the console's own write as an outside
        # change. Skip it and show the last state; the next poll checks again.
        if not lock.acquire(blocking=False):
            return watch.last_state
        try:
            return _check(watch, request, force)
        finally:
            lock.release()
    return _check(watch, request, force)


def _check(watch: cw.ConfigWatcher, request: Request, force: bool) -> cw.State:
    try:
        return watch.check(request.app.state.settings, force=force)
    except Exception as exc:
        logger.warning("config watch: check failed (%s)", type(exc).__name__)
        return cw.EMPTY


def notice_context(request: Request, now: datetime) -> dict[str, Any]:
    """What the ``#activity`` bar needs for the config notice."""
    watch = watcher_of(request)
    state = current_state(request)
    applied = None
    when = watch.applied_at if watch is not None else None
    if when is not None and now - when <= APPLIED_SHOWN_FOR:
        applied = (watch.applied_count, when)
    show = state.pending and watch is not None and not watch.is_dismissed(state)
    read_at = request.app.state.settings_read_at
    # The page the bar sits on, so the restart page can come back to it.
    here = urlsplit(request.headers.get("hx-current-url") or "").path
    return {
        "cfg": state if show else None,
        "cfg_summary": summary(state, read_at, applied is not None) if show else "",
        "cfg_applied": applied,
        "cfg_peer_ok": peer_is_loopback(request),
        # The Restart button (S12b), where this console can restart itself.
        "cfg_can_restart": can_restart(request),
        "cfg_next": safe_next(here),
        # A browser that may not press the button gets the command that picks the change up.
        "cfg_restart": restart_command(request)
        if show
        and not can_restart(request)
        and (state.needs_restart or (state.can_apply and not peer_is_loopback(request)))
        else "",
        "cfg_ctr_doctor": CTR_DOCTOR,
        "cfg_confirm": UNCHECKED_CONFIRM,
    }


def hhmm(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def summary(state: cw.State, read_at: datetime, just_applied: bool = False) -> str:
    """The notice's first line. Names and counts only, never a value."""
    if state.errors:
        return f"config.toml has errors; the console keeps the settings from {hhmm(read_at)}."
    if state.models_errors:
        return f"models.toml has errors; the console keeps the settings from {hhmm(read_at)}."
    parts: list[str] = []
    if state.replaced:
        parts.append(
            "config.toml may have been replaced on the host; this console cannot see the new "
            "file until it restarts."
        )
    if state.fields:
        if just_applied and state.reload_count == 0 and state.restart_count:
            verb = "needs" if state.restart_count == 1 else "need"
            parts.append(f"{plural(state.restart_count, 'setting')} still {verb} a restart.")
        else:
            for source in ("config.toml", "models.toml"):
                mine = [f for f in state.fields if f.source == source]
                if not mine:
                    continue
                inert = [f for f in mine if f.cls == cw.NO_EFFECT]
                where = " and ".join(dict.fromkeys(f.set_on or cw.SET_ON_CLI for f in inert))
                text = f"{source} changed: {plural(len(mine), 'setting')}"
                if inert and len(inert) == len(mine):
                    text += f" (no effect: set on {where})"
                elif inert:
                    text += f"; {len(inert)} with no effect (set on {where})"
                parts.append(text + ".")
    for change in state.env:
        parts.append(f"{change.file} changed: {plural(change.count, 'key')}.")
    return " ".join(parts)


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    def back(request: Request) -> Response:
        # htmx follows the redirect and swaps the fresh #activity fragment in.
        target = "/activity" if request.headers.get("hx-request") else "/config/changes"
        return RedirectResponse(target, status_code=303)

    @app.get("/config/changes", response_class=HTMLResponse)
    def config_changes(request: Request, flash: str = "") -> HTMLResponse:
        watch = watcher_of(request)
        state = current_state(request)
        messages = {
            "applied": "Applied.",
            "moved": "config.toml changed again before Apply; this is the new change.",
            "nothing": "Nothing to apply.",
            "models": "A models.toml change made outside the console is applied here, with "
            "Apply now, together with any config.toml change.",
        }
        return templates.TemplateResponse(
            request,
            "config_changes.html",
            {
                "title": "Config changes",
                "active": "",
                "nav": nav,
                "watching": watch is not None,
                "state": state,
                "dismissed": watch is not None and watch.is_dismissed(state),
                "peer_ok": peer_is_loopback(request),
                "restart": restart_command(request),
                "ctr_doctor": CTR_DOCTOR,
                "read_at": request.app.state.settings_read_at,
                "applied": request.app.state.settings_applied,
                "flash": messages.get(flash, ""),
                "classes": cw.CLASS_TEXT,
                "can_restart": can_restart(request),
                "running_work": request.app.state.work.labels(),
                "code_note": code_note(request),
                "confirm": UNCHECKED_CONFIRM,
            },
        )

    def restart_page(request: Request, status: int, **ctx: Any) -> HTMLResponse:
        base = {
            "title": "Restart console",
            "active": "",
            "nav": nav,
            "accepted": False,
            "why": "",
            "lines": [],
            "running": [],
            "restart": restart_command(request),
            "ctr_doctor": CTR_DOCTOR,
            "confirm_unchecked": False,
            "confirm": UNCHECKED_CONFIRM,
            "next": "/",
            "code_note": "",
            "show_command": False,
            "doctor": container.doctor_command(),
            "journal": container.is_container(),
        }
        base.update(ctx)
        return templates.TemplateResponse(request, "config_restart.html", base, status_code=status)

    @app.post("/config/restart")
    def config_restart(
        request: Request, unchecked: str = Form(""), next: str = Form("/")
    ) -> Response:
        """Restart the console (019 Addendum B "Restart"). A plain ``def``: the child check
        can take seconds, so it runs in the threadpool, never on the event loop."""
        if not peer_is_loopback(request):
            return PlainTextResponse(
                "refused: Restart works only from the machine running the console",
                status_code=403,
            )
        back_to = safe_next(next)
        shutdown = request.app.state.restart_shutdown
        if shutdown is None:
            return restart_page(
                request,
                409,
                why="This console cannot restart itself. Restart it by hand:",
                show_command=True,
                next=back_to,
            )
        if request.app.state.work.restarting:
            return restart_page(request, 409, why="A restart is already under way.", next=back_to)
        state = current_state(request, force=True)
        # In a container that cannot see the new file, the child cannot see it either: no
        # check, so ask first.
        skip_check = state.replaced and container.is_container()
        not_starting = (
            "The console would not start with the files as they are now, so it keeps "
            "running. Fix these and try again:"
        )
        if state.errors and not skip_check:
            # An empty, missing or broken config.toml: a new process would start on the
            # defaults or not at all. The watcher already says why, without values.
            return restart_page(
                request, 409, why=not_starting, lines=list(state.errors), next=back_to
            )
        if skip_check and unchecked != "1":
            return restart_page(request, 409, confirm_unchecked=True, next=back_to)
        if bind_lines := bind_problems(request, state):
            return restart_page(request, 409, why=not_starting, lines=bind_lines, next=back_to)
        if not skip_check:
            watch = watcher_of(request)
            check = request.app.state.restart_preflight or rs.run_preflight
            lines = check(require_settings=watch is not None and watch.start_has_settings)
            if lines:
                logger.warning("console restart refused: the new settings do not load")
                return restart_page(request, 409, why=not_starting, lines=lines, next=back_to)
        running = request.app.state.work.begin_restart()
        if running:
            return restart_page(
                request,
                409,
                why="Work is running; restart when it finishes:",
                running=running,
                next=back_to,
            )
        try:
            # 303 to a GET page: reloading the waiting page never posts a second restart.
            target = "/config/restarting?" + urlencode(
                {
                    "boot": request.app.state.boot_id,
                    "base": new_base(request, state),
                    "next": back_to,
                }
            )
            response = RedirectResponse(target, status_code=303)
            delay = request.app.state.restart_delay
            # After the response is sent, and a moment later so the browser can fetch the
            # waiting page first; uvicorn then drains in-flight requests and stops.
            response.background = BackgroundTask(_stop_after, delay, shutdown)
        except BaseException:
            request.app.state.work.cancel_restart()  # never leave paid work refused for nothing
            raise
        logger.warning("console restart: accepted; stopping the server")
        return response

    @app.get("/config/restarting", response_class=HTMLResponse)
    def config_restarting(
        request: Request, boot: str = "", base: str = "", next: str = "/"
    ) -> HTMLResponse:
        """The page that waits for the new process. Reading it changes nothing."""
        boot_id = boot if re.fullmatch(r"[0-9a-f]{1,64}", boot) else ""
        target = checked_base(request, base)
        return restart_page(
            request,
            200,
            accepted=True,
            base=target,
            same_origin=target == own_base(request),
            boot=boot_id,
            give_up=rs.GIVE_UP_S,
            next=safe_next(next),
            code_note=code_note(request),
            hide_activity=True,
        )

    def bind_problems(request: Request, state: cw.State) -> list[str]:
        """A moved host or port the new console could not use."""
        moved = {f.field for f in state.fields if f.cls == cw.RESTART}
        new = state.new_settings
        if new is None or not moved & {"console.host", "console.port"}:
            return []
        from jobhunter.console.app import check_host  # app imports this module

        running = request.app.state.settings.console
        host = new.console.host if "console.host" in moved else running.host
        if "console.host" not in moved:
            host = request.app.state.bind_host or host
        port = new.console.port if "console.port" in moved else running.port
        if "console.host" in moved:
            interlock = container.published_loopback_only() and host == "0.0.0.0"
            try:
                check_host(host, request.app.state.allow_remote or interlock)
            except ValueError:
                return [
                    "console.host: not a loopback address, so the new console would refuse "
                    "to start (it needs --allow-remote)"
                ]
        if "console.port" in moved and not request.app.state.port_free(host, port):
            return ["console.port: that port is already in use"]
        return []

    @app.post("/config/apply")
    def config_apply(request: Request, key: str = Form("")) -> Response:
        if not peer_is_loopback(request):
            return PlainTextResponse(
                "refused: Apply works only from the machine running the console", status_code=403
            )
        watch = watcher_of(request)
        if watch is None:
            return PlainTextResponse("this console is not watching config.toml", status_code=409)
        with request.app.state.settings_lock:
            running = request.app.state.settings
            # Re-run the check: apply exactly what is on disk now, not what the page showed.
            state = watch.check(running, force=True)
            if key and key != state.key:
                return _flash(request, "moved")
            if not state.can_apply or state.new_settings is None:
                return _flash(request, "nothing")
            new = cw.partial(running, state.new_settings, env_pending=bool(state.env))
            request.app.state.settings = new  # one attribute assignment: atomic
            when = now()
            request.app.state.settings_read_at = when
            request.app.state.settings_applied = True
            watch.mark_applied(state.reload_count, when)
            watch.check(new, force=True)  # what is left: restart-class fields
        logger.info(
            "config watch: applied %d settings (%s)",
            state.reload_count,
            ", ".join(f.field for f in state.fields if f.cls == cw.RELOAD),
        )
        return back(request)

    def _flash(request: Request, flash: str) -> Response:
        if request.headers.get("hx-request"):
            return RedirectResponse("/activity", status_code=303)
        return RedirectResponse(f"/config/changes?flash={flash}", status_code=303)

    @app.post("/config/dismiss")
    def config_dismiss(request: Request, key: str = Form("")) -> Response:
        watch = watcher_of(request)
        if watch is not None:
            watch.dismiss(key)
        return back(request)


def own_base(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def new_base(request: Request, state: cw.State) -> str:
    """Where the restarted console will answer: this URL, unless the change moves the bind
    host or port (and the command line does not pin it)."""
    url = request.url
    moved = {f.field for f in state.fields if f.cls == cw.RESTART}
    new = state.new_settings
    if new is None or not moved & {"console.host", "console.port"}:
        return own_base(request)
    host = url.hostname or "127.0.0.1"
    port = url.port
    if "console.host" in moved:
        host = new.console.host
        host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if "console.port" in moved:
        port = new.console.port
    shown = f"[{host}]" if ":" in host else host
    return f"{url.scheme}://{shown}:{port}" if port else f"{url.scheme}://{shown}"


def _stop_after(delay: float, shutdown: Callable[[], None]) -> None:
    if delay > 0:
        time.sleep(delay)
    shutdown()


def checked_base(request: Request, base: str) -> str:
    """The address the waiting page polls: a loopback (or this console's own) host only,
    so the query string cannot send the browser elsewhere."""
    from jobhunter.console.app import is_loopback  # app imports this module

    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        return own_base(request)
    host = parts.hostname or ""
    if (
        parts.scheme not in ("http", "https")
        or not host
        or not (is_loopback(host) or host == request.url.hostname)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        return own_base(request)
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown}:{port}" if port else f"{parts.scheme}://{shown}"
