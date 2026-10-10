"""/prefs Re-score now: the confirmed plan is what runs, queued requests are superseded, cross-site
writes are refused, a slow run keeps its heartbeat, progress ends cleanly. Fake scorers only."""

# ruff: noqa: F811

from __future__ import annotations

import time
from datetime import timedelta

from fastapi.testclient import TestClient
from test_rescore_routes import (
    DESC,
    NOW,
    SPEC,
    FakeScorer,
    client,  # noqa: F401
    confirm_and_run,
    conn,  # noqa: F401
    db_path,  # noqa: F401
    finish,
    make_client,
    pdir,  # noqa: F401
    token_of,
)

from jobhunter.config import Scoring
from jobhunter.core import db
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore as rs
from jobhunter.scoring.profile import load_profile

LOOPBACK = "http://127.0.0.1:8808"


def add_job(c, n: int, fv: str) -> None:
    """One more prefiltered, unscored job group, like the fixture's three."""
    c.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "location_scope, employment_type, stage, posted_at, first_seen_at, last_seen_at) "
        "VALUES (?, 'co', ?, 'https://example.com/j', ?, 'Acme', ?, 'single', 'full_time', "
        "'prefiltered', ?, ?, ?)",
        (n, str(n), f"Job {n}", DESC, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    c.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', 'x')",
        (n, n),
    )
    c.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    c.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (n,))
    c.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[]', ?, 'x')",
        (n, fv),
    )


def counts(conn):
    return (
        conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0],
        conn.execute("SELECT count(*) FROM fit_score").fetchone()[0],
    )


def queue_raw(conn) -> None:
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd) VALUES ('2026-01-01T00:00:00Z', 'all', 'v', 3, 0, 0)"
    )


# ─── The confirmed plan is what runs ────────────────────────────────────────


def test_a_plan_that_grew_after_the_confirm_is_not_run(client, conn, pdir):
    data = {"scope": "all", "scorer": SPEC}
    panel = client.get("/prefs/rescore/panel", params=data).text
    assert 'hx-confirm="Re-score 3 job groups' in panel
    fv = load_profile(pdir).filter_version
    for n in (4, 5):
        add_job(conn, n, fv)  # ingested while the panel sat open
    r = client.post("/prefs/rescore/run", data={**data, "plan": token_of(panel)})
    finish(client)
    assert "The plan changed since you confirmed it: it is now 5 job groups" in r.text
    assert counts(conn) == (0, 0)
    # The redrawn panel confirms the new numbers; that confirm runs exactly those 5.
    assert 'hx-confirm="Re-score 5 job groups' in r.text
    client.post("/prefs/rescore/run", data={**data, "plan": token_of(r.text)})
    finish(client)
    row = conn.execute("SELECT status, scored, total, group_ids FROM rescore_request").fetchone()
    assert (row["status"], row["scored"], row["total"]) == ("done", 5, 5)
    assert row["group_ids"] == "[1, 2, 3, 4, 5]"


def test_a_run_without_a_confirmed_plan_is_refused(client, conn):
    r = client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    finish(client)
    assert "The plan changed since you confirmed it" in r.text
    assert counts(conn) == (0, 0)


def test_a_queued_run_confirms_with_its_numbers(client, conn):
    queue_raw(conn)
    text = client.get("/prefs").text
    assert "Run&hellip;" in text and 'hx-confirm="Run this queued' not in text
    panel = client.get("/prefs/rescore/panel", params={"request_id": "1", "scorer": SPEC}).text
    assert (
        'hx-confirm="Run queued re-score #1: re-score 3 job groups with test:model? '
        "Estimated cost $0.01; the run stops before it spends more than $0.02." in panel
    )
    assert 'name="request_id" value="1"' in panel
    confirm_and_run(client, request_id=1)
    finish(client)
    row = conn.execute("SELECT status, scored, scorer, group_ids FROM rescore_request").fetchone()
    assert (row["status"], row["scored"], row["scorer"]) == ("done", 3, SPEC)
    assert row["group_ids"] == "[1, 2, 3]"


def test_rescore_now_supersedes_the_queued_request(client, conn, pdir):
    queue_raw(conn)  # a /prefs save queued 'all' with no scorer
    confirm_and_run(client)  # a new request, not the queued one
    finish(client)
    rows = conn.execute("SELECT id, status, note FROM rescore_request ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "canceled", "superseded by re-score #2"),
        (2, "done", None),
    ]
    # The nightly drain (default scorer) has nothing left to pay for.
    calls: list[str] = []
    out = rs.drain_pending(
        conn,
        load_profile(pdir),
        Scoring(screen_scorer="anthropic:claude-haiku-4-5"),
        now=lambda: NOW,
        scorer_factory=lambda spec: calls.append(spec),
    )
    assert out == [] and calls == []


# ─── Cross-site writes ──────────────────────────────────────────────────────


def loopback_client(pdir, db_path) -> TestClient:
    c = make_client(pdir, db_path)
    c.base_url = LOOPBACK  # Host: 127.0.0.1:8808, as a browser on the console sends
    c.app.state.rescore_scorer_factory = lambda spec: FakeScorer()
    return c


