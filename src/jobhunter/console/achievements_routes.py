"""Routes for /achievements (specs/021 "UI"): a stub board, the seen and settings writes.

A2 ships the routes so A3 (the board), A4 (the notice) and A5 (the Today strip) can be built in
parallel and their links resolve. The page here is deliberately plain: a count, the earned
list, and the settings form; A3 replaces the template.

- ``GET /achievements``: evaluates on every full load (it is rare), falls back to the stored
  unlocks on any ``sqlite3.Error`` (a pipeline run holding the write lock), then marks the
  unlocks it rendered as seen, writing only when one of them is unseen, with the same 250 ms
  busy timeout and fallback.
- ``POST /achievements/seen``: ``ids`` (repeatable) are marked seen; answers 204 with
  ``HX-Trigger: jh-activity`` so the activity band refreshes.
- ``POST /achievements/settings``: each field is optional and only fields present change:
  ``enabled`` and ``notify`` (``1``/``0``, or ``on``/``off``), and ``hidden`` (repeatable
  category keys) together with ``hidden_set=1`` (so hiding nothing can be submitted).
  Turning achievements back on clears ``backfilled_at`` so the next evaluation is a silent
  backfill. Answers 303 to ``/achievements``.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter.achievements import catalog, engine
from jobhunter.core import db

logger = logging.getLogger(__name__)

TRUE = ("1", "on", "true", "yes")
FALSE = ("0", "off", "false", "no")


async def multi_form(request: Request) -> dict[str, list[str]]:
    """An urlencoded body with repeated keys kept (no python-multipart)."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(raw, keep_blank_values=True)


MultiForm = Annotated[dict[str, list[str]], Depends(multi_form)]


def gate(app: FastAPI) -> engine.Gate:
    return app.state.achievements


def _flag(form: dict[str, list[str]], key: str) -> bool | None:
    values = form.get(key)
    if not values:
        return None
    v = values[-1].strip().lower()
    return True if v in TRUE else False if v in FALSE else None


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.get("/achievements", response_class=HTMLResponse)
    def achievements_page(request: Request, conn: Conn) -> HTMLResponse:
        g = gate(app)
        settings = engine.read_settings(conn)
        g.enabled = settings.enabled
        moment = now()
        if settings.enabled:
            try:
                g.refresh(conn, moment, force=True)
            except sqlite3.Error as exc:  # render what is stored; the next call retries
                logger.info("achievements: evaluate skipped: %s", exc)
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        visible = [u for u in engine.stored(conn) if u.category not in settings.hidden]
        visible = [u for u in visible if u.achievement_id in catalog.BY_ID]
        unseen = [u.id for u in visible if u.seen_at is None]
        if settings.enabled and unseen:
            try:
                engine.mark_seen(conn, unseen, moment, busy_ms=engine.WRITE_BUSY_MS)
            except sqlite3.Error as exc:  # the notice stays until the next visit
                logger.info("achievements: seen not recorded: %s", exc)
        cards = []
        for a in catalog.CATALOG:
            if a.category in settings.hidden:
                continue
            tiers = sorted(u.tier for u in visible if u.achievement_id == a.id)
            if tiers:
                cards.append({"a": a, "tier": max(tiers)})
        return templates.TemplateResponse(
            request,
            "achievements.html",
            {
                "title": "Achievements",
                "active": "/achievements",
                "nav": nav,
                "settings": settings,
                "categories": catalog.CATEGORIES,
                "earned": len(visible),
                "cards": cards,
            },
        )

    @app.post("/achievements/seen")
    def achievements_seen(conn: Conn, form: MultiForm) -> Response:
        ids = [int(x) for v in form.get("ids", []) for x in v.split(",") if x.strip().isdigit()]
        engine.mark_seen(conn, ids, now())
        return Response(status_code=204, headers={"HX-Trigger": "jh-activity"})

    @app.post("/achievements/settings")
    def achievements_settings(conn: Conn, form: MultiForm) -> Response:
        moment = now()
        enabled = _flag(form, "enabled")
        notify = _flag(form, "notify")
        with db.transaction(conn):
            before = engine.read_settings(conn)
            if enabled is not None:
                engine.put_setting(conn, engine.ENABLED, "1" if enabled else "0", moment)
                if enabled and not before.enabled:
                    # Back on: the next evaluation is a silent backfill.
                    conn.execute(
                        "DELETE FROM achievement_setting WHERE key = ?", (engine.BACKFILLED_AT,)
                    )
            if notify is not None:
                engine.put_setting(conn, engine.NOTIFY, "1" if notify else "0", moment)
            if form.get("hidden_set"):
                hidden = set(form.get("hidden", [])) & set(catalog.CATEGORY_KEYS)
                engine.put_setting(conn, engine.HIDDEN, engine.hidden_json(hidden), moment)
                engine.mark_category_seen(conn, hidden - before.hidden, moment)
        g = gate(app)
        g.reload_enabled(conn)
        g.invalidate()
        return RedirectResponse("/achievements", status_code=303)
