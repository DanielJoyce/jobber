"""``jobhunter db restore`` internals (specs/018-containers.md#restore, C6)."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobhunter.core import backup, db
from jobhunter.ops import restore as rst

REAL = db._load_migrations()
NEWEST = REAL[-1][0]
OLDER = REAL[-3][0]
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def make_db(path, monkeypatch, version=NEWEST, rows=0, tag="db"):
    """A jobhunter database at ``version`` with ``rows`` rows in a ``scratch`` table."""
    monkeypatch.setattr(db, "_load_migrations", lambda: [m for m in REAL if m[0] <= version])
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("CREATE TABLE scratch(n INTEGER, tag TEXT)")
    conn.executemany("INSERT INTO scratch VALUES (?, ?)", [(i, tag) for i in range(rows)])
    conn.close()
    monkeypatch.setattr(db, "_load_migrations", lambda: REAL)
    return path


def backup_of(live, dest):
    conn = sqlite3.connect(live)
    try:
        return backup.online_backup(conn, dest)
    finally:
        conn.close()


def scratch(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*), group_concat(DISTINCT tag) FROM scratch").fetchone()
    finally:
        conn.close()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """data/jobhunter.db with 10 'old' rows saved to a backup, then 25 more 'new' rows that
    sit uncheckpointed in the -wal while a connection stays open."""
    data = tmp_path / "data"
    live = make_db(data / "jobhunter.db", monkeypatch, rows=10, tag="old")
    saved = backup_of(live, tmp_path / "elsewhere" / "saved.db")
    keeper = sqlite3.connect(live, isolation_level=None)
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.executemany("INSERT INTO scratch VALUES (?, 'new')", [(i,) for i in range(25)])
    assert os.path.getsize(f"{live}-wal") > 0
    yield data, live, saved, keeper
    keeper.close()


def test_restore_over_an_uncheckpointed_wal(layout):
    data, live, saved, keeper = layout
    before_ino = os.stat(live).st_ino
    result = rst.restore(saved, live, data, now=NOW)

    assert scratch(live) == (10, "old")
    assert os.stat(live).st_ino == before_ino  # written in place, never renamed over
    # The open connection sees the restored data on its next read, not a stale copy.
    assert keeper.execute("SELECT count(*) FROM scratch").fetchone()[0] == 10
    conn = sqlite3.connect(live)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()

    # The safety backup holds the pre-restore state, including the rows that were only in the
    # -wal, under a manual name nothing prunes.
    assert result.safety_backup == data / "backups" / "jobhunter-20261010T120000Z-pre-restore.db"
    assert stat.S_IMODE(os.stat(result.safety_backup).st_mode) == 0o600
    assert scratch(result.safety_backup) == (35, "old,new")
    assert result.before["scratch"] == 35 and result.after["scratch"] == 10
    assert result.counts_match and result.notes == []
    # No staging file left behind in the data dir.
    assert sorted(p.name for p in data.iterdir() if p.name.startswith(".")) == []


def test_restore_while_another_connection_reads_and_writes(layout):
    data, live, saved, _keeper = layout
    stop = threading.Event()
    errors: list[BaseException] = []
    written: list[int] = []
    reads: list[int] = []

    def writer():
        conn = sqlite3.connect(live, isolation_level=None, timeout=30)
        try:
            i = 0
            while not stop.is_set():
                conn.execute("INSERT INTO scratch VALUES (?, 'writer')", (i,))
                written.append(i)
                i += 1
                time.sleep(0.001)
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    def reader():
        conn = sqlite3.connect(live, isolation_level=None, timeout=30)
        try:
            while not stop.is_set():
                reads.append(conn.execute("SELECT count(*) FROM scratch").fetchone()[0])
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    while len(written) < 20:
        time.sleep(0.005)
    result = rst.restore(saved, live, data, now=NOW)
    time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(10)

    assert errors == []
    conn = sqlite3.connect(live)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    tags = dict(conn.execute("SELECT tag, count(*) FROM scratch GROUP BY tag").fetchall())
    conn.close()
    # Everything from before the restore is gone ('new' rows, early writer rows); the backup's
    # rows are all there; only rows written after the restore were added on top.
    assert "new" not in tags
    assert tags["old"] == 10
    assert tags.get("writer", 0) < len(written)
    assert scratch(result.safety_backup)[0] >= 35 + 20


def test_a_backup_newer_than_the_code_is_refused_and_nothing_changes(layout, tmp_path):
    data, live, saved, _keeper = layout
    raw = sqlite3.connect(saved)
    raw.execute("INSERT INTO schema_version VALUES (?, 'future', 't')", (NEWEST + 1,))
    raw.commit()
    raw.close()
    with pytest.raises(rst.RestoreError, match="newer than this jobhunter"):
        rst.restore(saved, live, data, now=NOW)
    assert scratch(live) == (35, "old,new")
    assert not (data / "backups").exists()  # refused before the safety backup
    assert sorted(p.name for p in data.iterdir() if p.name.startswith(".")) == []


@pytest.mark.parametrize("marker", [False, True])
def test_an_older_backup_is_restored_and_the_note_says_what_happens_next(
    tmp_path, monkeypatch, marker
):
    data = tmp_path / "data"
    live = make_db(data / "jobhunter.db", monkeypatch, rows=4, tag="current")
    old = make_db(tmp_path / "old" / "old.db", monkeypatch, version=OLDER, rows=2, tag="old")
    old_backup = backup_of(old, tmp_path / "old" / "old-backup.db")
    if marker:
        (data / "deployment.toml").write_text('mode = "container"\n')
    result = rst.restore(old_backup, live, data, now=NOW)
    assert result.source_version == OLDER and result.code_version == NEWEST
    conn = sqlite3.connect(live)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == OLDER
    conn.close()  # restore never migrates
    [note] = result.notes
    if marker:
        assert "jobhunter upgrade" in note and "nothing migrates it" in note
    else:
        assert "next jobhunter command migrates it" in note


def test_rolling_back_a_newer_live_database_to_a_pre_migrate_backup(tmp_path, monkeypatch):
    """The live database may be newer than this code (rolled-back checkout): still restorable."""
    data = tmp_path / "data"
    live = make_db(data / "jobhunter.db", monkeypatch, rows=1)
    good = backup_of(live, tmp_path / "pre-migrate.db")
    raw = sqlite3.connect(live)
    raw.execute("INSERT INTO schema_version VALUES (?, 'future', 't')", (NEWEST + 1,))
    raw.commit()
    raw.close()
    result = rst.restore(good, live, data, now=NOW)
    conn = db.connect(live)  # the guard accepts it again
    assert db.current_version(conn) == NEWEST
    conn.close()
    assert result.safety_backup is not None


def test_not_a_database_or_the_live_file_itself_is_refused(layout, tmp_path):
    data, live, _saved, _keeper = layout
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite at all" * 100)
    with pytest.raises(rst.RestoreError, match="not a usable backup"):
        rst.restore(junk, live, data, now=NOW)
    with pytest.raises(rst.RestoreError, match="live database itself"):
        rst.restore(live, live, data, now=NOW)
    with pytest.raises(rst.RestoreError, match="no backup file"):
        rst.restore(tmp_path / "missing.db", live, data, now=NOW)
    empty = sqlite3.connect(tmp_path / "empty.db")
    empty.execute("CREATE TABLE other(x)")
    empty.close()
    with pytest.raises(rst.RestoreError, match="no jobhunter schema"):
        rst.restore(tmp_path / "empty.db", live, data, now=NOW)
    assert scratch(live) == (35, "old,new")
    assert not (data / "backups").exists()


def test_restore_into_a_missing_database_creates_it_private(layout, tmp_path):
    data, _live, saved, _keeper = layout
    target = data / "fresh" / "jobhunter.db"
    result = rst.restore(saved, target, data, now=NOW)
    assert result.safety_backup is None and result.before is None
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert scratch(target) == (10, "old")
    conn = sqlite3.connect(target)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_is_console_argv():
    assert rst.is_console_argv(["/home/u/.local/bin/jobhunter", "console"])
    assert rst.is_console_argv(["/app/.venv/bin/python", "/app/.venv/bin/jobhunter", "console"])
    assert rst.is_console_argv(["python", "-m", "jobhunter", "console", "--port", "8808"])
    assert not rst.is_console_argv(["jobhunter", "db", "restore", "x.db"])
    assert not rst.is_console_argv(["vim", "console"])
    assert not rst.is_console_argv(["jobhunter-console-notes", "console"])
    assert not rst.is_console_argv([])


def test_console_processes_reads_proc(tmp_path):
    proc = tmp_path / "proc"
    for pid, argv in {
        "101": [b"/usr/bin/python3", b"/home/u/.local/bin/jobhunter", b"console"],
        "102": [b"/usr/bin/bash"],
        "103": [b"jobhunter", b"run"],
        "self": [b"jobhunter", b"console"],  # not a pid
    }.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (proc / "104").mkdir()  # cmdline unreadable / gone
    found = rst.console_processes(proc)
    assert len(found) == 1 and found[0].startswith("101 (")


def test_busy_report_sees_another_process_holding_the_database(tmp_path, monkeypatch):
    live = make_db(tmp_path / "jobhunter.db", monkeypatch)
    empty_proc = tmp_path / "noproc"
    empty_proc.mkdir()
    assert rst.busy_report(live, proc_root=empty_proc) == []
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
            "c.execute('SELECT count(*) FROM schema_version').fetchone(); "
            "print('ready', flush=True); sys.stdin.read()",
            str(live),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        busy = rst.busy_report(live, proc_root=empty_proc)
        assert len(busy) == 1 and busy[0].startswith(f"{holder.pid} (")
    finally:
        holder.stdin.close()
        holder.wait(10)


def test_python_m_jobhunter_cli_console_is_a_console():
    assert rst.is_console_argv(["python3", "-m", "jobhunter.cli", "console"])
    assert not rst.is_console_argv(["python3", "jobhunter.cli", "console"])


def test_a_wal_header_backup_on_a_read_only_path_restores_without_litter(layout, tmp_path):
    data, live, saved, _keeper = layout
    ro = tmp_path / "snapshot"
    ro.mkdir()
    legacy = ro / "legacy.db"
    # A raw copy of a checkpointed WAL database: its header says WAL, and it has no -wal.
    src = sqlite3.connect(saved)
    src.execute("PRAGMA journal_mode=WAL")
    src.close()
    legacy.write_bytes(saved.read_bytes())
    assert legacy.read_bytes()[18:20] == b"\x02\x02"
    legacy.chmod(0o400)
    ro.chmod(0o500)
    try:
        rst.restore(legacy, live, data, now=NOW)
        assert sorted(p.name for p in ro.iterdir()) == ["legacy.db"]
    finally:
        ro.chmod(0o700)
    assert scratch(live) == (10, "old")


def test_a_source_with_its_own_wal_is_restored_with_the_wal_rows(layout, tmp_path, monkeypatch):
    data, live, _saved, _keeper = layout
    other = make_db(tmp_path / "other" / "jobhunter.db", monkeypatch, rows=2, tag="main")
    holder = sqlite3.connect(other, isolation_level=None)
    holder.execute("PRAGMA wal_autocheckpoint=0")
    holder.executemany("INSERT INTO scratch VALUES (?, 'walonly')", [(i,) for i in range(3)])
    # Snapshot the files while the -wal still holds the last rows.
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "x.db").write_bytes(other.read_bytes())
    (snap / "x.db-wal").write_bytes(Path(f"{other}-wal").read_bytes())
    holder.close()
    rst.restore(snap / "x.db", live, data, now=NOW)
    assert scratch(live) == (5, "main,walonly")
    assert sorted(p.name for p in snap.iterdir()) == ["x.db", "x.db-wal"]  # untouched


def test_a_page_size_mismatch_is_explained_and_nothing_changes(layout, tmp_path):
    data, live, saved, _keeper = layout
    b8 = tmp_path / "b8.db"
    b8.write_bytes(saved.read_bytes())  # a DELETE-mode backup: VACUUM can change its page size
    conn = sqlite3.connect(b8)
    conn.execute("PRAGMA page_size=8192")
    conn.execute("VACUUM")
    conn.close()
    conn = sqlite3.connect(b8)
    assert conn.execute("PRAGMA page_size").fetchone()[0] == 8192
    conn.close()
    with pytest.raises(rst.RestoreError, match="page size"):
        rst.restore(tmp_path / "b8.db", live, data, now=NOW)
    assert scratch(live) == (35, "old,new")
    assert not (data / "backups").exists()


def test_stale_staging_dirs_are_cleaned_but_nothing_else(tmp_path):
    (tmp_path / ".restore-abc123").mkdir()
    (tmp_path / ".restore-abc123" / "staged.db").write_text("x")
    (tmp_path / ".restore-notes.txt").write_text("keep")  # a file, not ours
    (tmp_path / "restore-abc").mkdir()
    removed = rst.clean_stale_staging(tmp_path)
    assert [p.name for p in removed] == [".restore-abc123"]
    assert (tmp_path / ".restore-notes.txt").exists() and (tmp_path / "restore-abc").exists()


def test_safety_is_reported_before_the_live_database_is_touched(layout):
    data, live, saved, _keeper = layout
    seen = []

    def on_safety(path):
        seen.append((path.exists(), scratch(live)))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        rst.restore(saved, live, data, now=NOW, on_safety=on_safety)
    assert seen == [(True, (35, "old,new"))]
    assert scratch(live) == (35, "old,new")
    assert [p.name for p in data.iterdir() if p.name.startswith(".restore-")] == []
