"""Console responses refuse to be framed (clickjacking of the paid buttons)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Settings
from jobhunter.console.app import create_app, with_frame_ancestors
from jobhunter.core import db


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "t.db"
    settings = Settings(paths=Paths(profile_dir=tmp_path / "profile", db_path=path))
    return TestClient(create_app(settings, lambda: db.connect(path)))


def assert_denied(r):
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_html_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert_denied(r)


def test_htmx_fragment(client):
    r = client.get("/dash/kpis", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert_denied(r)


def test_error_responses(client):
    assert_denied(client.get("/no-such-page"))
    refused = client.post("/prefs", headers={"Origin": "http://evil.example"})
    assert refused.status_code == 403
    assert_denied(refused)


def test_json(client):
    assert_denied(client.get("/healthz"))


def test_merges_into_existing_csp():
    merged = with_frame_ancestors("default-src 'none'; style-src 'unsafe-inline'")
    assert merged == "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    assert with_frame_ancestors("frame-ancestors *; img-src 'self'") == (
        "img-src 'self'; frame-ancestors 'none'"
    )
    assert with_frame_ancestors(None) == "frame-ancestors 'none'"
