"""The deep pass from the console (specs/020 "Deep pass from the console", M11).

``GET /deep/panel`` plans only: no network, no client, no write. ``POST /deep/run`` is the
only way a deep run starts: it needs a loopback peer (a non-loopback peer gets 403, with or
without ``--allow-remote``), the token of the plan the user confirmed, and the run lock taken
without waiting. The run happens on one background thread with its own connection, through
``deep.deep_score``, the CLI's own path; progress lives in ``deep_request``.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import ipaddress
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter import container
from jobhunter.config import resolve_path
from jobhunter.console.inbox_routes import load_console_profile
from jobhunter.core import runlock
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import deep_run as dr
from jobhunter.scoring.profile import Profile

logger = logging.getLogger(__name__)

RUN_LOCK_LABEL = "console deep pass"


def loopback_peer(request: Request) -> bool:
    """True when the TCP peer is this machine. ``--allow-remote`` does not change this: a
    deep run spends your credits, so only a browser on this machine may start one."""
    host = request.client.host if request.client else ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def require_loopback_peer(request: Request) -> None:
    if not loopback_peer(request):
        logger.warning("refused %s %s: non-loopback peer", request.method, request.url.path)
        raise HTTPException(403, "the deep pass starts only from a browser on this machine")


def cli_command(plan: "dr.DeepPlan | None", group_id: int | None, n: int) -> str:
    """The command a non-loopback viewer is given instead of a Run button."""
    base = "jobhunter-ctr" if container.is_container() else "jobhunter"
    if plan is not None and plan.mode == "group" and group_id is not None:
        return f"{base} score --deep-group {group_id}"
    return f"{base} score --deep {n}"


def take_run_lock(settings: Any) -> runlock.RunLock:
    """The run lock, never waiting: a timer run or a CLI score holding it means refused."""
    lock = runlock.RunLock(resolve_path(settings.paths.db_path), RUN_LOCK_LABEL)
    try:
        lock.acquire(None)
    except runlock.LockHeld as exc:
        raise dr.DeepRefused(
            f"{exc}. Deep pass not started; nothing spent. Try again when it finishes."
        ) from exc
    return lock


class DeepManager:
    """One deep run at a time, in this process. A re-score and a deep run never run at once:
    both hold the run lock."""

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
        mode: str,
        n: int,
        group_id: int | None,
        token: str | None,
        now: Callable[[], datetime],
        client_factory: dr.ClientFactory | None,
    ) -> int:
        """Start the confirmed plan and return its request id.

        Raises DeepBusy (one is running: a double click lands here), DeepPlanChanged (the plan
        built now is not the one confirmed, or no token), DeepRefused (the plan refuses, or
        the run lock is held). Nothing is written and no client is built in any of these.
        """
        with self._lock:
            if self.busy() and self.current is not None:
                raise dr.DeepBusy(self.current)
            live = dr.running_request(conn, now())
            if live is not None:
                raise dr.DeepBusy(int(live["id"]))
            plan = dr.make_plan(
                conn, profile, settings.scoring, mode=mode, n=n, now=now(), group_id=group_id
            )
            if not token or token != plan.token:
                raise dr.DeepPlanChanged(plan)
            if plan.refusal:
                raise dr.DeepRefused(plan.refusal)
            if not plan.group_ids:
                raise dr.DeepRefused(plan.note or "Nothing to deep-score.")
            lock = take_run_lock(settings)
            try:
                rid = dr.create_request(conn, plan, now())
                self.current = rid
                # 019 S12b's work registry is not on main yet. When it lands, the run enters
                # ``app.state.work`` here, before its thread starts, and leaves it in
                # ``_work``'s ``finally``, so a config-watch Restart answers 409 meanwhile.
                self._thread = threading.Thread(
                    target=self._work,
                    args=(conn_factory, rid, profile, settings, now, client_factory, lock),
                    name=f"deep-{rid}",
                    daemon=True,
                )
                self._thread.start()
            except BaseException:
                lock.release()
                raise
            return rid

    @staticmethod
    def _work(conn_factory, rid, profile, settings, now, client_factory, lock):
        try:
            conn = conn_factory()  # its own connection: never the request's
            try:
                dr.run_request(
                    conn, rid, profile, settings.scoring, now=now, client_factory=client_factory
                )
            except Exception as exc:
                logger.exception("deep pass #%s failed", rid)
                try:
                    conn.execute(
                        "UPDATE deep_request SET status = 'failed', error = ?, done_at = ? "
                        "WHERE id = ? AND status = 'running'",
                        (type(exc).__name__, to_iso(now()), rid),
                    )
                except sqlite3.Error:
                    logger.exception("could not record the deep pass failure")
            finally:
                conn.close()
        finally:
            lock.release()


def _int(value: Any, default: int | None) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else default


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    app.state.deep = DeepManager()
    app.state.deep_client_factory = None  # tests inject a fake; None builds anthropic.Anthropic
    templates.env.globals["deep_model_label"] = dr.model_label

    def panel_ctx(
        request: Request,
        conn: sqlite3.Connection,
        *,
        mode: str,
        n: int,
        group_id: int | None,
        message: str | None = None,
        active_id: int | None = None,
        plan: "dr.DeepPlan | None" = None,
    ) -> dict[str, Any]:
        settings = request.app.state.settings
        profile, error = load_console_profile(settings)
        running = dr.running_request(conn, now())
        if profile is not None and plan is None and running is None:
            plan = dr.make_plan(
                conn, profile, settings.scoring, mode=mode, n=n, now=now(), group_id=group_id
            )
        shown = running
        if shown is None and active_id is not None:
            shown = dr.get_request(conn, active_id)
        remote = not loopback_peer(request)
        return {
            "plan": plan,
            "mode": plan.mode if plan else mode,
            "n": plan.n if plan else n,
            "group_id": group_id,
            "n_choices": [c for c in dr.N_CHOICES if c <= settings.scoring.deep_shortlist]
            or [settings.scoring.deep_shortlist],
            "model": dr.model_label(settings.scoring.deep_scorer),
            "profile_error": error,
            "running": running,
            "shown": shown,
            "message": message,
            "remote": remote,
            "command": cli_command(plan, group_id, n),
        }

    def render_panel(request: Request, conn: sqlite3.Connection, **kw: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_deep_panel.html", {"dp": panel_ctx(request, conn, **kw)}
        )

    @app.get("/deep/panel", response_class=HTMLResponse)
    def panel(
        request: Request,
        conn: Conn,
        n: str | None = None,
        mode: str | None = None,
        group: str | None = None,
    ) -> HTMLResponse:
        group_id = _int(group, None)
        mode_ = "group" if group_id is not None else (mode if mode in ("next", "rerun") else "next")
        return render_panel(
            request, conn, mode=mode_, n=_int(n, dr.DEFAULT_N) or dr.DEFAULT_N, group_id=group_id
        )

    @app.post("/deep/run", response_class=HTMLResponse)
    async def run(request: Request, conn: Conn) -> HTMLResponse:
        require_loopback_peer(request)
        from jobhunter.console import prefs as pf

        form = pf.parse_form(await request.body())
        group_id = _int(pf.form_value(form, "group"), None)
        mode = "group" if group_id is not None else pf.form_value(form, "mode", "next")
        n = _int(pf.form_value(form, "n"), dr.DEFAULT_N) or dr.DEFAULT_N
        token = pf.form_value(form, "plan", "")
        settings = request.app.state.settings
        profile, error = load_console_profile(settings)
        kw: dict[str, Any] = {"mode": mode, "n": n, "group_id": group_id}
        if profile is None:
            return render_panel(
                request, conn, message=f"Fix the preferences file first: {error}", **kw
            )
        try:
            rid = request.app.state.deep.start(
                conn,
                request.app.state.conn_factory,
                profile,
                settings,
                mode=mode,
                n=n,
                group_id=group_id,
                token=token,
                now=now,
                client_factory=request.app.state.deep_client_factory,
            )
        except dr.DeepBusy as exc:
            return render_panel(
                request,
                conn,
                message=f"{exc} Showing its progress.",
                active_id=exc.request_id,
                **kw,
            )
        except dr.DeepPlanChanged as exc:
            return render_panel(request, conn, message=str(exc), plan=exc.plan, **kw)
        except dr.DeepRefused as exc:
            return render_panel(request, conn, message=str(exc), **kw)
        return render_panel(request, conn, active_id=rid, **kw)

    @app.get("/deep/{request_id}/progress", response_class=HTMLResponse)
    def progress(request: Request, conn: Conn, request_id: int) -> HTMLResponse:
        dr.running_request(conn, now())  # a run whose thread died is shown as interrupted
        row = dr.get_request(conn, request_id)
        return templates.TemplateResponse(
            request,
            "_deep_progress.html",
            {"r": row, "remote": not loopback_peer(request)},
        )

    @app.post("/deep/{request_id}/cancel", response_class=HTMLResponse)
    def cancel(request: Request, conn: Conn, request_id: int) -> HTMLResponse:
        require_loopback_peer(request)
        dr.request_cancel(conn, request_id, now())
        row = dr.get_request(conn, request_id)
        return templates.TemplateResponse(
            request,
            "_deep_progress.html",
            {"r": row, "remote": False},
        )
