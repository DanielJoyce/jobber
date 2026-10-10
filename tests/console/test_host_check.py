"""The loopback-Host check on every method (specs/017 "Local API security", phase 1d; 018 C1).

A DNS-rebinding page (a hostile name resolving to 127.0.0.1) sends ``Sec-Fetch-Site`` on its
GETs, so a GET of a packet under a non-loopback Host must be refused unless ``--allow-remote``.
Requests without browser fetch metadata (curl, the CLI, TestClient) pass as before.
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


def test_rebinding_get_of_a_packet_is_refused(client, pid):  # noqa: F811
    r = get(client, f"/packet/{pid}", "evil.example:8808", **{"sec-fetch-site": "same-origin"})
    assert r.status_code == 403 and "loopback" in r.text
    assert "Systems Engineer" not in r.text


@pytest.mark.parametrize("path", ["/prefs", "/inbox", "/pipeline", "/static/app.css"])
def test_every_route_is_checked(client, path):  # noqa: F811
    r = get(client, path, "evil.example:8808", **{"sec-fetch-site": "none"})
    assert r.status_code == 403, path


def test_testserver_host_with_fetch_metadata_is_refused(client, pid):  # noqa: F811
    r = client.get(f"/packet/{pid}", headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 403  # TestClient's Host is "testserver"


def test_loopback_gets_pass_even_from_another_site(client, pid):  # noqa: F811
    for host in (LOOP, "localhost:8808", "[::1]:8808"):
        r = get(client, f"/packet/{pid}", host, **{"sec-fetch-site": "cross-site"})
        assert r.status_code == 200, host  # a link followed from your mail, say


def test_requests_without_fetch_metadata_pass_as_before(client, pid):  # noqa: F811
    assert client.get(f"/packet/{pid}").status_code == 200


def test_allow_remote_accepts_another_host_for_reads(pdir, db_path, pid):  # noqa: F811
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    app = create_app(settings, lambda: db.connect(db_path), allow_remote=True)
    c = TestClient(app)
    r = get(c, f"/packet/{pid}", "evil.example:8808", **{"sec-fetch-site": "same-origin"})
    assert r.status_code == 200


def test_writes_keep_the_same_origin_rule():
    h = {"host": LOOP, "origin": f"http://{LOOP}", "sec-fetch-site": "same-origin"}
    assert cross_site_reason("POST", h) is None
    assert cross_site_reason("POST", {**h, "sec-fetch-site": "cross-site"}) is not None
    assert cross_site_reason("GET", {**h, "sec-fetch-site": "cross-site"}) is None
    assert cross_site_reason("GET", {**h, "host": "evil.example:8808"}) is not None
    assert cross_site_reason("GET", {"host": "evil.example:8808"}) is None  # no metadata
