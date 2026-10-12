"""evaluate: read first, write only on change, silent backfill, revocation, settings, the
busy fallback and the fingerprint (specs/021 "How unlocks are computed", "Backfill",
"Revocation")."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from achv_world import NOW, TZ, World, iso

from jobhunter.achievements import engine, facts
from jobhunter.core import db

EXPECTED_DAY_ONE = {
    ("setup.prefs", 1),
    ("search.found", 1),
    ("apply.first", 1),
    ("apply.momentum", 1),
    ("apply.momentum", 2),
    ("out.bounce_back", 1),
    ("rhythm.weeks", 1),
}


def history(w: World) -> None:
    """A history shaped like the spec's copy: 18 email-confirmed sends on 10 local days (one
    day with 6, one with 3), classifier rejections, a prefs save, one capture."""
    days = ["2026-09-19"] * 6 + ["2026-09-22"] * 3
    days += ["2026-09-23", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
    days += ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    for i, d in enumerate(days):
        w.app(f"{d} {9 + i % 6:02d}:00")
    for d in ("2026-09-15", "2026-09-25", "2026-10-03"):
        w.rejection(f"{d} 08:00")
    w.prefs_save("2026-10-09 12:00")
    w.capture("2026-10-10 12:00")


def ro(path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def drop_application(w: World, app_id: int) -> None:
    """What a shortlist undo or a merge's loser removal leaves: the application gone."""
    for table in ("application_event", "followup_log", "contact", "attachment"):
        w.conn.execute(f"DELETE FROM {table} WHERE application_id = ?", (app_id,))
    w.conn.execute("DELETE FROM application WHERE id = ?", (app_id,))


# --- backfill --------------------------------------------------------------------------------


def test_first_evaluate_is_a_silent_backfill(world):
    history(world)
    new = world.evaluate()
    assert {(u.achievement_id, u.tier) for u in new} == EXPECTED_DAY_ONE
    assert all(u.seen_at == u.detected_at for u in new)
    assert engine.read_settings(world.conn).backfilled_at == iso(NOW)
    # crossing dates are real: momentum 5 is the 5th counted send (3 on the 19th, then the 22nd)
    um = world.unlocks()[("apply.momentum", 1)]
    assert facts.parse_utc(um.unlocked_at).astimezone(TZ).date().isoformat() == "2026-09-22"


def test_backfill_with_no_history_still_records_backfilled(world):
    assert world.evaluate() == []
    assert engine.read_settings(world.conn).backfilled_at is not None
    world.prefs_save("2026-10-10 10:00")
    (u,) = world.evaluate()
    assert u.achievement_id == "setup.prefs" and u.seen_at is None  # news, not backfill


def test_later_unlocks_notify_even_with_an_old_crossing_date(world):
    history(world)
    world.evaluate()
    a = world.app()
    world.event(a, "rejected", "2026-09-20 10:00")  # recorded today, about an old answer
    (u,) = world.evaluate()
    assert (u.achievement_id, u.seen_at) == ("follow.loop", None)


def test_twice_in_a_row_gives_the_same_rows(world):
    history(world)
    world.evaluate()
    before = world.unlocks()
    assert world.evaluate() == []
    assert world.unlocks() == before


def test_no_change_call_performs_no_write(world, db_path):
    history(world)
    world.evaluate()
    conn = ro(db_path)
    try:
        assert engine.evaluate(conn, NOW, TZ) == []  # a write would raise on a read-only conn
    finally:
        conn.close()


def test_off_writes_nothing(world, db_path):
    history(world)
    engine.put_setting(world.conn, engine.ENABLED, "0", NOW)
    conn = ro(db_path)
    try:
        assert engine.evaluate(conn, NOW, TZ) == []
    finally:
        conn.close()
    assert world.unlocks() == {}


def test_racing_first_evaluations_give_one_silent_set(world, db_path):
    history(world)
    other = db.connect(db_path)
    try:
        # The other tab finished its read phase (it saw no backfill and everything to insert)
        # before this one committed; its write phase re-reads inside the transaction.
        engine.compute(other, NOW, TZ)
        world.evaluate()
        assert engine._write(other, NOW, TZ) == []
    finally:
        other.close()
    rows = world.unlocks()
    assert set(rows) == EXPECTED_DAY_ONE
    assert all(u.seen_at is not None for u in rows.values())


def test_busy_database_raises_quickly_and_restores_the_timeout(world, db_path):
    history(world)
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            world.evaluate()
        assert time.monotonic() - started < 1.5
        assert world.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert not world.conn.in_transaction
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert len(world.evaluate()) == len(EXPECTED_DAY_ONE)  # the next call retries


# --- settings --------------------------------------------------------------------------------


def test_notices_off_writes_seen(world):
    world.evaluate()
    engine.put_setting(world.conn, engine.NOTIFY, "0", NOW)
    world.prefs_save("2026-10-10 10:00")
    (u,) = world.evaluate()
    assert u.seen_at is not None


