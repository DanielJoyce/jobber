from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter.cli import app as cli_app
from jobhunter.config import Settings
from jobhunter.console.app import NAV, PAGES, check_host, create_app
from jobhunter.core import db


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "t.db"
    return TestClient(create_app(Settings(), lambda: db.connect(path)))


ROUTES = [p for p, _, _ in PAGES if p not in ("/", "/inbox", "/pipeline", "/followups")]
# /, /inbox, /job, /pipeline and /followups have their own tests


@pytest.mark.parametrize("route", ROUTES)
def test_placeholder_routes(client, route):
    r = client.get(route)
    assert r.status_code == 200
    assert "Coming soon" in r.text
    for _, label in NAV:
        assert f">{label}</a>" in r.text
    assert "htmx-2.0.4.min.js" in r.text


def test_today_is_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="map"' in r.text
    for _, label in NAV:
        assert f">{label}</a>" in r.text
    assert "htmx-2.0.4.min.js" in r.text


def test_healthz(client, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    expected = db.current_version(conn)
    body = client.get("/healthz").json()
    assert body == {"ok": True, "schema_version": expected}
    assert expected > 0


def test_css_tokens(client):
    css = client.get("/static/app.css").text
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css
    assert "--surface-1: #1a1a19" in css


def test_static_js_and_htmx(client):
    r = client.get("/static/vendor/htmx-2.0.4.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:200]
    assert "registerKeys" in client.get("/static/app.js").text


def test_check_host():
    check_host("127.0.0.1")
    check_host("localhost")
    check_host("::1")
    with pytest.raises(ValueError):
        check_host("0.0.0.0")
    with pytest.raises(ValueError):
        check_host("192.168.1.5")
    check_host("0.0.0.0", allow_remote=True)


def test_cli_refuses_remote():
    res = CliRunner().invoke(cli_app, ["console", "--host", "0.0.0.0"])
    assert res.exit_code == 2
    assert "--allow-remote" in res.output
