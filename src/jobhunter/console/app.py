"""FastAPI console app factory (specs/007-console-and-tracking.md)."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunter.config import Settings
from jobhunter.console import (
    alerts_routes,
    detail_routes,
    inbox_routes,
    pages_routes,
    prefs_routes,
    proposals_routes,
    rejections_routes,
    rescore_routes,
    tracking_routes,
)
from jobhunter.console import dashboard as dash
from jobhunter.core import bucketnames, db, geo, rejections
from jobhunter.scoring.profile import Profile, ProfileError, load_profile_for

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

_static_versions: dict[tuple[str, int], str] = {}


def static_url(path: str) -> str:
    """``/static/<path>?v=<content hash>`` so a plain reload picks up changed CSS/JS.

    Why: browsers cached the old inbox.css after the bulk-select change and the new row
    markup rendered with stale styles until a hard refresh. The hash is cached per mtime.
    """
    file = STATIC_DIR / path
    try:
        mtime = file.stat().st_mtime_ns
    except OSError:
        return f"/static/{path}"
    key = (path, mtime)
    if key not in _static_versions:
        _static_versions[key] = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={_static_versions[key]}"


ConnFactory = Callable[[], sqlite3.Connection]
ProfileLoader = Callable[[], Profile]
Clock = Callable[[], datetime]

# (path, nav label or None when not in the nav, page heading)
PAGES: list[tuple[str, str | None, str]] = [
    ("/", "Today", "Today"),
    ("/inbox", "Inbox", "Inbox"),
    ("/pipeline", "Pipeline", "Pipeline"),
    ("/followups", "Follow-ups", "Follow-ups"),
    ("/rejections", "Employer rejections", "Employer rejections"),
    ("/sources", "Sources", "Sources"),
    ("/rejected", None, "Rejected"),
    ("/search", "Search", "Search"),
    ("/costs", "Costs", "Costs"),
    ("/prefs", "Prefs", "Preferences"),
    ("/alerts", None, "Alerts"),
]
NAV = [(path, label) for path, label, _ in PAGES if label]


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_host(host: str, allow_remote: bool = False) -> None:
    """Refuse non-loopback bind hosts unless explicitly allowed (the console has no auth)."""
    if not allow_remote and not is_loopback(host):
        raise ValueError(
            f"refusing to bind the console (no auth) to non-loopback host {host!r}; "
            "pass --allow-remote to override"
        )


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request DB connection, closed after the response."""
    conn = request.app.state.conn_factory()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def default_profile_loader(settings: Settings) -> ProfileLoader:
    """Load the profile on each call; fall back to defaults so the console still renders."""

    def load() -> Profile:
        try:
            return load_profile_for(settings)
        except ProfileError as exc:
            logger.warning("console: using default profile: %s", exc)
            return Profile()

    return load


def sparkline(series: list[float | None], width: int = 120, height: int = 28) -> str:
    """SVG polyline points for a sparkline; None gaps are skipped."""
    vals = [v for v in series if v is not None]
    if len(series) < 2 or not vals:
        return ""
    top = max(vals) or 1.0
    step = width / (len(series) - 1)
    pad = 2
    pts = [
        f"{i * step:.1f},{height - pad - (v / top) * (height - 2 * pad):.1f}"
        for i, v in enumerate(series)
        if v is not None
    ]
    return " ".join(pts)


