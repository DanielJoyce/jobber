"""The unlock notice in the activity band (specs/021 "Unlock notice", ticket A4).

Covers: the notice appears once per unlock and stays until dismissed; the x posts only the ids
it rendered; notices off and hidden categories show nothing; two tabs; an unchanged
fingerprint performs no write; a busy database falls back to stored rows; HX-Trigger on the
write routes; the band markup the browser relies on.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from jobhunter.achievements import engine
from jobhunter.config import Paths, Settings
from jobhunter.console import achievements_routes as ar
from jobhunter.console.app import create_app
from jobhunter.core import db

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=TZ).astimezone(UTC)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


class Trace:
    """Every SQL statement the console's connections run, while ``on``."""

    def __init__(self) -> None:
        self.on = False
        self.sql: list[str] = []

    def __call__(self, stmt: str) -> None:
        if self.on:
            self.sql.append(stmt)

    def writes(self) -> list[str]:
        verbs = ("INSERT", "UPDATE", "DELETE", "BEGIN", "REPLACE")
        return [s for s in self.sql if s.lstrip().upper().startswith(verbs)]


@pytest.fixture
def trace():
    return Trace()


@pytest.fixture
def app(tmp_path, db_path, trace):
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = Settings(paths=Paths(profile_dir=profile, db_path=db_path))

    def factory():
        c = db.connect(db_path)
        c.set_trace_callback(trace)
        return c

    a = create_app(settings, factory, clock=lambda: NOW)
    a.state.achievements.tz = TZ
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def run(db_path, sql, *args):
    c = db.connect(db_path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def prefs_save(db_path):
    run(
        db_path,
        "INSERT INTO profile_change (at, field_path, filter_version, scoring_version, source) "
        "VALUES ('2026-10-09T19:00:00+00:00', 'soft.x', 'f', 's', 'ui')",
    )


def capture(db_path, n=0):
    run(
        db_path,
        "INSERT INTO capture_log (action_id, route, captured_at, host, outcome) "
        "VALUES (?, '/ext/capture', '2026-10-10T19:00:00+00:00', 'example.com', 'added')",
        f"a{n}",
    )


def unlocks(db_path):
    return {
        r[0]: (r[1], r[2])
        for r in run(db_path, "SELECT achievement_id, id, seen_at FROM achievement_unlock")
    }


def notice(text: str) -> dict | None:
    """The rendered notice: its data-unlock, its posted ids and its visible text."""
    m = re.search(r'<span class="activity-item ach-notice"([^>]*)>(.*?)</button></span>', text)
    if m is None:
        return None
    attrs, body = m.groups()
    vals = re.search(r"hx-vals='([^']*)'", body)
    return {
        "unlock": int(re.search(r'data-unlock="(\d+)"', attrs).group(1)),
        "data_ids": re.search(r'data-ids="([^"]*)"', attrs).group(1),
        "posted": json.loads(html.unescape(vals.group(1)))["ids"],
        "text": re.sub(r"<[^>]+>", " ", body),
        "attrs": attrs,
        "body": body,
    }


def backfill(client):
    """The first poll after the migration is the silent backfill."""
    r = client.get("/activity")
    assert notice(r.text) is None


# --- the notice ---------------------------------------------------------------------------


def test_backfill_is_silent_and_a_later_unlock_shows_once_until_dismissed(client, db_path):
    prefs_save(db_path)
    backfill(client)  # Tuned in from history: no notice
    assert unlocks(db_path)["setup.prefs"][1] is not None
    capture(db_path)
    r = client.get("/activity")
    n = notice(r.text)
    found_id = unlocks(db_path)["search.found"][0]
    assert n is not None and n["unlock"] == found_id
    assert "Achievement: Found it yourself" in n["text"]
    assert n["posted"] == n["data_ids"] == str(found_id)
    # It keeps showing on later polls (one row, not re-inserted) until dismissed.
    for _ in range(3):
        assert notice(client.get("/activity").text)["unlock"] == found_id
    assert len(unlocks(db_path)) == 2
    r = client.post("/achievements/seen", data={"ids": n["posted"]})
    assert r.status_code == 204 and r.headers["hx-trigger"] == "jh-activity"
    assert notice(client.get("/activity").text) is None
    # and it never comes back
    capture(db_path, 1)  # a change, but no new tier (Found it yourself tier 2 needs more)
    assert notice(client.get("/activity").text) is None


def test_dismiss_posts_only_the_ids_it_showed(client, app, db_path):
    backfill(client)
    prefs_save(db_path)
    capture(db_path)
    n = notice(client.get("/activity").text)
    rows = unlocks(db_path)
    shown = {rows["setup.prefs"][0], rows["search.found"][0]}
    assert {int(i) for i in n["posted"].split(",")} == shown
    assert "+1 more" in n["text"]
    # A third unlock lands after the band rendered (another tab, the CLI, a write route).
    run(
        db_path,
        "INSERT INTO alert_signup (source_key, subscribed_at, updated_at) "
        "VALUES ('k', '2026-10-11T18:00:00+00:00', '2026-10-11T18:00:00+00:00')",
    )
    c = db.connect(db_path)
    try:
        new = engine.evaluate(c, NOW, TZ)
    finally:
        c.close()
    assert [u.achievement_id for u in new] == ["setup.alerts"]
    client.post("/achievements/seen", data={"ids": n["posted"]})
    after = unlocks(db_path)
    assert after["setup.prefs"][1] and after["search.found"][1]
    assert after["setup.alerts"][1] is None  # arrived later: still unseen
    later = notice(client.get("/activity").text)
    assert later["posted"] == str(after["setup.alerts"][0])
    assert "Net cast" in later["text"] and "more" not in later["text"]


def test_newest_unlock_is_the_one_named(client, db_path):
    backfill(client)
    prefs_save(db_path)
    capture(db_path)
    n = notice(client.get("/activity").text)
    rows = unlocks(db_path)
    newest = max(rows["setup.prefs"][0], rows["search.found"][0])
    assert n["unlock"] == newest


def test_notices_off_show_nothing_and_write_seen(client, db_path):
    backfill(client)
    client.post("/achievements/settings", data={"notify": "0"}, follow_redirects=False)
    capture(db_path)
    assert notice(client.get("/activity").text) is None
    assert unlocks(db_path)["search.found"][1] is not None


def test_notices_off_hides_an_unseen_row_already_waiting(client, db_path):
    backfill(client)
    capture(db_path)
    assert notice(client.get("/activity").text) is not None
    client.post("/achievements/settings", data={"notify": "0"}, follow_redirects=False)
    assert notice(client.get("/activity").text) is None


def test_hidden_category_shows_no_notice(client, db_path):
    backfill(client)
    client.post(
        "/achievements/settings",
        data={"hidden_set": "1", "hidden": ["search"]},
        follow_redirects=False,
    )
    capture(db_path)
    assert notice(client.get("/activity").text) is None
    prefs_save(db_path)  # setup is visible
    n = notice(client.get("/activity").text)
    assert "Tuned in" in n["text"] and "more" not in n["text"]
    assert n["posted"] == str(unlocks(db_path)["setup.prefs"][0])


def test_achievements_off_shows_nothing_and_writes_nothing(client, db_path, trace):
    client.post("/achievements/settings", data={"enabled": "0"}, follow_redirects=False)
    capture(db_path)
    trace.on = True
    r = client.get("/activity")
    assert notice(r.text) is None
    assert unlocks(db_path) == {}
    assert trace.writes() == []


def test_tier_is_named_only_for_a_family(client, db_path):
    backfill(client)
    run(
        db_path,
        "INSERT INTO alert_signup (source_key, subscribed_at, updated_at) "
        "VALUES ('k', '2026-10-11T18:00:00+00:00', '2026-10-11T18:00:00+00:00')",
    )
    assert "Net cast (tier 1)" in notice(client.get("/activity").text)["text"]
    client.post("/achievements/seen", data={"ids": str(unlocks(db_path)["setup.alerts"][0])})
    capture(db_path)
    text = notice(client.get("/activity").text)["text"]
    assert "Found it yourself" in text and "tier" not in text


# --- tabs and concurrency -----------------------------------------------------------------


def test_two_tabs_dismiss_in_one_clears_the_other(app, db_path):
    tab1, tab2 = TestClient(app), TestClient(app)
    backfill(tab1)
    capture(db_path)
    n1 = notice(tab1.get("/activity").text)
    n2 = notice(tab2.get("/activity").text)
    assert n1["unlock"] == n2["unlock"]
    assert len(unlocks(db_path)) == 1  # the second tab did not insert again
    tab1.post("/achievements/seen", data={"ids": n1["posted"]})
    assert notice(tab2.get("/activity").text) is None


def test_two_consoles_racing_the_same_change_write_one_row(tmp_path, db_path):
    """Two console processes (each its own gate) seeing the same new unlock."""
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = Settings(paths=Paths(profile_dir=profile, db_path=db_path))
    apps = [create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW) for _ in "ab"]
    for a in apps:
        a.state.achievements.tz = TZ
    tabs = [TestClient(a) for a in apps]
    backfill(tabs[0])
    capture(db_path)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def poll(t):
        barrier.wait()
        results.append(t.get("/activity").text)

    threads = [threading.Thread(target=poll, args=(t,)) for t in tabs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(unlocks(db_path)) == 1
    rows = run(db_path, "SELECT COUNT(*) FROM achievement_unlock WHERE seen_at IS NULL")
    assert rows[0][0] == 1
    # Whatever each poll rendered, the next one in both shows the one unlock.
    assert {notice(t.get("/activity").text)["unlock"] for t in tabs} == {
        unlocks(db_path)["search.found"][0]
    }


def test_unchanged_fingerprint_performs_no_write_and_no_evaluate(
    client, db_path, trace, monkeypatch
):
    prefs_save(db_path)
    backfill(client)
    capture(db_path)
    client.get("/activity")  # evaluates and writes the unlock
    calls = []
    real = engine.evaluate
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: calls.append(1) or real(*a, **k))
    trace.on = True
    for _ in range(3):
        r = client.get("/activity")
        assert notice(r.text) is not None
    assert calls == []
    assert trace.writes() == []
    # A change re-arms it.
    capture(db_path, 1)
    client.get("/activity")
    assert calls == [1]