def test_hidden_category_is_computed_but_seen_at_once(world):
    world.evaluate()
    engine.put_setting(world.conn, engine.HIDDEN, engine.hidden_json(["setup"]), NOW)
    world.prefs_save("2026-10-10 10:00")
    world.capture("2026-10-10 11:00")
    new = {u.achievement_id: u for u in world.evaluate()}
    assert new["setup.prefs"].seen_at is not None
    assert new["search.found"].seen_at is None


# --- revocation ------------------------------------------------------------------------------


def shortlisted(w: World, n: int) -> list[int]:
    apps = []
    for i in range(n):
        a = w.app()
        w.event(a, "interested", f"2026-10-0{6 + i // 3} {9 + i % 3:02d}:00", note="moved here")
        apps.append(a)
    return apps


def test_undo_one_of_six_keeps_the_unlock_and_its_seen(world):
    world.evaluate()
    apps = shortlisted(world, 6)
    (u,) = world.evaluate()
    assert u.achievement_id == "search.shortlist" and u.seen_at is None
    engine.mark_seen(world.conn, [u.id], NOW)
    drop_application(world, apps[0])
    assert world.evaluate(NOW + timedelta(hours=1)) == []
    kept = world.unlocks()[("search.shortlist", 1)]
    assert kept.id == u.id and kept.seen_at is not None
    assert apps[0] not in json.loads(kept.evidence)["application"]  # refreshed in place


def test_undo_the_only_one_revokes_within_24_hours(world):
    world.evaluate()
    apps = shortlisted(world, 5)
    world.evaluate()
    drop_application(world, apps[-1])
    world.evaluate(NOW + timedelta(hours=23))
    assert ("search.shortlist", 1) not in world.unlocks()


def test_nothing_revokes_after_24_hours(world):
    world.evaluate()
    apps = shortlisted(world, 5)
    world.evaluate()
    drop_application(world, apps[-1])
    world.evaluate(NOW + timedelta(hours=25))
    assert ("search.shortlist", 1) in world.unlocks()


def test_merge_deleting_an_evidenced_application_does_not_revoke(world):
    world.evaluate()
    apps = [world.app(f"2026-10-0{6 + i // 3} {9 + i % 3:02d}:00") for i in range(6)]
    world.evaluate()
    drop_application(world, apps[0])
    world.evaluate(NOW + timedelta(hours=2))
    assert ("apply.momentum", 1) in world.unlocks()


def test_revoking_a_tier_takes_the_tiers_above(world):
    world.evaluate()
    apps = [world.app(f"2026-10-{i // 3 + 1:02d} {9 + i % 3:02d}:00") for i in range(10)]
    world.evaluate()
    assert {("apply.momentum", 1), ("apply.momentum", 2)} <= set(world.unlocks())
    for a in apps[:6]:
        drop_application(world, a)
    world.evaluate(NOW + timedelta(hours=1))
    assert not {("apply.momentum", 1), ("apply.momentum", 2)} & set(world.unlocks())


def test_rows_of_unknown_ids_are_never_touched(world):
    world.evaluate()
    world.conn.execute(
        "INSERT INTO achievement_unlock (achievement_id, tier, unlocked_at, detected_at, "
        "evidence, catalog_version) VALUES ('setup.future', 1, ?, ?, '{}', 1)",
        (iso(NOW), iso(NOW)),
    )
    world.evaluate(NOW + timedelta(hours=1))
    assert ("setup.future", 1) in world.unlocks()


# --- setup.ready (All set): 019's essentials_ok, judged by the caller


def test_all_set_unlocks_when_essentials_are_ok(world):
    world.evaluate()  # the silent backfill, with the essentials not yet judged
    (u,) = engine.evaluate(world.conn, NOW + timedelta(hours=1), TZ, essentials=True)
    assert (u.achievement_id, u.tier) == ("setup.ready", 1)
    assert u.seen_at is None  # a later unlock notifies


def test_all_set_is_not_granted_while_an_essential_is_missing(world):
    world.evaluate()
    assert engine.evaluate(world.conn, NOW + timedelta(hours=1), TZ, essentials=False) == []
    assert ("setup.ready", 1) not in world.unlocks()


def test_all_set_is_kept_when_the_essentials_are_unknown(world):
    engine.evaluate(world.conn, NOW, TZ, essentials=True)  # backfill
    assert engine.evaluate(world.conn, NOW + timedelta(hours=1), TZ) == []
    assert ("setup.ready", 1) in world.unlocks()


def test_all_set_revokes_within_24_hours_when_an_essential_breaks(world):
    engine.evaluate(world.conn, NOW, TZ, essentials=True)
    engine.evaluate(world.conn, NOW + timedelta(hours=1), TZ, essentials=False)
    assert ("setup.ready", 1) not in world.unlocks()


def test_all_set_is_permanent_after_24_hours(world):
    engine.evaluate(world.conn, NOW, TZ, essentials=True)
    engine.evaluate(world.conn, NOW + timedelta(hours=25), TZ, essentials=False)
    assert ("setup.ready", 1) in world.unlocks()


