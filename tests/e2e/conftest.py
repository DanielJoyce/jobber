"""Browser fixtures: a uvicorn-served console on a temp DB, chromium locked to localhost."""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest
import uvicorn
from fastapi.responses import HTMLResponse
from playwright.sync_api import Browser, sync_playwright

from jobhunter.config import Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

from . import seed as seeding

ARTIFACTS = Path(__file__).parent / "artifacts"
# Resolved at import, before the autouse fixture in tests/conftest.py moves HOME to a tmp dir:
# a browser launched by the code under test (the PDF export) must still find its Chromium.
_BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
    Path(os.path.expanduser("~")) / ".cache" / "ms-playwright"
)


@pytest.fixture(autouse=True)
def _real_browsers_path(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _BROWSERS)


@dataclass
class Server:
    url: str
    db_path: Path
    ids: dict[str, int]
    app: object = None  # the FastAPI app, for tests that inject fakes into app.state

    def rows(self, sql: str, params=()):
        return seeding.query(self.db_path, sql, params)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e.db"
    ids = seeding.seed(db_path, f"{base}/__employer")
    profile_dir = seeding.write_profile(tmp_path)
    settings = Settings.model_validate(
        {
            "paths": {
                "profile_dir": str(profile_dir),
                "db_path": str(db_path),
                "cache_dir": str(tmp_path / "cache"),
                "data_dir": str(tmp_path),
            },
            # A name no real provider answers to: a test that reaches a scorer must inject one.
            "scoring": {"screen_scorer": "test:model"},
        }
    )
    app = create_app(settings, lambda: db.connect(db_path))

    @app.get("/__employer", response_class=HTMLResponse)
    def employer() -> str:
        return "<!doctype html><title>Employer</title><h1>Dummy employer page</h1>"

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    yield Server(base, db_path, ids, app)
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="session")
def pw():
    """One Playwright driver per session: a second sync_playwright() in the same process fails
    while the first is running (the extension tests launch their own browsers from it)."""
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(pw):
    b = pw.chromium.launch()
    yield b
    b.close()


@pytest.fixture
def page(browser: Browser, request):
    """A page whose context aborts (and records) every non-localhost request."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, color_scheme="light")
    blocked: list[str] = []

    def guard(route):
        if urlparse(route.request.url).hostname == "127.0.0.1":
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    ctx.route("**/*", guard)
    ctx.tracing.start(screenshots=True, snapshots=True)
    pg = ctx.new_page()
    pg.errors = []  # console errors and uncaught exceptions
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        ARTIFACTS.mkdir(exist_ok=True)
        stem = request.node.name
        pg.screenshot(path=str(ARTIFACTS / f"{stem}.png"))
        ctx.tracing.stop(path=str(ARTIFACTS / f"{stem}.zip"))
    else:
        ctx.tracing.stop()
    ctx.close()
    assert not blocked, f"non-localhost requests attempted: {blocked}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    return rep
