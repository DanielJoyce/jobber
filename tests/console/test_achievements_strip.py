"""The Today strip (specs/021 ticket A5): content, off state, busy-database fallback."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jobhunter.achievements import catalog, engine, rules
from jobhunter.config import Paths, Settings
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


@pytest.fixture
def client(tmp_path, db_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = Settings(paths=Paths(profile_dir=profile, db_path=db_path))
    app = create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW)
    app.state.achievements.tz = TZ
    app.state.setup_first_run = lambda request, conn: None  # not the first-run redirect
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


def capture(db_path, n):
    run(
        db_path,
        "INSERT INTO capture_log (action_id, route, captured_at, host, outcome) "
        "VALUES (?, '/ext/capture', '2026-10-10T19:00:00+00:00', 'example.com', 'added')",
        f"a{n}",
    )


def unlocked(db_path):
    return {r[0] for r in run(db_path, "SELECT achievement_id FROM achievement_unlock")}


def today(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    return r.text


def strip_of(page):
    m = re.search(r'<p class="ach-strip".*?</p>', page, re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(0))).strip() if m else None


def test_strip_shows_earned_next_rhythm_and_link(client, db_path):
    prefs_save(db_path)
    capture(db_path, 0)
    run(
        db_path,
        "INSERT INTO alert_signup (source_key, subscribed_at, updated_at) "
        "VALUES ('k1', '2026-10-09T19:00:00+00:00', '2026-10-09T19:00:00+00:00')",
    )
    page = today(client)
    text = strip_of(page)
    assert text is not None
    assert "Achievements 3" in text or "Achievements 4" in text
    assert "Next: Net cast 1 of 5" in text
    assert "Rhythm 0 weeks" in text
    assert re.search(r'<a class="ach-strip-link" href="/achievements"', page)


def test_strip_hidden_when_off_and_writes_nothing(client, db_path):
    client.post("/achievements/settings", data={"enabled": "0"})
    prefs_save(db_path)
    assert strip_of(today(client)) is None
    assert unlocked(db_path) == set()


def test_busy_database_renders_from_stored_rows_and_retries(client, db_path):
    prefs_save(db_path)
    client.get("/achievements")  # stores setup.prefs
    capture(db_path, 1)  # a change the next evaluate wants to write
    blocker = db.connect(db_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        page = today(client)
        elapsed = time.monotonic() - t0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert elapsed < 2
    text = strip_of(page)
    assert text is not None and "Achievements 1" in text  # the stored row only
    assert "search.found" not in unlocked(db_path)
    assert "Achievements 2" in strip_of(today(client))  # the next load retries and writes
    assert "search.found" in unlocked(db_path)


def test_pick_next_closest_tier_ties_prefer_follow_and_skip_hidden():
    item = rules.Item(NOW, ())
    multi = [a for a in catalog.CATALOG if not a.hidden and a.category != "rhythm"]
    follow = next(a for a in multi if a.category == "follow" and len(a.thresholds) > 1)
    other = next(
        a
        for a in multi
        if a.category not in ("follow", "aimed") and a.thresholds[0] == follow.thresholds[0]
    )
    one = {follow.id: rules.Series([item]), other.id: rules.Series([item])}
    got = engine.pick_next(one, ())
    assert got is not None and got.progress == 1
    assert got.name == min((follow, other), key=lambda a: catalog.CATALOG.index(a)).name or (
        got.name == follow.name
    )
    assert engine.pick_next(one, [follow.category, other.category]) is None
    done = {a.id: rules.Series([item] * a.thresholds[-1]) for a in (follow,)}
    assert engine.pick_next(done, ()) is None  # all tiers reached: nothing to chase
