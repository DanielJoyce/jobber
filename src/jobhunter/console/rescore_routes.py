"""Re-score now on /prefs (specs/014 "Re-score now"): panel, run, progress, cancel.

A run happens on a background thread inside the console process, on its own SQLite
connection, one at a time. Progress lives in the ``rescore_request`` row, so the polled
fragment reads the DB and also shows runs started from the CLI. The console stays
loopback-only; nothing here is reachable from elsewhere.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.console import prefs_graceful as gr
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore as rs
from jobhunter.scoring.decisions import JEV_DEFAULT_MODEL
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import ScorerError, privacy_notice

logger = logging.getLogger(__name__)

SCOPE_LABELS = {
    "recent": f"Recent: open A\N{EN DASH}E jobs from the last {rs.RECENT_DAYS} days",
    "all": "All: every job waiting to be scored",
}
ANTHROPIC_HAIKU = "anthropic:claude-haiku-4-5"


def available_scorers(scoring: Any, env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """(spec, label) for the default scorer plus scorers whose key is set. Never shows keys."""
    env = os.environ if env is None else env
    out = [(scoring.screen_scorer, "configured default")]
    if env.get(scoring.openrouter.api_key_env):
        out.append((f"jev:{JEV_DEFAULT_MODEL}", "Jev, synchronous"))
    if env.get("ANTHROPIC_API_KEY"):
        out.append((ANTHROPIC_HAIKU, "Anthropic batch, collected later"))
    seen: set[str] = set()
    return [(s, label) for s, label in out if not (s in seen or seen.add(s))]


def local_time(value: str | None) -> str:
    """A stored UTC ISO timestamp as local wall-clock time."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


