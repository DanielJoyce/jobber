"""``/ext/v1/match`` and ``/ext/v1/match/pick`` (specs/022 "Routes", "Matching the page to a
job"). The worker supplies the tab id; nothing page-supplied can stand in for it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobhunter.apply import ext_pairing
from jobhunter.config import Console, Paths, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.pipeline.dedupe import _refresh_group
from jobhunter.pipeline.dedupe_url import normalize_apply_url

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
ORIGIN = ext_pairing.EXTENSION_ORIGIN
GH = "https://boards.greenhouse.io/acme/jobs/4012345"
EMBED = "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345"
PAGE = "https://careers.acme.example/jobs/77/apply"


@pytest.fixture
def env(tmp_path):
    pdir = tmp_path / "profile"
    pdir.mkdir()
    db_path = tmp_path / "e.db"
    c = db.connect(db_path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    c.commit()
    c.close()
    return SimpleNamespace(pdir=pdir, db_path=db_path, data=tmp_path / "data")


@pytest.fixture
def client(env):
    settings = Settings(
        paths=Paths(
            profile_dir=env.pdir,
            db_path=env.db_path,
            cache_dir=env.pdir.parent / "cache",
            data_dir=env.data,
        ),
        console=Console(port=8808),
    )
    app = create_app(settings, lambda: db.connect(env.db_path), clock=lambda: NOW)
    return TestClient(app, base_url="http://127.0.0.1:8808", follow_redirects=False)


@pytest.fixture
def conn(env):
    c = db.connect(env.db_path)
    yield c
    c.close()


@pytest.fixture
def token(client, env):
    code, _ = ext_pairing.new_code(ext_pairing.state_path(env.data), NOW)
    r = client.post(
        "/ext/v1/pair", json={"code": code}, headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def post(client, token, path, body):
    return client.post(
        f"/ext/v1/{path}",
        json=body,
        headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0", "Authorization": f"Bearer {token}"},
    )


def group(conn, url, title="Synthetic Engineer", employer="Acme Synthetic"):
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, created_at) "
        "VALUES (NULL, 1, 'exact_hash', ?)",
        (ISO,),
    ).lastrowid
    conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, job_group_id, stage, "
        "description_text, first_seen_at, last_seen_at) "
        "VALUES ('co', ?, ?, ?, ?, ?, 'grouped', 't', ?, ?)",
        (str(uuid.uuid4()), url, title, employer, gid, ISO, ISO),
    )
    _refresh_group(conn, gid)
    conn.commit()
    return gid


def test_match_needs_the_pairing(client):
    r = client.post(
        "/ext/v1/match",
        json={"frame_url": GH},
        headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"},
    )
    assert r.status_code == 401


def test_match_decides_by_a_stored_url(client, token, conn):
    gid = group(conn, EMBED)
    r = post(client, token, "match", {"frame_url": GH + "#app", "tab_id": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decided"] and body["rule"] == 3 and body["group_id"] == gid
    assert body["kind"] == "ats" and body["ats"] == "greenhouse"
    assert body["variant"] == "greenhouse_classic"
    assert body["posting_key"] == "greenhouse:acme:4012345"
    assert body["candidates"] == [
        {"group_id": gid, "title": "Synthetic Engineer", "employer": "Acme Synthetic"}
    ]


def test_match_asks_with_nothing_stored(client, token, conn):
    group(conn, "https://careers.acme.example/jobs/1")  # same employer and title: never matches
    body = post(client, token, "match", {"frame_url": GH, "tab_id": 1}).json()
    assert not body["decided"] and body["rule"] == 5 and body["candidates"] == []


def test_a_page_supplied_tab_id_is_refused(client, token):
    """Only the worker's top-level tab_id is read; any other field is a 422, so a value the
    page put in its descriptors or facts can never ride along as a tab id."""
    for extra in ({"page": {"tab_id": 7}}, {"tabId": 7}, {"sender_tab": 7}):
        r = post(client, token, "match", {"frame_url": PAGE, "tab_id": 3, **extra})
        assert r.status_code == 422, extra
    for bad in (-1, "7", 2**31):
        r = post(client, token, "match", {"frame_url": PAGE, "tab_id": bad})
        assert r.status_code == 422, bad


def test_generic_session_binds_only_the_workers_tab(client, token, conn):
    gid = group(conn, "https://board.example/job/1")
    app = conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, 'preparing', ?, ?)",
        (gid, ISO, ISO),
    ).lastrowid
    lure = PAGE + "?tab_id=7"
    sid = conn.execute(
        "INSERT INTO fill_session (application_id, ats, start_url, tab_id, host, started_at, "
        "last_seen_at) VALUES (?, 'generic', ?, 7, 'careers.acme.example', ?, ?)",
        (app, normalize_apply_url(lure), ISO, ISO),
    ).lastrowid
    conn.commit()
    other = post(client, token, "match", {"frame_url": lure, "tab_id": 3, "session_id": sid})
    assert not other.json()["decided"]
    same = post(client, token, "match", {"frame_url": lure, "tab_id": 7}).json()
    assert same["decided"] and same["rule"] == 1 and same["session_id"] == sid
    assert same["application_id"] == app and same["kind"] == "generic"


def test_pick_remembers_the_posting_and_the_next_match_uses_it(client, token, conn):
    a = group(conn, "https://careers.acme.example/jobs/1")
    body = {"action_id": str(uuid.uuid4()), "frame_url": EMBED, "group_id": a}
    r = post(client, token, "match/pick", body)
    assert r.status_code == 200, r.text
    assert r.json()["remembered"] and r.json()["posting_key"] == "greenhouse:acme:4012345"
    m = post(client, token, "match", {"frame_url": GH, "tab_id": 1}).json()
    assert (m["decided"], m["rule"], m["group_id"]) == (True, 2, a)
    # The same action again changes nothing.
    assert post(client, token, "match/pick", body).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM ats_posting_ref").fetchone()[0] == 1


def test_pick_on_a_generic_page_or_a_gone_group(client, token, conn):
    a = group(conn, "https://careers.acme.example/jobs/1")
    r = post(client, token, "match/pick",
             {"action_id": str(uuid.uuid4()), "frame_url": PAGE, "group_id": a})  # fmt: skip
    assert r.status_code == 200 and r.json()["remembered"] is False
    r = post(client, token, "match/pick",
             {"action_id": str(uuid.uuid4()), "frame_url": GH, "group_id": 999})  # fmt: skip
    assert r.status_code == 404 and r.json()["gone"] is True
    r = post(client, token, "match/pick",
             {"action_id": "not-a-uuid", "frame_url": GH, "group_id": a})  # fmt: skip
    assert r.status_code == 422
