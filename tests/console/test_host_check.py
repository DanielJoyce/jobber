"""The loopback-Host check on every request (specs/017 phase 1d; 018 C1).

A DNS-rebinding page (a hostile name resolving to 127.0.0.1) sends its GETs with
``Host: <its name>`` and, being plain http on a non-loopback name, no ``Origin`` and no
``Sec-Fetch-*`` (checked in Chromium: tests/e2e/test_rebinding.py). So the Host alone must
refuse it, whatever other headers the request carries, unless ``--allow-remote``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_packet_pages import GH, client, db_path, new_packet, pages, pdir  # noqa: F401

from jobhunter.config import Paths, Settings
from jobhunter.console.app import create_app, cross_site_reason
from jobhunter.core import db

LOOP = "127.0.0.1:8808"


@pytest.fixture
def pid(client):  # noqa: F811
    return new_packet(client, url=GH, employer="Acme", title="Systems Engineer")


def get(client, path, host, **headers):  # noqa: F811
    return client.get(path, headers={"host": host, **headers})


def test_rebinding_get_without_any_fetch_metadata_is_refused(client, pid):  # noqa: F811
    # exactly what Chromium sends from http://evil.example:8808: Host and nothing else
    r = get(client, f"/packet/{pid}", "evil.example:8808")
    assert r.status_code == 403 and "loopback" in r.text
    assert "Systems Engineer" not in r.text


@pytest.mark.parametrize("path", ["/prefs", "/inbox", "/pipeline", "/healthz", "/static/app.css"])
@pytest.mark.parametrize("headers", [{}, {"sec-fetch-site": "same-origin"}, {"origin": "x"}])
def test_every_route_is_checked_whatever_the_headers(client, path, headers):  # noqa: F811
    assert get(client, path, "evil.example:8808", **headers).status_code == 403, path


def test_testserver_host_is_refused_like_any_other_name(pdir, db_path, pid):  # noqa: F811
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    c = TestClient(create_app(settings, lambda: db.connect(db_path)), base_url="http://testserver")
    assert c.get(f"/packet/{pid}").status_code == 403


def test_loopback_hosts_pass_even_from_another_site(client, pid):  # noqa: F811
    for host in (LOOP, "localhost:8808", "[::1]:8808", "127.0.0.1"):
        r = get(client, f"/packet/{pid}", host, **{"sec-fetch-site": "cross-site"})
        assert r.status_code == 200, host  # a link followed from your mail, say


def test_allow_remote_accepts_another_host_for_reads(pdir, db_path, pid):  # noqa: F811
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    app = create_app(settings, lambda: db.connect(db_path), allow_remote=True)
    c = TestClient(app, base_url="http://192.0.2.10:8808")
    assert c.get(f"/packet/{pid}").status_code == 200


def test_writes_keep_the_same_origin_rule():
    h = {"host": LOOP, "origin": f"http://{LOOP}", "sec-fetch-site": "same-origin"}
    assert cross_site_reason("POST", h) is None
    assert cross_site_reason("POST", {**h, "sec-fetch-site": "cross-site"}) is not None
    assert cross_site_reason("GET", {**h, "sec-fetch-site": "cross-site"}) is None
    assert cross_site_reason("GET", {"host": "evil.example:8808"}) is not None
    assert cross_site_reason("GET", {"host": "evil.example:8808"}, allow_remote=True) is None
    assert cross_site_reason("GET", {}) is not None  # no Host at all
