"""/achievements stub, its seen and settings routes, and the nav entry (specs/021 ticket A2)."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=TZ).astimezone(UTC)
NAV_LINK = '<a href="/achievements"'


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
    return TestClient(app)


def run(db_path, sql, *args):
    c = db.connect(db_path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def prefs_save(db_path, at="2026-10-09T19:00:00+00:00"):
    run(
        db_path,
        "INSERT INTO profile_change (at, field_path, filter_version, scoring_version, source) "
        "VALUES (?, 'soft.x', 'f', 's', 'ui')",
        at,
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


def earned(page, ach_id):
    m = re.search(rf'<li class="ach-card" data-state="(\w+)" data-id="{re.escape(ach_id)}"', page)
    assert m, ach_id
    return m.group(1) == "earned"


def test_nav_entry_and_stub_page(client, db_path):
    prefs_save(db_path)
    r = client.get("/achievements")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "1 earned" in r.text and "Tuned in" in r.text
    assert NAV_LINK in r.text and 'aria-current="page"' in r.text
    assert NAV_LINK in client.get("/help").text
    # the first visit was the silent backfill
    assert unlocks(db_path)["setup.prefs"][1] is not None


def test_every_link_on_the_board_resolves(client, db_path):
    prefs_save(db_path)
    page = client.get("/achievements").text
    hrefs = {h for h in re.findall(r'href="([^"]+)"', page) if h.startswith("/")}
    assert "/achievements" in hrefs
    for h in sorted(hrefs):
        assert client.get(h).status_code == 200, h
    for action in re.findall(r'action="([^"]+)"', page):
        assert action == "/achievements/settings"


def test_visit_marks_rendered_unlocks_seen(client, db_path):
    client.get("/achievements")  # backfill (nothing yet)
    prefs_save(db_path)
    c = db.connect(db_path)
    from jobhunter.achievements import engine

    engine.evaluate(c, NOW, TZ)
    c.close()
    assert unlocks(db_path)["setup.prefs"][1] is None  # a notice is due
    client.get("/achievements")
    assert unlocks(db_path)["setup.prefs"][1] is not None


def test_seen_marks_only_the_posted_ids(client, db_path):
    client.get("/achievements")
    prefs_save(db_path)
    capture(db_path)
    c = db.connect(db_path)
    from jobhunter.achievements import engine

    engine.evaluate(c, NOW, TZ)
    c.close()
    rows = unlocks(db_path)
    r = client.post("/achievements/seen", data={"ids": str(rows["setup.prefs"][0])})
    assert r.status_code == 204
    assert r.headers["hx-trigger"] == "jh-activity"
    after = unlocks(db_path)
    assert after["setup.prefs"][1] is not None
    assert after["search.found"][1] is None


def test_turning_off_hides_the_nav_and_writes_nothing(client, db_path):
    r = client.post("/achievements/settings", data={"enabled": "0"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/achievements"
    assert NAV_LINK not in client.get("/help").text
    prefs_save(db_path)
    page = client.get("/achievements").text
    assert "Achievements are off" in page
    assert unlocks(db_path) == {}
    # back on: a silent backfill
    client.post("/achievements/settings", data={"enabled": "1"})
    assert NAV_LINK in client.get("/help").text
    client.get("/achievements")
    assert unlocks(db_path)["setup.prefs"][1] is not None


def test_on_again_backfills_silently_what_happened_while_off(client, db_path):
    client.get("/achievements")  # backfilled (empty)
    client.post("/achievements/settings", data={"enabled": "0"})
    prefs_save(db_path)
    client.post("/achievements/settings", data={"enabled": "1"}, follow_redirects=False)
    c = db.connect(db_path)
    from jobhunter.achievements import engine

    (u,) = engine.evaluate(c, NOW, TZ)
    c.close()
    assert u.seen_at is not None


def test_hidden_category_is_off_the_board_and_its_notices_seen(client, db_path):
    client.get("/achievements")
    prefs_save(db_path)
    capture(db_path)
    c = db.connect(db_path)
    from jobhunter.achievements import engine

    engine.evaluate(c, NOW, TZ)
    c.close()
    client.post(
        "/achievements/settings",
        data={"hidden_set": "1", "hidden": ["setup", "nope"]},
        follow_redirects=False,
    )
    assert unlocks(db_path)["setup.prefs"][1] is not None  # seen at once
    assert unlocks(db_path)["search.found"][1] is None
    s = engine.read_settings(db.connect(db_path))
    assert s.hidden == frozenset({"setup"})
    page = client.get("/achievements").text
    assert "Tuned in" not in page and "Found it yourself" in page and "1 earned" in page
    client.post("/achievements/settings", data={"hidden_set": "1"})
    assert "Tuned in" in client.get("/achievements").text


def test_notify_setting(client, db_path):
    client.post("/achievements/settings", data={"notify": "0"})
    from jobhunter.achievements import engine

    assert engine.read_settings(db.connect(db_path)).notify is False


def test_busy_database_still_renders_from_stored_rows(client, db_path):
    prefs_save(db_path)
    client.get("/achievements")  # Tuned in stored
    capture(db_path)  # a change that needs a write
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        r = client.get("/achievements")
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 200
    assert earned(r.text, "search.found") is False and earned(r.text, "setup.prefs")
    assert elapsed < 1.5
    # the next visit retries
    assert earned(client.get("/achievements").text, "search.found")


def test_busy_evaluate_skips_the_seen_write(client, db_path, monkeypatch):
    from jobhunter.achievements import engine

    client.get("/achievements")  # backfilled (empty)
    prefs_save(db_path)
    c = db.connect(db_path)
    engine.evaluate(c, NOW, TZ)  # an unseen unlock
    c.close()
    capture(db_path)  # and a change that needs a write
    calls = []
    monkeypatch.setattr(engine, "mark_seen", lambda *a, **k: calls.append(a))
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        r = client.get("/achievements")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 200 and "Tuned in" in r.text
    assert calls == []
    client.get("/achievements")
    assert len(calls) == 1  # the next, unblocked visit marks it


def test_board_cards_sections_and_copy(client, db_path):
    prefs_save(db_path)
    page = client.get("/achievements").text
    for label in ("Setup", "Search", "Apply", "Follow-up", "Outcomes", "Rhythm"):
        assert f">{label}</h2>" in page
    assert earned(page, "setup.prefs") and not earned(page, "apply.momentum")
    # locked tiered card: tier text beside the pips, a hint, and a visible count
    assert "Locked" in page and "Hint: " in page
    assert re.search(r"0 of 5</span>", page)
    # hidden achievements stay secret until earned, and never name rejections
    assert "Hidden achievement" in page and "Bounce back" not in page
    assert "The long game" not in page
    assert re.search(r"\bsymbol id=\"ach-apply\"", page) and 'aria-hidden="true"' in page
    for word in ("rejected", "Rejection"):
        assert word not in page


def test_why_lists_evidence_with_resolvable_links(client, db_path):
    prefs_save(db_path)
    page = client.get("/achievements").text
    assert "<summary>Why</summary>" in page and "Earned Oct" in page
    assert "Prefs saved" in page


def test_board_marks_progress_for_tiered_search_card(client, db_path):
    for i in range(3):
        capture(db_path, i)
    page = client.get("/achievements").text
    assert 'data-state="earned" data-id="search.found"' in page
    assert "Tier " not in page.split('data-id="search.found"')[1].split("</li>")[0]


def test_settings_form_labels_and_state(client, db_path):
    client.post("/achievements/settings", data={"hidden_set": "1", "hidden": ["rhythm"]})
    client.post("/achievements/settings", data={"notify": "0"})
    page = client.get("/achievements").text
    assert re.search(r'name="notify" value="0" checked', page)
    assert re.search(r'name="hidden" value="rhythm" checked', page)
    assert "<legend>Hide categories</legend>" in page
    assert ">Rhythm</h2>" not in page
    assert page.count("<label>") == page.count("</label>") >= 8
