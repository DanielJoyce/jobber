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


def test_rule_exception_renders_the_strip_from_stored_rows(client, db_path, monkeypatch):
    prefs_save(db_path)
    client.get("/achievements")  # stores setup.prefs
    capture(db_path, 1)

    def boom(*args, **kwargs):
        raise ValueError("bad timestamp")

    monkeypatch.setattr(engine, "compute", boom)
    text = strip_of(today(client))
    assert text is not None and "Achievements 1" in text


def _series(n):
    return rules.Series([rules.Item(NOW, ())] * n)


def test_tie_prefers_follow_up_over_an_earlier_catalog_entry():
    order = [a.id for a in catalog.CATALOG]
    assert order.index("apply.momentum") < order.index("follow.plan")
    got = engine.pick_next({"apply.momentum": _series(2), "follow.plan": _series(2)}, ())
    assert got is not None and got.name == catalog.BY_ID["follow.plan"].name


def test_tie_prefers_aimed_over_momentum():
    order = [a.id for a in catalog.CATALOG]
    assert order.index("apply.momentum") < order.index("apply.targeted")
    got = engine.pick_next({"apply.momentum": _series(2), "apply.targeted": _series(2)}, ())
    assert got is not None and got.name == catalog.BY_ID["apply.targeted"].name


def test_pick_next_picks_closest_tier_and_skips_hidden_and_finished():
    # 4 of 5 beats 2 of 5
    got = engine.pick_next({"apply.momentum": _series(4), "follow.plan": _series(2)}, ())
    assert got is not None and got.progress == 4 and got.target == 5
    assert engine.pick_next({"apply.momentum": _series(4)}, ["apply"]) is None
    assert engine.pick_next({"follow.plan": _series(15)}, ()) is None  # last tier reached


def test_all_set_is_judged_when_the_today_strip_evaluates_first(client, db_path, monkeypatch):
    """The strip can be the first evaluation on a new fingerprint: it must judge the essentials
    too, or All set would wait for a change that never comes."""
    from jobhunter import readiness

    monkeypatch.setattr(readiness, "essentials_ok_cheap", lambda *a, **k: True)
    assert client.get("/").status_code == 200
    rows = run(db_path, "SELECT tier FROM achievement_unlock WHERE achievement_id = 'setup.ready'")
    assert [tuple(r) for r in rows] == [(1,)]
