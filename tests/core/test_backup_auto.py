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
