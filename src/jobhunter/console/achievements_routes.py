"""Routes for /achievements (specs/021 "UI"): a stub board, the seen and settings writes.

The board (A3) groups cards by category: earned, then in progress, then locked.

- ``GET /achievements``: evaluates on every full load (it is rare), falls back to the stored
  unlocks on any ``sqlite3.Error`` (a pipeline run holding the write lock), then marks the
  unlocks it rendered as seen, writing only when one of them is unseen, with the same 250 ms
  busy timeout and fallback.
- ``POST /achievements/seen``: ``ids`` (repeatable, or comma-separated: the band's x posts
  exactly the ids its notice shows) are marked seen, waiting at most 250 ms for the write lock;
  answers 204 with ``HX-Trigger: jh-activity`` so the activity band refreshes, or 503 when the
  database is busy (the notice stays and the next poll shows it again).
- ``POST /achievements/settings``: each field is optional and only fields present change:
  ``enabled`` and ``notify`` (``1``/``0``, or ``on``/``off``), and ``hidden`` (repeatable
  category keys) together with ``hidden_set=1`` (so hiding nothing can be submitted).
  Turning achievements back on clears ``backfilled_at`` so the next evaluation is a silent
  backfill. Answers 303 to ``/achievements``.

Prompt feedback (specs/021 "Unlock notice", ticket A4): a successful POST to a console write
route that can unlock something (``UNLOCK_WRITES``) gets ``HX-Trigger: jh-activity``, merged
into any trigger the route set itself, so the band (``jh-activity from:body``) refreshes at
once instead of on its next 3 s or 60 s poll. The write changes the fingerprint, so that
refresh evaluates.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from jobhunter import readiness
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


BAND_EVENT = "jh-activity"

# Console write routes whose change can unlock (or, within 24 h, revoke) something.
UNLOCK_WRITES = tuple(
    re.compile(p)
    for p in (
        r"/pipeline/\d+/(move|event|details|next-action)",
        r"/pipeline/\d+/contacts(/\d+/delete)?",
        r"/pipeline/\d+/attachments(/\d+/delete)?",
        r"/job/\d+/(applied|prepare)",
        r"/followups/\d+/(done|no-response|applied)",
        r"/inbox/(\d+/label|\d+/undo|bulk|bulk/undo)",
        r"/packet/\d+/ready",
        r"/rejections(/\d+/(confirm|delete))?",
        r"/proposals/\d+/accept",
        r"/alerts/[^/]+/(subscribe|unsubscribe)",
        r"/prefs/(save|section/[^/]+|raw|revert/\d+)",
        r"/setup/starter-prefs",
    )
)


def is_unlock_write(method: str, path: str) -> bool:
    return method == "POST" and any(p.fullmatch(path) for p in UNLOCK_WRITES)


def with_band_event(existing: str | None) -> str:
    """``HX-Trigger`` with ``jh-activity`` added to whatever the route already set (a
    comma-separated list of names, or a JSON object)."""
    if not existing or not existing.strip():
        return BAND_EVENT
    text = existing.strip()
    if text.startswith("{"):
        try:
            events = json.loads(text)
        except ValueError:
            return existing
        if isinstance(events, dict):
            events.setdefault(BAND_EVENT, "")
            return json.dumps(events)
        return existing
    names = [n.strip() for n in text.split(",") if n.strip()]
    return existing if BAND_EVENT in names else ", ".join([*names, BAND_EVENT])


def gate(app: FastAPI) -> engine.Gate:
    return app.state.achievements


def essentials_judge(app: FastAPI) -> Callable[[], bool | None]:
    """The gate's ``setup.ready`` input: 019's essentials over the cheap checks, from the same
    app state /setup reads, with no spend read. Called at most once per fingerprint."""

    def judge() -> bool | None:
        state = app.state
        return readiness.essentials_ok_cheap(
            state.settings,
            secrets_report=getattr(state, "secrets_report", None),
            now=datetime.now(UTC),
            mode=getattr(state, "setup_mode", None)
            or readiness.detect_mode(settings=state.settings),
            registry_rows=getattr(state, "registry_rows", None),
            environ=getattr(state, "setup_environ", None),
            home=getattr(state, "setup_home", None),
        )

    return judge


def _flag(form: dict[str, list[str]], key: str) -> bool | None:
    values = form.get(key)
    if not values:
        return None
    v = values[-1].strip().lower()
    return True if v in TRUE else False if v in FALSE else None


# Evidence tables that have a page to link to; the rest are shown as plain text.
EVIDENCE_LINKS = {"application": "/pipeline/{}", "job_group": "/job/{}"}
EVIDENCE_LABELS = {
    "application": "Application",
    "job_group": "Job",
    "week": "Week of",
    "contact": "Contact",
    "rejection": "Reply",
    "followup_log": "Follow-up",
    "application_packet": "Packet",
    "capture_log": "Captured",
    "alert_signup": "Alert",
    "profile_change": "Prefs saved",
    "manual": "Added by hand",
}
MAX_EVIDENCE = 12


def _evidence(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    for table, ids in data.items():
        if not isinstance(ids, list):
            continue
        tmpl = EVIDENCE_LINKS.get(table)
        for ident in ids:
            label = EVIDENCE_LABELS.get(table, str(table))
            href = tmpl.format(ident) if tmpl and str(ident).isdigit() else None
            out.append({"text": f"{label} {ident}", "href": href})
    return out


def _local(iso: str, tz: Any) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(tz).strftime("%b %-d")
    except ValueError:
        return ""


def _rhythm_line(r: Any) -> str:
    if r is None:
        return ""
    if r.run:
        return f"Rhythm: {r.run} week{'s' if r.run != 1 else ''}"
    if r.best:
        return f"Rhythm: paused. Your best run is {r.best} week{'s' if r.best != 1 else ''}"
    return ""


def _board(
    conn: sqlite3.Connection,
    g: engine.Gate,
    moment: datetime,
    settings: engine.Settings,
    visible: list[engine.Unlock],
    busy: bool,
) -> dict[str, Any]:
    """Sections of cards, newest earned first, then in progress, then locked."""
    tz = g.zone()
    comp = None
    if settings.enabled:
        try:
            comp = engine.compute(conn, moment, tz)
        except sqlite3.Error as exc:  # progress bars are left out on a busy database
            logger.info("achievements: progress unavailable: %s", exc)
    by_ach: dict[str, list[engine.Unlock]] = {}
    for u in visible:
        by_ach.setdefault(u.achievement_id, []).append(u)
    sections = []
    for key, label in catalog.CATEGORIES:
        if key in settings.hidden:
            continue
        cards = []
        for a in catalog.CATALOG:
            if a.category != key:
                continue
            ups = sorted(by_ach.get(a.id, []), key=lambda u: u.tier)
            top = ups[-1] if ups else None
            tier = top.tier if top else 0
            ev = _evidence(top.evidence) if top else []
            done = tier >= a.tiers
            progress = comp.series[a.id].progress if comp and a.id in comp.series else None
            target = a.thresholds[min(tier, a.tiers - 1)]
            state = "earned" if tier else ("progress" if progress else "locked")
            cards.append(
                {
                    "a": a,
                    "tier": tier,
                    "done": done,
                    "state": state,
                    "secret": a.hidden and not tier,
                    "progress": None if progress is None else min(progress, target),
                    "raw_progress": progress,
                    "target": target,
                    "when": _local(top.unlocked_at, tz) if top else "",
                    "sort": top.unlocked_at if top else "",
                    "evidence": ev[:MAX_EVIDENCE],
                    "evidence_more": max(0, len(ev) - MAX_EVIDENCE),
                }
            )
        order = {"earned": 0, "progress": 1, "locked": 2}
        cards.sort(key=lambda c: c["sort"], reverse=True)  # newest first within a state
        cards.sort(key=lambda c: order[c["state"]])
        sections.append({"key": key, "label": label, "cards": cards})
    r = comp.rhythm if comp else None
    return {
        "sections": sections,
        "rhythm_line": _rhythm_line(r),
        "freezes": r.freezes if r else 0,
        "week_active": bool(r and r.this_week_days),
        "have_progress": comp is not None,
    }


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    @app.middleware("http")
    async def band_refresh_trigger(request: Request, call_next):
        response = await call_next(request)
        if (
            response.status_code < 400
            and gate(app).enabled
            and is_unlock_write(request.method, request.url.path)
        ):
            response.headers["HX-Trigger"] = with_band_event(response.headers.get("HX-Trigger"))
        return response

    @app.get("/achievements", response_class=HTMLResponse)
    def achievements_page(request: Request, conn: Conn) -> HTMLResponse:
        g = gate(app)
        settings = engine.read_settings(conn)
        g.enabled = settings.enabled
        moment = now()
        busy = False
        if settings.enabled:
            try:
                g.refresh(conn, moment, force=True, essentials=essentials_judge(app))
            except sqlite3.Error as exc:  # render what is stored; the next call retries
                logger.info("achievements: evaluate skipped: %s", exc)
                busy = True  # don't wait on the lock a second time for seen
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        visible = [u for u in engine.stored(conn) if u.category not in settings.hidden]
        visible = [u for u in visible if u.achievement_id in catalog.BY_ID]
        unseen = [u.id for u in visible if u.seen_at is None]
        if settings.enabled and unseen and not busy:
            try:
                engine.mark_seen(conn, unseen, moment, busy_ms=engine.WRITE_BUSY_MS)
            except sqlite3.Error as exc:  # the notice stays until the next visit
                logger.info("achievements: seen not recorded: %s", exc)
        board = _board(conn, g, moment, settings, visible, busy)
        resp = templates.TemplateResponse(
            request,
            "achievements.html",
            {
                "title": "Achievements",
                "active": "/achievements",
                "nav": nav,
                "settings": settings,
                "categories": catalog.CATEGORIES,
                "earned": len(visible),
                **board,
            },
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/achievements/seen")
    def achievements_seen(conn: Conn, form: MultiForm) -> Response:
        ids = [
            int(x)
            for v in form.get("ids", [])
            for x in v.split(",")
            if x.strip().isdigit() and len(x.strip()) < 19
        ]
        try:
            engine.mark_seen(conn, ids, now(), busy_ms=engine.WRITE_BUSY_MS)
        except sqlite3.Error as exc:  # a run holds the write lock: the notice stays for now
            logger.info("achievements: seen not recorded: %s", exc)
            return Response(status_code=503, headers={"Retry-After": "3"})
        return Response(status_code=204, headers={"HX-Trigger": BAND_EVENT})

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