def test_poll_does_not_queue_behind_another_evaluation(app, client, db_path, monkeypatch):
    backfill(client)
    capture(db_path)
    g = app.state.achievements
    calls = []
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: calls.append(1) or [])
    assert g._running.acquire()  # another request is evaluating
    try:
        started = time.monotonic()
        r = client.get("/activity")
        assert time.monotonic() - started < 2
    finally:
        g._running.release()
    assert r.status_code == 200 and calls == []
    client.get("/activity")  # the fingerprint was left alone: this one evaluates
    assert calls == [1]


def test_busy_database_falls_back_and_the_next_poll_catches_up(client, db_path):
    backfill(client)
    capture(db_path)
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")  # a pipeline run or a re-score writing
    try:
        started = time.monotonic()
        r = client.get("/activity")
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 200
    assert elapsed < 2  # 250 ms busy timeout, not the connection's 5 s
    assert notice(r.text) is None and unlocks(db_path) == {}
    assert notice(client.get("/activity").text) is not None


def test_dismiss_on_a_busy_database_answers_503_and_keeps_the_notice(client, db_path):
    backfill(client)
    capture(db_path)
    n = notice(client.get("/activity").text)
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        r = client.post("/achievements/seen", data={"ids": n["posted"]})
        assert time.monotonic() - started < 2
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 503
    assert notice(client.get("/activity").text)["unlock"] == n["unlock"]