class RescoreManager:
    """One background run at a time, in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.current: int | None = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float = 10.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def start(
        self,
        conn: sqlite3.Connection,
        conn_factory: Callable[[], sqlite3.Connection],
        profile: Profile,
        settings: Any,
        *,
        scope: str,
        spec: str,
        request_id: int | None,
        now: Callable[[], datetime],
        scorer_factory: rs.ScorerFactory | None,
        client_factory: rs.ClientFactory | None,
    ) -> int:
        """Start a run and return its request id. Raises RescoreBusy / RescoreRefused."""
        with self._lock:
            if self.busy() and self.current is not None:
                raise rs.RescoreBusy(self.current)
            live = rs.running_request(conn, now())
            if live is not None:
                raise rs.RescoreBusy(int(live["id"]))
            if request_id is not None:
                row = rs.get_request(conn, request_id)
                if row is None or row["status"] != "pending":
                    raise rs.RescoreRefused(f"re-score #{request_id} is not pending")
                scope = row["scope"]
            plan = rs.make_plan(conn, profile, settings.scoring, scope, spec, now())
            if plan.refusal:
                raise rs.RescoreRefused(plan.refusal)
            rid = request_id or rs.create_request(conn, profile, plan, now())
            self.current = rid
            self._thread = threading.Thread(
                target=self._work,
                args=(
                    conn_factory,
                    rid,
                    profile,
                    settings,
                    spec,
                    now,
                    scorer_factory,
                    client_factory,
                    plan.prefilter,
                ),
                name=f"rescore-{rid}",
                daemon=True,
            )
            self._thread.start()
            return rid

    @staticmethod
    def _work(conn_factory, rid, profile, settings, spec, now, scorer_factory, client_factory, pre):
        conn = conn_factory()  # its own connection: never the request's
        try:
            rs.run_request(
                conn,
                rid,
                profile,
                settings.scoring,
                scorer=spec,
                now=now,
                scorer_factory=scorer_factory,
                client_factory=client_factory,
                prefiltered=pre,
            )
        except Exception as exc:
            logger.warning("re-score #%s could not start: %s", rid, exc)
            try:
                conn.execute(
                    "UPDATE rescore_request SET status = 'failed', error = ?, done_at = ? "
                    "WHERE id = ? AND status IN ('pending', 'running')",
                    (str(exc) or type(exc).__name__, to_iso(now()), rid),
                )
            except sqlite3.Error:
                logger.exception("could not record the re-score failure")
        finally:
            conn.close()


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    app.state.rescore = RescoreManager()
    app.state.rescore_scorer_factory = None  # tests inject a fake; None builds the real scorer
    app.state.rescore_client_factory = None
    templates.env.filters["localtime"] = local_time

    def state_of(request: Request) -> gr.PrefsState:
        return gr.lenient_state(request.app.state.settings)

    def finished(conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM rescore_request WHERE status IN ('done', 'failed') "
            "ORDER BY done_at DESC, id DESC LIMIT 1"
        ).fetchone()

    def panel_ctx(
        request: Request,
        conn: sqlite3.Connection,
        *,
        scope: str | None = None,
        scorer: str | None = None,
        message: str | None = None,
        active_id: int | None = None,
    ) -> dict[str, Any]:
        settings = request.app.state.settings
        st = state_of(request)
        scorers = available_scorers(settings.scoring)
        spec = scorer if scorer in dict(scorers) else scorers[0][0]
        scope = scope if scope in rs.SCOPES else "recent"
        running = rs.running_request(conn, now())
        plan = notice = None
        if st.kind == "ok" and running is None:
            try:
                plan = rs.make_plan(conn, st.profile, settings.scoring, scope, spec, now())
                notice = privacy_notice(spec, settings.scoring)
            except (rs.RescoreError, ScorerError) as exc:
                message = message or str(exc)
        shown = running
        if shown is None and active_id is not None:
            shown = rs.get_request(conn, active_id)
        if shown is None:
            shown = finished(conn)
        return {
            "ok": st.kind == "ok",
            "plan": plan,
            "notice": notice,
            "scope": scope,
            "scorer": spec,
            "scorers": scorers,
            "scope_labels": SCOPE_LABELS,
            "running": running,
            "shown": shown,
            "message": message,
            "pending": conn.execute(
                "SELECT * FROM rescore_request WHERE status = 'pending' ORDER BY id DESC"
            ).fetchall(),
        }

    def render_panel(request: Request, conn: sqlite3.Connection, **kw: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_rescore_panel.html", {"rs": panel_ctx(request, conn, **kw)}
        )

    # The prefs page embeds the same context.
    app.state.rescore_panel_context = panel_ctx

    @app.get("/prefs/rescore/panel", response_class=HTMLResponse)
    def panel(
        request: Request, conn: Conn, scope: str | None = None, scorer: str | None = None
    ) -> HTMLResponse:
        return render_panel(request, conn, scope=scope, scorer=scorer)

    @app.post("/prefs/rescore/run", response_class=HTMLResponse)
    async def run(request: Request, conn: Conn) -> HTMLResponse:
        from jobhunter.console import prefs as pf

        form = pf.parse_form(await request.body())
        scope = pf.form_value(form, "scope", "recent")
        spec_in = pf.form_value(form, "scorer", "")
        raw_id = pf.form_value(form, "request_id", "")
        settings = request.app.state.settings
        st = state_of(request)
        if st.kind != "ok":
            return render_panel(request, conn, message="Fix the preferences file first.")
        spec = (
            spec_in
            if spec_in in dict(available_scorers(settings.scoring))
            else (settings.scoring.screen_scorer)
        )
        try:
            rid = request.app.state.rescore.start(
                conn,
                request.app.state.conn_factory,
                st.profile,
                settings,
                scope=scope,
                spec=spec,
                request_id=int(raw_id) if raw_id.isdigit() else None,
                now=now,
                scorer_factory=request.app.state.rescore_scorer_factory,
                client_factory=request.app.state.rescore_client_factory,
            )
        except rs.RescoreBusy as exc:
            return render_panel(
                request, conn, message=f"{exc} Showing its progress.", active_id=exc.request_id
            )
        except (rs.RescoreRefused, ScorerError) as exc:
            return render_panel(request, conn, scope=scope, scorer=spec, message=str(exc))
        return render_panel(request, conn, active_id=rid, scope=scope, scorer=spec)

    @app.get("/prefs/rescore/progress/{request_id}", response_class=HTMLResponse)
    def progress(request: Request, conn: Conn, request_id: int) -> HTMLResponse:
        row = rs.get_request(conn, request_id)
        return templates.TemplateResponse(request, "_rescore_progress.html", {"r": row})

    @app.post("/prefs/rescore/{request_id}/cancel", response_class=HTMLResponse)
    def cancel(request: Request, conn: Conn, request_id: int) -> HTMLResponse:
        rs.cancel_request(conn, request_id, now())
        return render_panel(request, conn)
