"""Automatic backups and 7 daily + 4 weekly pruning (specs/018-containers.md#backups, C6)."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

from jobhunter.core import backup

NOW = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)  # a Saturday, ISO week 41


def touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def auto(directory, stamp: str):
    return touch(directory / f"jobhunter-auto-{stamp}Z.db")


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def nightly(directory, last: datetime, days: int):
    for i in range(days):
        auto(directory, (last - timedelta(days=i)).strftime("%Y%m%dT%H%M%S"))


def test_prune_keeps_7_daily_and_4_weekly_and_touches_nothing_else(tmp_path):
    backups = tmp_path / "backups"
    autod = backups / "auto"
    # 60 nightly backups at 03:00 UTC up to today, plus an earlier one today and two extra on
    # 2026-09-27 (the newest of each day or week is the one kept).
    nightly(autod, datetime(2026, 10, 10, 3, 0, tzinfo=UTC), 60)
    auto(autod, "20261010T010000")
    auto(autod, "20260927T010000")
    auto(autod, "20260927T235959")
    # Things that must survive whatever their age.
    others = [
        touch(autod / "pre-migrate-28-30-20250101T000000Z.db"),
        touch(autod / "pre-migrate-29-30-20250102T000000Z.db"),
        touch(autod / "jobhunter-auto-20250101T030000Z.db.tmp"),
        touch(autod / ".jobhunter-auto-20250101T030000Z.db.abcd.tmp"),
        touch(autod / "jobhunter-auto-20250101T030000.db"),  # no Z
        touch(autod / "jobhunter-auto-20250101T030000Z.db-wal"),
        touch(autod / "Jobhunter-auto-20250101T030000Z.db"),
        touch(autod / "jobhunter-auto-20251399T030000Z.db"),  # right shape, not a date
        touch(autod / "notes.txt"),
        touch(backups / "jobhunter-20261009-160902-pre-jev.db"),
        touch(backups / "jobhunter-auto-20250101T030000Z.db"),  # right name, wrong directory
    ]
    (autod / "jobhunter-auto-20250102T030000Z.db").mkdir()  # a directory, not a file
    link_target = touch(tmp_path / "elsewhere.db")
    (autod / "jobhunter-auto-20250103T030000Z.db").symlink_to(link_target)

    removed = backup.prune_auto(autod, NOW)

    kept_auto = sorted(n for n in names(autod) if backup.AUTO_RE.match(n) and (autod / n).is_file())
    # Hand-computed: the newest of each of the last 7 days (Oct 4..10) and of each of the last
    # 4 ISO weeks (41: Oct 10, 40: Oct 4, 39: Sep 27 23:59:59, 38: Sep 20).
    assert kept_auto == [
        "jobhunter-auto-20250103T030000Z.db",  # the symlink: never followed or deleted
        "jobhunter-auto-20251399T030000Z.db",
        "jobhunter-auto-20260920T030000Z.db",
        "jobhunter-auto-20260927T235959Z.db",
        "jobhunter-auto-20261004T030000Z.db",
        "jobhunter-auto-20261005T030000Z.db",
        "jobhunter-auto-20261006T030000Z.db",
        "jobhunter-auto-20261007T030000Z.db",
        "jobhunter-auto-20261008T030000Z.db",
        "jobhunter-auto-20261009T030000Z.db",
        "jobhunter-auto-20261010T030000Z.db",
    ]
    assert len(removed) == 60 + 3 - 9
    assert all(p.parent == autod and backup.AUTO_RE.match(p.name) for p in removed)
    for path in others:
        assert path.exists(), path
    assert (autod / "jobhunter-auto-20250102T030000Z.db").is_dir()
    assert link_target.exists()

    # Pruning again changes nothing.
    assert backup.prune_auto(autod, NOW) == []


def test_days_without_a_backup_do_not_use_up_slots(tmp_path):
    """A machine that was off for months still keeps its last 7 backups."""
    autod = tmp_path / "auto"
    nightly(autod, datetime(2026, 6, 30, 3, 0, tzinfo=UTC), 20)  # nothing since June
    backup.prune_auto(autod, NOW)
    kept = names(autod)
    # Days: Jun 24..30. Weeks: 27 (Jun 29-30), 26 (Jun 22-28: Jun 28), 25: Jun 21, 24: Jun 14.
    assert kept == [
        "jobhunter-auto-20260614T030000Z.db",
        "jobhunter-auto-20260621T030000Z.db",
        *[f"jobhunter-auto-202606{d}T030000Z.db" for d in range(24, 31)],
    ]


def test_a_future_stamp_cannot_push_real_backups_out(tmp_path):
    autod = tmp_path / "auto"
    nightly(autod, datetime(2026, 10, 10, 3, 0, tzinfo=UTC), 10)
    future = [auto(autod, f"2030010{d}T030000") for d in range(1, 9)]
    backup.prune_auto(autod, NOW)
    for path in future:
        assert path.exists()
    for d in range(4, 11):  # the 7 real daily backups all survive
        assert (autod / f"jobhunter-auto-202610{d:02d}T030000Z.db").exists()


def test_protect_keeps_the_backup_just_written_even_with_tiny_retention(tmp_path):
    autod = tmp_path / "auto"
    new = auto(autod, "20261009T030000")
    newer = auto(autod, "20261010T030000")
    backup.prune_auto(autod, NOW, daily=1, weekly=0, protect=new)
    assert new.exists() and newer.exists()


def wal_db(path, rows=3):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    return conn


def test_auto_backup_writes_a_private_utc_named_copy_then_prunes(tmp_path):
    data = tmp_path / "data"
    autod = data / "backups" / "auto"
    nightly(autod, datetime(2026, 10, 9, 3, 0, tzinfo=UTC), 30)
    pre = touch(autod / "pre-migrate-1-2-20200101T000000Z.db")
    manual = touch(data / "backups" / "jobhunter-20200101T000000Z.db")
    live = data / "jobhunter.db"
    conn = wal_db(live, rows=5)  # rows still in the -wal, uncheckpointed
    try:
        when = datetime(2026, 10, 10, 5, 6, 7, tzinfo=UTC)
        dest, removed = backup.auto_backup(conn, data, when)
    finally:
        conn.close()
    assert dest == autod / "jobhunter-auto-20261010T050607Z.db"
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 5
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    copy.close()
    assert removed and dest not in removed
    assert pre.exists() and manual.exists()
    assert len([n for n in names(autod) if n.startswith("jobhunter-auto-")]) <= 11


def test_a_local_time_stamp_is_written_in_utc(tmp_path):
    from zoneinfo import ZoneInfo

    local = datetime(2026, 10, 10, 20, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert backup.auto_name(local) == "jobhunter-auto-20261011T033000Z.db"
    assert backup.AUTO_RE.match(backup.auto_name(local))
    manual = backup.manual_name(local, "restore")
    assert manual == "jobhunter-20261011T033000Z-pre-restore.db"
    assert not backup.AUTO_RE.match(manual) and not backup.PRE_MIGRATE_RE.match(manual)


def test_table_counts_skips_fts_shadow_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE a(x)")
    conn.execute("INSERT INTO a VALUES (1), (2)")
    conn.execute("CREATE VIRTUAL TABLE f USING fts5(body)")
    conn.execute("INSERT INTO f VALUES ('hello')")
    assert backup.table_counts(conn) == {"a": 2}
    conn.close()


def test_days_are_utc_days_whatever_the_local_timezone(tmp_path, monkeypatch):
    """23:30Z and 00:30Z the next morning are two UTC days, though one local day in many zones;
    two runs in one UTC day are one day, though two local days east of UTC."""
    import time

    for tz in ("America/Los_Angeles", "Asia/Tokyo", "UTC"):
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        autod = tmp_path / tz.replace("/", "_")
        for stamp in ["20261009T233000", "20261010T003000", "20261010T143000"]:
            auto(autod, stamp)
        backup.prune_auto(autod, NOW, daily=2, weekly=0)
        assert names(autod) == [
            "jobhunter-auto-20261009T233000Z.db",
            "jobhunter-auto-20261010T143000Z.db",
        ], tz
    monkeypatch.delenv("TZ")
    time.tzset()


def test_weeks_are_iso_weeks_across_a_year_end(tmp_path):
    """2026-12-28 .. 2027-01-03 is one ISO week (2026-W53), though it spans two calendar years,
    and the same week number in another year is another week."""
    autod = tmp_path / "auto"
    for stamp in [
        "20261228T030000",  # 2026-W53 (Mon)
        "20270102T030000",  # 2026-W53 (Sat): newest of that week
        "20261221T030000",  # 2026-W52
        "20250106T030000",  # 2025-W02
        "20260105T030000",  # 2026-W02: same week number, other year
    ]:
        auto(autod, stamp)
    backup.prune_auto(autod, datetime(2027, 1, 3, tzinfo=UTC), daily=0, weekly=4)
    assert names(autod) == [
        "jobhunter-auto-20250106T030000Z.db",
        "jobhunter-auto-20260105T030000Z.db",
        "jobhunter-auto-20261221T030000Z.db",
        "jobhunter-auto-20270102T030000Z.db",
    ]


def test_the_same_day_in_another_year_is_another_day(tmp_path):
    autod = tmp_path / "auto"
    auto(autod, "20251010T030000")
    auto(autod, "20261010T030000")
    backup.prune_auto(autod, NOW, daily=2, weekly=0)
    assert len(names(autod)) == 2


def test_leftover_temp_files_are_reported_not_deleted(tmp_path):
    autod = tmp_path / "auto"
    old = touch(autod / ".jobhunter-auto-20261001T030000Z.db.ab_c1.tmp")
    fresh = touch(autod / ".jobhunter-auto-20261010T030000Z.db.zz9.tmp")
    pre = touch(autod / ".pre-migrate-28-33-20261001T000000Z.db.q1.tmp")
    other = touch(autod / ".something.tmp")
    past = (NOW - timedelta(days=2)).timestamp()
    for p in (old, pre, other):
        os.utime(p, (past, past))
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
    assert backup.leftover_temp_files(autod, NOW) == sorted([old, pre])
    backup.prune_auto(autod, NOW)
    assert all(p.exists() for p in (old, fresh, pre, other))