# --- markup the browser relies on -----------------------------------------------------------


def test_band_markup(client, db_path):
    backfill(client)
    capture(db_path)
    r = client.get("/activity")
    assert "jh-activity from:body" in r.text
    n = notice(r.text)
    assert 'aria-live="off"' in n["attrs"]
    assert 'class="ach-dismiss"' in n["body"] and "activity-dismiss" not in n["body"]
    assert 'hx-post="/achievements/seen"' in n["body"] and 'hx-swap="none"' in n["body"]
    for href in re.findall(r'href="([^"]+)"', n["body"]):
        assert client.get(href).status_code == 200
    page = client.get("/help").text
    announce = page.index('id="ach-announce"')
    assert 'role="status"' in page[announce - 80 : announce + 120]
    # outside the band, which is swapped wholesale on every poll
    band = re.search(r'<div id="activity"[^>]*></div>', page)
    assert band is not None and announce > band.end()


def test_notice_does_not_disturb_the_rescore_line(client, db_path):
    backfill(client)
    capture(db_path)
    run(
        db_path,
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, done_at, scored, cost_usd) "
        "VALUES (?, 'recent', 'v', 1, 0, 0, 'done', ?, 1, 0.01)",
        NOW.isoformat(),
        NOW.isoformat(),
    )
    r = client.get("/activity")
    assert 'class="activity-dismiss"' in r.text and notice(r.text) is not None
    # dismissing the re-score line (query param) keeps the achievement notice
    rid = re.search(r'data-dismiss="(\d+)"', r.text).group(1)
    r = client.get("/activity", params={"dismissed": rid})
    assert "activity-dismiss" not in r.text and notice(r.text) is not None


