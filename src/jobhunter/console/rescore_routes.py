"""Re-score now on /prefs (specs/014 "Re-score now"): panel, run, progress, cancel.

A run happens on a background thread inside the console process, on its own SQLite
connection, one at a time. Progress lives in the ``rescore_request`` row, so the polled
fragment reads the DB and also shows runs started from the CLI. The console stays
loopback-only, and ``app.py`` refuses cross-site state-changing requests.

The confirm dialog shows a plan (groups, estimate, spend ceiling); the form posts that plan's
token back, and a run starts only when the plan built at click time is the same one. The plan
is stored on the request, so the run cannot grow past what was confirmed.
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

from jobhunter.config import (
    check_scorer_spec,
    hidden_label,
    resolve_path,
    shown_value,
    usable_model_text,
)
from jobhunter.console import prefs_graceful as gr
from jobhunter.console.work import Restarting, WorkRegistry, refused
from jobhunter.core import runlock
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore as rs
from jobhunter.scoring.decisions import JEV_DEFAULT_MODEL
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import PRESENT, ScorerError, credential_for, privacy_notice

logger = logging.getLogger(__name__)

SCOPE_LABELS = {
    "recent": f"Recent: open A\N{EN DASH}E jobs from the last {rs.RECENT_DAYS} days",
    "all": "All: every job waiting to be scored",
}
ANTHROPIC_HAIKU = "anthropic:claude-haiku-5-5"
LIVE = ("pending", "running")
# The /models Save + re-run variant of the panel (specs/020 M5) posts to its own routes.
VIA_RUN_URL = {"models": "/models/screen/rescore/run"}
VIA_PANEL_URL = {"models": "/models/screen/rescore"}


def available_scorers(scoring: Any, env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """(spec, label) for the default scorer plus scorers whose key is set. Never shows keys."""
    env = os.environ if env is None else env
    problem = scoring.scorer_problems().get("screen_scorer")
    out = [
        (
            scoring.screen_scorer,
            "configured default, INVALID: see message" if problem else "configured default",
        )
    ]
    # credential_for (specs/019 S1): ANTHROPIC_AUTH_TOKEN and an SDK profile count too.
    jev = f"jev:{JEV_DEFAULT_MODEL}"
    if credential_for(jev, scoring, environ=env).status == PRESENT:
        out.append((jev, "Jev, synchronous"))
    if credential_for(ANTHROPIC_HAIKU, scoring, environ=env).status == PRESENT:
        out.append((ANTHROPIC_HAIKU, "Anthropic batch, collected later"))
    seen: set[str] = set()
    return [(s, label) for s, label in out if not (s in seen or seen.add(s))]


def wire_spec(spec: str) -> str:
    """What the page posts for ``spec``: the spec itself, or its digest label when it must
    not be shown (the server maps the label back with ``resolve_spec``)."""
    return shown_value(spec) or hidden_label(spec)


def resolve_spec(posted: str | None, scorers: list[tuple[str, str]]) -> str | None:
    """The offered scorer spec that ``posted`` (a spec or its digest label) names, or None."""
    for spec, _ in scorers:
        if posted in (spec, wire_spec(spec)):
            return spec
    return None


def _spec_problem(spec: str, field: str = "scorer", scoring: Any = None) -> str | None:
    """The one-line reason a scorer spec must not run, or None. The configured screen scorer
    goes through ``scorer_problems``, which also reports a use an invalid models.toml blocks."""
    if scoring is not None and spec == scoring.screen_scorer:
        return scoring.scorer_problems().get("screen_scorer")
    try:
        check_scorer_spec(spec, field)
    except ValueError as exc:
        return str(exc)
    return None


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


def take_run_lock(settings: Any) -> runlock.RunLock:
    """The run lock for the console's paid re-score, taken without waiting.

    Raises RescoreRefused (the panel shows it with the form, so the user can retry) when
    another jobhunter job (a timer run, a CLI score) holds it.
    """
    lock = runlock.RunLock(resolve_path(settings.paths.db_path), "console re-score")
    try:
        lock.acquire(None)
    except runlock.LockHeld as exc:
        raise rs.RescoreRefused(
            f"{exc}. Re-score not started; nothing spent. Try again when it finishes."
        ) from exc
    return lock


class RescoreManager:
    """One background run at a time, in this process."""

    def __init__(self, work: WorkRegistry) -> None:
        self._lock = threading.Lock()
        # The console's work registry (019 S12b): a run is registered before its thread starts
        # and leaves when the thread ends, so a console Restart waits for it.
        self.work = work
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
        token: str | None,
        now: Callable[[], datetime],
        scorer_factory: rs.ScorerFactory | None,
        client_factory: rs.ClientFactory | None,
    ) -> int:
        """Start a run of the confirmed plan and return its request id.

        ``token`` is the token of the plan the user confirmed. When the plan built now differs
        (new jobs, a new profile, other costs), nothing runs: PlanChanged carries the new plan.
        Raises RescoreBusy / RescoreRefused / PlanChanged, or Restarting once a console
        restart was accepted.
        """
        with self._lock:
            if self.work.restarting:
                raise Restarting()
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
            if not token or token != plan.token:
                raise rs.PlanChanged(plan)
            # The paid part holds the run lock (specs/018 Run lock, C5), never waiting for it:
            # a request thread must not block, and a timer job may hold it for hours.
            token = self.work.enter("a re-score")  # before the run lock and the DB row
            try:
                lock = take_run_lock(settings)
            except BaseException:
                self.work.leave(token)
                raise
            try:
                if request_id is not None:
                    rs.attach_plan(conn, request_id, plan)
                    rid = request_id
                else:
                    rid = rs.create_request(conn, profile, plan, now())
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
                        lock,
                        token,
                    ),
                    name=f"rescore-{rid}",
                    daemon=True,
                )
                self._thread.start()
            except BaseException:
                lock.release()
                self.work.leave(token)
                raise
            return rid

    def _work(
        self,
        conn_factory,
        rid,
        profile,
        settings,
        spec,
        now,
        scorer_factory,
        client_factory,
        pre,
        lock,
        token,
    ):
        try:
            RescoreManager._run(
                conn_factory, rid, profile, settings, spec, now, scorer_factory, client_factory, pre
            )
        finally:
            lock.release()
            self.work.leave(token)

    @staticmethod
    def _run(conn_factory, rid, profile, settings, spec, now, scorer_factory, client_factory, pre):
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
    app.state.rescore = RescoreManager(app.state.work)
    app.state.rescore_scorer_factory = None  # tests inject a fake; None builds the real scorer
    app.state.rescore_client_factory = None
    templates.env.filters["localtime"] = local_time
    templates.env.filters["scorer_label"] = lambda spec: (
        shown_value(spec) or hidden_label(str(spec))
    )

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
        request_id: int | None = None,
        fixed: str | None = None,
        via: str = "",
    ) -> dict[str, Any]:
        """The panel's context. ``fixed`` (with ``via="models"``) is the /models Save + re-run
        variant (specs/020 M5): the scorer is the model just saved, with no picker, and the
        form posts to the /models route that checks it is still the saved value."""
        settings = request.app.state.settings
        st = state_of(request)
        if fixed is not None:
            scorers = [(fixed, "saved on /models")]
            spec = fixed
            request_id = None  # queued requests are run from /prefs
        else:
            scorers = available_scorers(settings.scoring)
            spec = resolve_spec(scorer, scorers) or scorers[0][0]
        scope = scope if scope in rs.SCOPES else "recent"
        queued = rs.get_request(conn, request_id) if request_id is not None else None
        if queued is not None and queued["status"] != "pending":
            queued = None
        if queued is not None:
            scope = queued["scope"]  # running a queued request plans its own scope
        running = rs.running_request(conn, now())
        plan = notice = None
        if fixed is not None and not usable_model_text(fixed):
            # Never echo a key-shaped value (heading, dialog, field or a problem message).
            problem = (
                "The saved screening model cannot be shown here, so it is not re-scored from "
                "this page. Choose another model above, or use `jobhunter score --rescore`."
            )
        else:
            problem = _spec_problem(spec, scoring=settings.scoring)
        if problem:
            message = message or problem
        if st.kind == "ok" and running is None and not problem:
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
            "problem": problem,
            "scorers": [(wire_spec(sp), label) for sp, label in scorers],
            "scorer_wire": wire_spec(spec),
            "scope_labels": SCOPE_LABELS,
            "running": running,
            "queued": queued,
            "shown": shown,
            "message": message,
            "pending": conn.execute(
                "SELECT * FROM rescore_request WHERE status = 'pending' ORDER BY id DESC"
            ).fetchall(),
            "via": via,
            "fixed": fixed is not None,
            "scorer_shown": shown_value(spec) or (hidden_label(spec) if fixed is not None else ""),
            "run_url": VIA_RUN_URL.get(via, "/prefs/rescore/run"),
            "panel_url": VIA_PANEL_URL.get(via, "/prefs/rescore/panel"),
        }

    def render_panel(request: Request, conn: sqlite3.Connection, **kw: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_rescore_panel.html", {"rs": panel_ctx(request, conn, **kw)}
        )

    # The prefs page embeds the same context.
    app.state.rescore_panel_context = panel_ctx

    @app.get("/prefs/rescore/panel", response_class=HTMLResponse)
    def panel(
        request: Request,
        conn: Conn,
        scope: str | None = None,
        scorer: str | None = None,
        request_id: str | None = None,
    ) -> HTMLResponse:
        rid = int(request_id) if request_id and request_id.isdigit() else None
        return render_panel(request, conn, scope=scope, scorer=scorer, request_id=rid)

    @app.post("/prefs/rescore/run", response_class=HTMLResponse)
    async def run(request: Request, conn: Conn) -> HTMLResponse:
        from jobhunter.console import prefs as pf

        form = pf.parse_form(await request.body())
        scope = pf.form_value(form, "scope", "recent")
        spec_in = pf.form_value(form, "scorer", "")
        raw_id = pf.form_value(form, "request_id", "")
        token = pf.form_value(form, "plan", "")
        queued_id = int(raw_id) if raw_id.isdigit() else None
        settings = request.app.state.settings
        if state_of(request).kind != "ok":
            return render_panel(request, conn, message="Fix the preferences file first.")
        spec = resolve_spec(spec_in, available_scorers(settings.scoring)) or (
            settings.scoring.screen_scorer
        )
        return run_confirmed(request, conn, scope=scope, spec=spec, token=token,
                             queued_id=queued_id)  # fmt: skip

    def run_confirmed(
        request: Request,
        conn: sqlite3.Connection,
        *,
        scope: str,
        spec: str,
        token: str,
        queued_id: int | None = None,
        **variant: Any,
    ) -> HTMLResponse:
        """Start the run the user confirmed (``token`` is that plan's), through the one
        ``RescoreManager``: single flight, the run lock, the plan's cap check and ``max_usd``.
        ``variant`` (``fixed``, ``via``) renders the /models panel instead of the /prefs one."""
        settings = request.app.state.settings
        st = state_of(request)
        if st.kind != "ok":
            return render_panel(request, conn, message="Fix the preferences file first.",
                                **variant)  # fmt: skip
        if problem := _spec_problem(spec, scoring=settings.scoring):
            return render_panel(request, conn, scope=scope, scorer=spec, message=problem,
                                **variant)  # fmt: skip
        try:
            rid = request.app.state.rescore.start(
                conn,
                request.app.state.conn_factory,
                st.profile,
                settings,
                scope=scope,
                spec=spec,
                request_id=queued_id,
                token=token,
                now=now,
                scorer_factory=request.app.state.rescore_scorer_factory,
                client_factory=request.app.state.rescore_client_factory,
            )
        except Restarting as exc:
            return refused(
                render_panel(request, conn, scope=scope, scorer=spec, message=str(exc), **variant)
            )
        except rs.RescoreBusy as exc:
            return render_panel(
                request, conn, message=f"{exc} Showing its progress.", active_id=exc.request_id,
                **variant,
            )  # fmt: skip
        except (rs.RescoreRefused, ScorerError) as exc:
            return render_panel(
                request, conn, scope=scope, scorer=spec, message=str(exc), request_id=queued_id,
                **variant,
            )  # fmt: skip
        return render_panel(request, conn, active_id=rid, scope=scope, scorer=spec, **variant)

    app.state.rescore_run_confirmed = run_confirmed
    app.state.rescore_render_panel = render_panel

    @app.get("/prefs/rescore/progress/{request_id}", response_class=HTMLResponse)
    def progress(request: Request, conn: Conn, request_id: int, via: str = "") -> HTMLResponse:
        rs.running_request(conn, now())  # a run whose thread died is shown as interrupted
        row = rs.get_request(conn, request_id)
        via = via if via in VIA_RUN_URL else ""
        if request.headers.get("hx-request") and (row is None or row["status"] not in LIVE):
            # The poll saw the run end: redraw the whole panel (the form comes back and the
            # "a re-score is running" line goes), not just this fragment. From /models, the
            # /models variant for the screening model in effect now.
            variant: dict[str, Any] = {}
            if via:
                variant = {"fixed": request.app.state.settings.scoring.screen_scorer, "via": via}
            response = render_panel(request, conn, active_id=request_id, **variant)
            response.headers["HX-Retarget"] = "#rescore-panel"
            response.headers["HX-Reswap"] = "innerHTML"
            return response
        return templates.TemplateResponse(request, "_rescore_progress.html", {"r": row, "via": via})

    @app.post("/prefs/rescore/{request_id}/cancel", response_class=HTMLResponse)
    def cancel(request: Request, conn: Conn, request_id: int) -> HTMLResponse:
        rs.cancel_request(conn, request_id, now())
        return render_panel(request, conn)
