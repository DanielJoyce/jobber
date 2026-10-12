"""The nav activity indicator: GET /activity fragment and its hook in base.html (bug edb9ba6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Settings
from jobhunter.console.app import NAV, create_app
from jobhunter.core import db, runlock
from jobhunter.pipeline.listing import to_iso

NOW = datetime.now(UTC)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


@pytest.fixture
def client(tmp_path, db_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = Settings(paths=Paths(profile_dir=profile, db_path=db_path))
    return TestClient(create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW))


def add(db_path, status, **kw):
    cols = {
        "requested_at": to_iso(NOW),
        "scope": "recent",
        "scoring_version": "v",
        "job_count": 1,
        "cost_per_job_usd": 0.0,
        "estimated_usd": 0.0,
        "status": status,
        **kw,
    }
    c = db.connect(db_path)
    cur = c.execute(
        f"INSERT INTO rescore_request ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )
    c.close()
    return cur.lastrowid


def test_idle_fragment_is_empty_and_polls_slowly(client):
    r = client.get("/activity")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert 'id="activity"' in r.text and 'class="activity"' in r.text
    assert "every 60s" in r.text
    assert "Re-scor" not in r.text and "Run in progress" not in r.text
    assert r.text.rstrip().endswith("></div>")  # no children: CSS :empty hides it


def test_running_rescore_shows_progress_and_polls_fast(client, db_path):
    add(
        db_path,
        "running",
        scorer="jev:some/model",
        total=569,
        scored=120,
        started_at=to_iso(NOW),
        heartbeat_at=to_iso(NOW),
    )
    r = client.get("/activity")
    assert "Re-scoring 120/569 with jev:some/model" in r.text
    assert 'href="/prefs#sec-rescore"' in r.text
    assert "every 3s" in r.text


def test_stale_running_rescore_is_not_shown_as_running(client, db_path):
    old = to_iso(NOW - timedelta(hours=1))
    add(db_path, "running", scorer="x", total=5, scored=1, started_at=old, heartbeat_at=old)
    r = client.get("/activity")
    assert "Re-scoring" not in r.text
    assert "every 60s" in r.text


def test_pending_request_is_shown_without_fast_polling(client, db_path):
    add(db_path, "pending")
    r = client.get("/activity")
    assert "Re-score queued" in r.text
    assert "every 60s" in r.text


def test_run_in_progress_when_another_process_holds_the_lock(client, db_path, run_lock_holder):
    holder = run_lock_holder(db_path)
    r = client.get("/activity")
    assert "Run in progress" in r.text
    assert "every 3s" in r.text
    holder.kill()
    assert "Run in progress" not in client.get("/activity").text


def test_probe_does_not_take_the_lock(client, db_path):
    client.get("/activity")
    lock = runlock.RunLock(db_path, "t")
    lock.acquire(None)  # still free after probes
    lock.release()


def test_done_state_and_dismiss(client, db_path):
    rid = add(
        db_path,
        "done",
        scorer="jev:m",
        total=569,
        scored=560,
        cost_usd=0.07,
        done_at=to_iso(NOW - timedelta(seconds=5)),
    )
    r = client.get("/activity")
    assert "Re-score done: 560 scored, $0.07" in r.text
    assert "dashboard updated" in r.text
    assert "every 60s" in r.text
    assert "Re-score" not in client.get(f"/activity?dismissed={rid}").text


def test_old_finished_request_is_not_announced(client, db_path):
    add(db_path, "done", scored=5, done_at=to_iso(NOW - timedelta(hours=2)))
    assert "Re-score done" not in client.get("/activity").text


def test_failed_state_shows_short_reason(client, db_path):
    add(db_path, "failed", error="boom " * 100, done_at=to_iso(NOW))
    r = client.get("/activity")
    assert "Re-score failed: boom" in r.text
    assert len(r.text) < 1500  # the reason is truncated


def test_refuses_non_loopback_host(client):
    r = client.get("/activity", headers={"host": "evil.example.com"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", [p for p, _ in NAV] + ["/alerts", "/rejected"])
def test_every_base_page_carries_the_hook(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert 'id="activity"' in r.text and 'hx-get="/activity"' in r.text


@pytest.mark.parametrize("js", ["tracking.js", "inbox.js"])
def test_page_scripts_ignore_the_activity_poll(js):
    """The 3s /activity swap fires document-level htmx events; page handlers that consume a
    pending selection on any event would lose it (bug edb9ba6 review). The achievement notice's
    x (POST /achievements/seen, hx-swap none) is a band request too (specs/021 A4)."""
    from jobhunter.console.app import STATIC_DIR

    src = (STATIC_DIR / js).read_text(encoding="utf-8")
    assert 'p === "/activity" || p === "/achievements/seen"' in src
    handlers = src.count('addEventListener("htmx:')
    assert handlers and src.count("if (bandRequest(ev)) return;") == handlers