# --- HX-Trigger on write routes -------------------------------------------------------------


def test_every_unlock_write_pattern_names_a_real_post_route(app):
    posts = [r.path_format for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods]
    samples = [re.sub(r"\{[^}]*\}", "7", p) for p in posts]
    for pattern in ar.UNLOCK_WRITES:
        assert any(pattern.fullmatch(s) for s in samples), pattern.pattern


@pytest.mark.parametrize(
    "path",
    [
        "/pipeline/3/move",
        "/pipeline/3/event",
        "/pipeline/3/next-action",
        "/pipeline/3/contacts",
        "/job/3/applied",
        "/followups/3/done",
        "/followups/3/no-response",
        "/inbox/3/label",
        "/inbox/bulk",
        "/packet/3/ready",
        "/rejections",
        "/rejections/3/confirm",
        "/proposals/3/accept",
        "/alerts/greenhouse/subscribe",
        "/prefs/save",
    ],
)
def test_spec_listed_routes_trigger_the_band(path):
    assert ar.is_unlock_write("POST", path)


@pytest.mark.parametrize(
    "path", ["/prefs/rescore/run", "/packet/3/generate", "/job/3/score", "/achievements/seen"]
)
def test_paid_and_unrelated_routes_do_not(path):
    assert not ar.is_unlock_write("POST", path)


def test_trigger_merges_with_what_a_route_set():
    assert ar.with_band_event(None) == "jh-activity"
    assert ar.with_band_event("prefs-changed") == "prefs-changed, jh-activity"
    assert ar.with_band_event("a, jh-activity") == "a, jh-activity"
    assert json.loads(ar.with_band_event('{"toast": {"x": 1}}')) == {
        "toast": {"x": 1},
        "jh-activity": "",
    }


def test_a_write_route_response_carries_the_trigger(client, db_path):
    backfill(client)
    r = client.post(
        "/rejections",
        data={"employer": "Acme", "date": "2026-10-11"},
        follow_redirects=False,
    )
    assert r.status_code < 400, r.text
    assert "jh-activity" in r.headers.get("hx-trigger", "")
    # a refused write (404) carries none
    r = client.post("/pipeline/999999/next-action", data={"next_action": "x"})
    assert r.status_code == 404
    assert "jh-activity" not in r.headers.get("hx-trigger", "")


def test_a_rule_bug_does_not_break_the_band(client, db_path, monkeypatch, caplog):
    backfill(client)
    capture(db_path)
    client.get("/activity")  # Found it yourself stored, unseen
    capture(db_path, 1)  # a change, so the next poll evaluates

    def boom(*a, **k):
        raise RuntimeError("rule bug")

    monkeypatch.setattr(engine, "evaluate", boom)
    r = client.get("/activity")
    assert r.status_code == 200
    assert "Found it yourself" in notice(r.text)["text"]  # stored rows still shown
    assert "rule bug" in caplog.text
    # A failure past the evaluation: no notice, the rest of the band as usual.
    monkeypatch.setattr(engine, "unseen", boom)
    run(
        db_path,
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, done_at, scored, cost_usd) "
        "VALUES (?, 'recent', 'v', 1, 0, 0, 'done', ?, 1, 0.01)",
        NOW.isoformat(),
        NOW.isoformat(),
    )
    r = client.get("/activity")
    assert r.status_code == 200
    assert notice(r.text) is None and "Re-score done" in r.text
    assert client.get("/help").status_code == 200


def test_notice_keys_carry_detected_at(client, db_path):
    backfill(client)
    capture(db_path)
    r = client.get("/activity")
    (row,) = run(db_path, "SELECT id, detected_at FROM achievement_unlock WHERE seen_at IS NULL")
    key = f"{row[0]}@{row[1]}"
    assert f'data-key="{key}"' in r.text and f'data-keys="{key}"' in r.text


def test_no_trigger_while_achievements_are_off(client, db_path):
    client.post("/achievements/settings", data={"enabled": "0"}, follow_redirects=False)
    r = client.post(
        "/rejections",
        data={"employer": "Acme", "date": "2026-10-11"},
        follow_redirects=False,
    )
    assert "jh-activity" not in r.headers.get("hx-trigger", "")