def create_app(
    settings: Settings,
    conn_factory: ConnFactory | None = None,
    profile_loader: ProfileLoader | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the console. ``conn_factory`` defaults to opening ``settings.paths.db_path``."""
    factory: ConnFactory = conn_factory or (lambda: db.connect(settings.paths.db_path))
    get_profile: ProfileLoader = profile_loader or default_profile_loader(settings)
    now: Clock = clock or (lambda: datetime.now(UTC))

    boot = factory()
    try:
        db.migrate(boot)
    finally:
        boot.close()

    app = FastAPI(title="jobhunter console", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["sparkline"] = sparkline
    templates.env.globals["static_url"] = static_url
    templates.env.globals["bucket_name"] = bucketnames.bucket_name
    templates.env.globals["group_name"] = bucketnames.group_name
    templates.env.globals["bucket_names_json"] = json.dumps(
        {b: bucketnames.bucket_name(b) for b in bucketnames.LETTERS}
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.conn_factory = factory

    @app.middleware("http")
    async def no_store_pages(request: Request, call_next):
        # Why: Back restored a cached /inbox from before a shortlist, so triaged rows came
        # back and looked untriaged. Pages are cheap local renders; never cache them.
        # Static files keep normal caching (their URLs carry a content hash).
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def render(request: Request, title: str, active: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            {"title": title, "active": active, "nav": NAV},
        )

    @app.get("/healthz")
    def healthz(conn: Conn) -> dict[str, object]:
        return {"ok": True, "schema_version": db.current_version(conn)}

    def add_page(path: str, title: str) -> None:
        def handler(request: Request) -> HTMLResponse:
            return render(request, title, path)

        app.add_api_route(path, handler, methods=["GET"], response_class=HTMLResponse)

    inbox_routes.register(app, templates, get_conn, NAV)
    tracking_routes.register(app, templates, get_conn, NAV, now)
    pages_routes.register(app, templates, get_conn, NAV, now)

    def table_context(
        conn: sqlite3.Connection,
        range_: int,
        metric: str,
        sort: str | None,
        dir_: str | None,
        buckets: tuple[str, ...] = dash.DEFAULT_GROUP,
    ) -> dict[str, object]:
        rows = dash.state_stats(conn, get_profile(), metric, range_, now(), buckets)
        sort = sort if sort in dash.SORT_KEYS else metric
        dir_ = "asc" if dir_ == "asc" or (dir_ is None and sort == "state") else "desc"
        return {
            "rows": dash.sort_rows(rows, sort, dir_),
            "range": range_,
            "metric": metric,
            "sort": sort,
            "dir": dir_,
            "metrics": dash.METRICS,
            "coverage": dash.COVERAGE,
            "buckets": list(buckets),
            "buckets_param": ",".join(buckets),
            "buckets_name": bucketnames.group_name(buckets),
        }

    def kpi_context(
        conn: sqlite3.Connection, range_: int, buckets: tuple[str, ...] = dash.DEFAULT_GROUP
    ) -> dict[str, object]:
        k = dash.kpis(conn, get_profile(), range_, now(), settings.scoring.weekly_cap_usd, buckets)
        return {
            "k": k,
            "range": range_,
            "kpi_buckets_name": bucketnames.group_name(buckets),
            "kpi_buckets_title": bucketnames.group_title(buckets),
        }

    @app.get("/", response_class=HTMLResponse)
    def today(
        request: Request,
        conn: Conn,
        range: str | None = None,
        metric: str | None = None,
        sort: str | None = None,
        dir: str | None = None,
        buckets: str | None = None,
    ) -> HTMLResponse:
        range_ = dash.clamp_range(range)
        metric_ = dash.clamp_metric(metric)
        ctx: dict[str, object] = {
            "title": "Today",
            "active": "/",
            "nav": NAV,
            "today": now(),
            "ranges": dash.RANGES,
            "bucket_letters": bucketnames.LETTERS,
            "bucket_hints": {b: v[1] for b, v in bucketnames.BUCKET_TITLES.items()},
            "tile_grid": json.dumps({k: list(v) for k, v in geo.TILE_GRID.items()}),
            "fips": json.dumps({s.fips: s.usps for s in geo.STATES}),
            "names": json.dumps({s.usps: s.name for s in geo.STATES}),
            "sankey": json.dumps(
                dash.sankey(
                    conn,
                    get_profile(),
                    extra_rejected=rejections.rejected_group_ids(conn),
                    elsewhere_rejected=rejections.unmatched_email_count(conn),
                )
            ).replace("</", "<\\/"),
            **kpi_context(conn, range_, dash.clamp_buckets(buckets)),
            **table_context(conn, range_, metric_, sort, dir, dash.clamp_buckets(buckets)),
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/api/dash/kpis")
    def api_kpis(conn: Conn, range: str | None = None, buckets: str | None = None) -> JSONResponse:
        range_ = dash.clamp_range(range)
        return JSONResponse(
            dash.kpis(
                conn,
                get_profile(),
                range_,
                now(),
                settings.scoring.weekly_cap_usd,
                dash.clamp_buckets(buckets),
            )
        )

    @app.get("/dash/kpis", response_class=HTMLResponse)
    def kpis_fragment(
        request: Request,
        conn: Conn,
        range: str | None = None,
        buckets: str | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_kpis.html",
            kpi_context(conn, dash.clamp_range(range), dash.clamp_buckets(buckets)),
        )

    @app.get("/api/dash/map")
    def api_map(
        conn: Conn,
        metric: str | None = None,
        range: str | None = None,
        buckets: str | None = None,
    ) -> JSONResponse:
        range_ = dash.clamp_range(range)
        metric_ = dash.clamp_metric(metric)
        picked = dash.clamp_buckets(buckets)
        rows, totals = dash.state_stats_with_totals(
            conn, get_profile(), metric_, range_, now(), picked
        )
        return JSONResponse(dash.map_payload(rows, metric_, range_, picked, totals))

    @app.get("/api/dash/series")
    def api_series(
        conn: Conn, range: str | None = None, buckets: str | None = None
    ) -> JSONResponse:
        return JSONResponse(
            dash.series(
                conn, get_profile(), dash.clamp_range(range), now(), dash.clamp_buckets(buckets)
            )
        )

    @app.get("/dash/state-table", response_class=HTMLResponse)
    def state_table(
        request: Request,
        conn: Conn,
        metric: str | None = None,
        range: str | None = None,
        sort: str | None = None,
        dir: str | None = None,
        buckets: str | None = None,
    ) -> HTMLResponse:
        ctx = table_context(
            conn,
            dash.clamp_range(range),
            dash.clamp_metric(metric),
            sort,
            dir,
            dash.clamp_buckets(buckets),
        )
        return templates.TemplateResponse(request, "_state_table.html", ctx)

    detail_routes.register(app, templates, get_conn, NAV, get_profile, now)
    rescore_routes.register(app, templates, get_conn, now)
    prefs_routes.register(app, templates, get_conn, NAV, now)
    proposals_routes.register(app, templates, get_conn, NAV, now)
    rejections_routes.register(app, templates, get_conn, NAV, now)
    alerts_routes.register(app, templates, get_conn, NAV, now)

    # Placeholders last, only for nav pages no module has claimed yet. New pages need no
    # edit here (this list used to conflict on every parallel console branch).
    claimed = {getattr(r, "path", None) for r in app.routes}
    app.state.placeholder_paths = [p for p, _, _ in PAGES if p not in claimed]
    for path, _, title in PAGES:
        if path not in claimed:
            add_page(path, title)

    return app
