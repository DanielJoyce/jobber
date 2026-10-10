from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter.cli import app as cli_app
from jobhunter.config import Paths, Settings
from jobhunter.console.app import NAV, check_host, create_app, static_url
from jobhunter.core import db


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "t.db"
    settings = Settings(paths=Paths(profile_dir=tmp_path / "profile", db_path=path))
    return TestClient(create_app(settings, lambda: db.connect(path)))


def placeholder_paths() -> list[str]:
    """Nav pages no route module has claimed yet; they must render the placeholder."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.model_validate({"paths": {"db_path": f"{tmp}/p.db"}})
        return list(create_app(settings).state.placeholder_paths)


# Pages that still render the placeholder: whatever no route module has claimed.
ROUTES = placeholder_paths()


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


def test_static_urls_are_content_versioned(client):
    # A plain reload must pick up changed CSS/JS; stale inbox.css once broke the row layout.
    url = static_url("app.css")
    assert re.fullmatch(r"/static/app\.css\?v=[0-9a-f]{10}", url)
    page = client.get("/").text
    assert url in page
    assert client.get(url).status_code == 200
    assert static_url("no-such-file.css") == "/static/no-such-file.css"


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


def test_static_version_follows_file_content(tmp_path, monkeypatch):
    # An edited stylesheet must get a new ?v= without a restart (stale-CSS bug).
    import os

    from jobhunter.console import app as app_mod

    monkeypatch.setattr(app_mod, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "_static_versions", {})
    css = tmp_path / "x.css"
    css.write_text("a { color: red; }")
    first = static_url("x.css")
    assert static_url("x.css") == first  # unchanged file, same URL
    css.write_text("a { color: blue; }")
    stat = css.stat()
    os.utime(css, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert static_url("x.css") != first


@pytest.mark.parametrize(
    "path", ["/", "/inbox", "/pipeline", "/followups", "/job/1", "/api/dash/kpis", "/healthz"]
)
def test_pages_are_never_cached(client, path):
    # Back must re-fetch every page (Pipeline card positions go stale like the inbox rows do).
    assert client.get(path).headers["cache-control"] == "no-store", path


def test_pages_are_never_cached_but_static_files_are(client):
    assert client.get("/inbox").headers["cache-control"] == "no-store"
    assert "no-store" not in client.get(static_url("app.css")).headers.get("cache-control", "")


def test_every_sankey_link_resolves(client):
    # The Sankey once linked to /tracking, a page that never existed (404 on click).
    from jobhunter.console.dashboard import _SANKEY_NODES

    for _id, _label, _col, _kind, href in _SANKEY_NODES:
        if href:
            assert client.get(href.replace("{b}", "A")).status_code == 200, href
