"""Shared plumbing for htmlconfig tests: a recorded fake site behind httpx.MockTransport."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from jobhunter.config import Settings
from jobhunter.core.fetch import FetchContext
from jobhunter.core.models import SourceRow
from jobhunter.sources.registry import load_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "htmlconfig"
ALLOW_ALL = "User-agent: *\nAllow: /\n"
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Site:
    """Serves ``robots`` at /robots.txt and defers everything else to ``handler``."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        robots: str = ALLOW_ALL,
    ) -> None:
        self.handler = handler
        self.robots = robots
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        self.requests.append(request)
        return self.handler(request)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def make_ctx(src: SourceRow, conn, site: Site, tmp_path) -> FetchContext:
    clock = Clock()
    settings = Settings.model_validate({"paths": {"cache_dir": str(tmp_path / "cache")}})
    return FetchContext(
        src,
        settings,
        conn,
        transport=httpx.MockTransport(site.handle),
        clock=clock,
        sleep=clock.sleep,
    )


def registry_row(key: str, **overrides) -> SourceRow:
    row = next(r for r in load_registry() if r.key == key)
    return row.model_copy(update=overrides) if overrides else row


def fixture_text(board: str, name: str) -> str:
    return (FIXTURES / board / name).read_text()
