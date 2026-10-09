"""Routes for /sources, /rejected, /search and /costs (specs/007)."""

# No ``from __future__ import annotations``: FastAPI must resolve the local ``Conn`` alias.
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.console import pages
from jobhunter.console.inbox_routes import load_console_profile
from jobhunter.sources.registry import load_registry


def _float(value: str) -> float | None:
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def render(request: Request, name: str, title: str, active: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"title": title, "active": active, "nav": nav, **ctx}
        )

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, conn: Conn) -> HTMLResponse:
        # Tests may inject app.state.registry_loader to avoid depending on the packaged registry.
        loader = getattr(app.state, "registry_loader", load_registry)
        rows = pages.source_health(conn, loader())
        return render(
            request,
            "sources.html",
            "Sources",
            "/sources",
            sources=rows,
            non_ok=sum(1 for s in rows if s.status != "ok"),
            runs=pages.recent_runs(conn),
        )

    @app.get("/rejected", response_class=HTMLResponse)
    def rejected_page(request: Request, conn: Conn, reason: str | None = None) -> HTMLResponse:
        profile, error = load_console_profile(request.app.state.settings)
        reason = (reason or "").strip() or None
        data = pages.rejected(conn, profile, reason) if profile is not None else None
        return render(
            request,
            "rejected.html",
            "Rejected",
            "/rejected",
            data=data,
            reason=reason,
            profile_error=error,
        )

    @app.get("/search", response_class=HTMLResponse)
    def search_page(
        request: Request,
        conn: Conn,
        q: str = "",
        state: str = "",
        source_class: str = "",
        bucket_from: str = "",
        bucket_to: str = "",
        salary_floor: str = "",
        posted_after: str = "",
    ) -> HTMLResponse:
        profile, _ = load_console_profile(request.app.state.settings)
        result = pages.search(
            conn,
            profile,
            q,
            state=state.strip().upper() or None,
            source_class=source_class.strip().upper() or None,
            bucket_from=bucket_from.strip().upper() or None,
            bucket_to=bucket_to.strip().upper() or None,
            salary_floor=_float(salary_floor),
            posted_after=posted_after.strip() or None,
        )
        return render(
            request,
            "search.html",
            "Search",
            "/search",
            result=result,
            q=q,
            state=state.strip().upper(),
            source_class=source_class.strip().upper(),
            bucket_from=bucket_from.strip().upper(),
            bucket_to=bucket_to.strip().upper(),
            salary_floor=salary_floor,
            posted_after=posted_after,
            buckets=pages.BUCKETS,
        )

    @app.get("/costs", response_class=HTMLResponse)
    def costs_page(request: Request, conn: Conn, days: int = 30) -> HTMLResponse:
        days = days if days in pages.RANGES else 30
        scoring = request.app.state.settings.scoring
        data = pages.costs(conn, now(), days, scoring.daily_cap_usd, scoring.weekly_cap_usd)
        return render(request, "costs.html", "Costs", "/costs", c=data, ranges=pages.RANGES)