def test_essentials_are_judged_once_per_fingerprint_and_on_every_forced_refresh(world):
    gate = engine.Gate(tz=TZ)
    judged = []

    def essentials():
        judged.append(1)
        return True

    gate.refresh(world.conn, NOW, essentials=essentials)
    gate.refresh(world.conn, NOW, essentials=essentials)  # same fingerprint: no evaluation
    assert len(judged) == 1
    gate.refresh(world.conn, NOW, force=True, essentials=essentials)  # forced: judged again
    assert len(judged) == 2
    world.prefs_save("2026-10-10 10:00")
    gate.refresh(world.conn, NOW, essentials=essentials)
    assert len(judged) == 3
    assert ("setup.ready", 1) in world.unlocks()


def test_a_failing_essentials_check_means_unknown(world):
    gate = engine.Gate(tz=TZ)

    def broken():
        raise RuntimeError("readiness broke")

    gate.refresh(world.conn, NOW, essentials=broken)  # no exception reaches the caller
    assert ("setup.ready", 1) not in world.unlocks()


def test_a_run_that_ends_never_revokes(world):
    world.evaluate()
    for monday in ("2026-09-21", "2026-09-28"):
        world.prefs_save(f"{monday} 10:00")
        world.prefs_save(f"{monday[:-2]}{int(monday[-2:]) + 2:02d} 10:00")
    world.evaluate()
    assert ("rhythm.weeks", 1) in world.unlocks()
    later = NOW + timedelta(days=21)  # three missed weeks later
    world.evaluate(later)
    assert ("rhythm.weeks", 1) in world.unlocks()


# --- fingerprint and gate --------------------------------------------------------------------


def test_fingerprint_changes_with_rule_inputs_and_the_week(world):
    fp = engine.fingerprint(world.conn, NOW, TZ)
    assert engine.fingerprint(world.conn, NOW, TZ) == fp
    world.rejection("2026-10-01 10:00", state="pending")
    fp2 = engine.fingerprint(world.conn, NOW, TZ)
    assert fp2 != fp
    world.conn.execute("UPDATE rejection SET state = 'confirmed'")
    assert engine.fingerprint(world.conn, NOW, TZ) != fp2  # the confirmed count moved
    assert engine.fingerprint(world.conn, NOW + timedelta(days=1), TZ) != engine.fingerprint(
        world.conn, NOW, TZ
    )


def test_gate_evaluates_only_on_change(world, monkeypatch):
    gate = engine.Gate(tz=TZ)
    calls = []
    real = engine.evaluate
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: calls.append(1) or real(*a, **k))
    gate.refresh(world.conn, NOW)
    gate.refresh(world.conn, NOW)
    assert len(calls) == 1
    world.prefs_save("2026-10-10 10:00")
    gate.refresh(world.conn, NOW)
    assert len(calls) == 2
    gate.refresh(world.conn, NOW, force=True)
    assert len(calls) == 3


def test_gate_retries_after_a_failure(world, db_path):
    gate = engine.Gate(tz=TZ)
    history(world)
    holder = db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            gate.refresh(world.conn, NOW)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert len(gate.refresh(world.conn, NOW)) == len(EXPECTED_DAY_ONE)


# --- local zone ------------------------------------------------------------------------------


@pytest.fixture
def la_process_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_local_zone_is_dst_aware(world, la_process_zone):
    """No injected tz: a summer 00:30 PDT event keeps its day when now is in PST."""
    a = world.app()
    world.conn.execute(
        "INSERT INTO application_event (application_id, at, status, source, recorded_at) "
        "VALUES (?, '2026-07-15T07:30:00+00:00', 'applied', 'email', "
        "'2026-07-15T07:30:00+00:00')",
        (a,),
    )
    winter = datetime(2026, 12, 1, 20, 0, tzinfo=UTC)
    tz = engine.local_tz()
    f = facts.load(world.conn, winter, tz)
    assert f.day(f.sent[a].at) == date(2026, 7, 15)
    assert f.day(winter) == date(2026, 12, 1)
    # Both seasons at once, so no fixed offset (whatever the real date today) can pass.
    assert f.day("2026-12-01T07:30:00+00:00") == date(2026, 11, 30)
    gate = engine.Gate()  # no tz: the process zone
    gate.refresh(world.conn, winter)
    (u,) = [u for u in engine.stored(world.conn) if u.achievement_id == "apply.first"]
    assert u.unlocked_at == "2026-07-15T07:30:00+00:00"


def test_system_local_fallback_is_dst_aware(la_process_zone):
    tz = engine.SystemLocal()
    summer = datetime(2026, 7, 15, 7, 30, tzinfo=UTC).astimezone(tz)
    winter = datetime(2026, 12, 15, 7, 30, tzinfo=UTC).astimezone(tz)
    assert (summer.hour, summer.day) == (0, 15)
    assert (winter.hour, winter.day) == (23, 14)