def test_cross_site_post_cannot_start_a_paid_run(pdir, db_path, conn):
    c = loopback_client(pdir, db_path)
    token = token_of(c.get("/prefs/rescore/panel", params={"scope": "all"}).text)
    forged = f"scope=all&scorer={SPEC}&plan={token}"
    for headers in (
        {"Origin": "https://evil.example", "Content-Type": "text/plain"},
        {"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"},
        {"Origin": "null"},
    ):
        r = c.post("/prefs/rescore/run", content=forged, headers=headers)
        assert r.status_code == 403, headers
    c.app.state.rescore.join()
    assert counts(conn) == (0, 0)


def test_dns_rebinding_host_is_refused(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    c.base_url = "http://evil.example:8808"
    r = c.post(
        "/prefs/rescore/run",
        data={"scope": "all"},
        headers={"Origin": "http://evil.example:8808", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 403 and "loopback" in r.text
    assert counts(conn) == (0, 0)


def test_other_console_writes_are_guarded_too(pdir, db_path):
    c = loopback_client(pdir, db_path)
    for path in ("/prefs/save", "/prefs/preview", "/prefs/rescore/1/cancel"):
        r = c.post(path, data={}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path
    # Reads stay open, and a same-origin write from the console's own page goes through.
    assert c.get("/prefs", headers={"Origin": "https://evil.example"}).status_code == 200
    r = c.post(
        "/prefs/rescore/1/cancel",
        headers={"Origin": LOOPBACK, "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200


def test_same_origin_run_from_the_page_works(pdir, db_path, conn):
    c = loopback_client(pdir, db_path)
    token = token_of(c.get("/prefs/rescore/panel", params={"scope": "all"}).text)
    r = c.post(
        "/prefs/rescore/run",
        data={"scope": "all", "scorer": SPEC, "plan": token},
        headers={"Origin": LOOPBACK, "Sec-Fetch-Site": "same-origin", "HX-Request": "true"},
    )
    c.app.state.rescore.join()
    assert r.status_code == 200 and counts(conn) == (1, 3)


def test_allow_remote_accepts_another_host_but_still_same_origin(pdir, db_path):
    from jobhunter.config import Paths, Settings
    from jobhunter.console.app import create_app

    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    app = create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW, allow_remote=True)
    c = TestClient(app, base_url="http://192.0.2.10:8808")
    ok = c.post("/prefs/rescore/1/cancel", headers={"Origin": "http://192.0.2.10:8808"})
    bad = c.post("/prefs/rescore/1/cancel", headers={"Origin": "https://evil.example"})
    assert ok.status_code == 200 and bad.status_code == 403


# ─── Heartbeat and progress ─────────────────────────────────────────────────


def test_a_chunk_slower_than_the_stale_limit_keeps_its_heartbeat(pdir, db_path, conn, monkeypatch):
    monkeypatch.setattr(rs, "HEARTBEAT_EVERY_S", 0.01)
    clock = [NOW]
    seen: list[str | None] = []

    class Slow(FakeScorer):
        def submit(self, requests):
            clock[0] += timedelta(minutes=16)  # a local model chewing through one chunk
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                hb = conn.execute("SELECT heartbeat_at FROM rescore_request").fetchone()[0]
                if hb == to_iso(clock[0]):
                    break
                time.sleep(0.01)
            other = db.connect(db_path)  # another process's view: is the run still alive?
            live = rs.running_request(other, clock[0])
            other.close()
            seen.append(live["status"] if live else None)
            return super().submit(requests)

    profile = load_profile(pdir)
    plan = rs.make_plan(conn, profile, Scoring(), "all", SPEC, NOW)
    rid = rs.create_request(conn, profile, plan, NOW)
    run_conn = db.connect(db_path)
    try:
        rs.run_request(
            run_conn,
            rid,
            profile,
            Scoring(),
            now=lambda: clock[0],
            scorer_factory=lambda s: Slow(),
            chunk=3,
        )
    finally:
        run_conn.close()
    assert seen == ["running"]
    assert rs.get_request(conn, rid)["status"] == "done"


def test_progress_of_a_dead_run_shows_it_interrupted(client, conn):
    old = to_iso(NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, started_at, heartbeat_at) "
        "VALUES (?, 'all', 'v', 3, 0, 0, 'running', ?, ?)",
        (old, old, old),
    )
    text = client.get("/prefs/rescore/progress/1").text
    assert "Failed." in text and "interrupted" in text and "Re-scoring" not in text


def test_the_poll_that_sees_the_end_redraws_the_whole_panel(client, conn):
    confirm_and_run(client)
    finish(client)
    r = client.get("/prefs/rescore/progress/1", headers={"HX-Request": "true"})
    assert r.headers["HX-Retarget"] == "#rescore-panel" and r.headers["HX-Reswap"] == "innerHTML"
    assert "Done." in r.text and 'id="rescore-form"' in r.text
    assert "A re-score is running" not in r.text
    # Without htmx (or while running) it is still just the fragment.
    assert "HX-Retarget" not in client.get("/prefs/rescore/progress/1").headers
