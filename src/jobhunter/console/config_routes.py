"""Config watch routes (specs/019 Addendum B, S12a): the notice, ``GET /config/changes``,
``POST /config/apply`` and ``POST /config/dismiss``.

The check itself runs inside ``GET /activity`` (``notice_context``), throttled in
``jobhunter.config_watch``. Apply swaps ``app.state.settings`` once, under
``app.state.settings_lock``, with every restart-class field kept at its running value. Restart is
S12b: until then the notice shows the restart command only.

Who may press: every route goes through the app's middleware (loopback ``Host``, same-origin
writes). Apply also needs a loopback **peer**, even under ``--allow-remote``; a LAN browser
sees the command instead of the button. Dismiss needs only the middleware.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local annotations.
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter import config_watch as cw
from jobhunter import readiness as rd

logger = logging.getLogger(__name__)

APPLIED_SHOWN_FOR = timedelta(minutes=10)
CTR_DOCTOR = "jobhunter-ctr doctor"


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


def current_state(request: Request, *, force: bool = False) -> cw.State:
    """The watch state against the running settings; EMPTY when the console is not watching.
    A failure is logged by type only (its text could carry a value) and shows nothing."""
    watch = watcher_of(request)
    if watch is None:
        return cw.EMPTY
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
    return {
        "cfg": state if show else None,
        "cfg_summary": summary(state, read_at, applied is not None) if show else "",
        "cfg_applied": applied,
        "cfg_peer_ok": peer_is_loopback(request),
        # A browser that may not press Apply gets the command that picks the change up instead.
        "cfg_restart": restart_command(request)
        if show and (state.needs_restart or (state.can_apply and not peer_is_loopback(request)))
        else "",
        "cfg_ctr_doctor": CTR_DOCTOR,
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
            },
        )

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
