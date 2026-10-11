"""Online SQLite backups (specs/018-containers.md#backups, tickets C12 and C6).

``online_backup`` is the one helper every backup goes through: the SQLite online backup API
(safe while other processes hold the WAL database open), written to a temp file beside the
destination, checked with ``integrity_check``, made owner-only, then renamed into place. A
copy of the live ``jobhunter.db`` file (or a rename over it) would miss the ``-wal`` contents.

Automatic backups live in ``<data dir>/backups/auto/``, apart from the manual
``backups/jobhunter-*-pre-<what>.db`` rollback points, which nothing here ever touches.
Pruning deletes only files whose whole name matches one exact pattern in that directory.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunter.xdg import mkdir_private

AUTO_SUBDIR = Path("backups") / "auto"
PRE_MIGRATE_KEEP = 3
PRE_MIGRATE_RE = re.compile(r"^pre-migrate-(\d+)-(\d+)-(\d{8}T\d{6})Z\.db$")


class BackupError(RuntimeError):
    """A backup could not be written or failed its integrity check."""


def auto_dir(data_dir: Path) -> Path:
    """``<data dir>/backups/auto``: automatic backups only, never the manual ones."""
    return Path(data_dir) / AUTO_SUBDIR


def utc_stamp(now: datetime | None = None) -> str:
    """``YYYYMMDDTHHMMSS`` in UTC (the name suffix adds the ``Z``)."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S")


def online_backup(src: sqlite3.Connection, dest: Path) -> Path:
    """Copy the database behind ``src`` into ``dest`` through the online backup API.

    The copy is written to a temp file in ``dest``'s directory (so it lands on the same
    filesystem, with the directory's SELinux label), switched out of WAL mode so it is one
    self-contained file, checked with ``PRAGMA integrity_check``, chmod 0600, then renamed
    over ``dest``. On any failure nothing is left at ``dest`` and :class:`BackupError` is
    raised. Returns ``dest``.
    """
    dest = Path(dest)
    mkdir_private(dest.parent)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        os.chmod(tmp, 0o600)
        target = sqlite3.connect(tmp)
        try:
            src.backup(target)
            # The copied header still says WAL; a backup should be one file with no -wal.
            target.execute("PRAGMA journal_mode=DELETE")
            row = target.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise BackupError(f"backup to {dest} failed: {exc}") from exc
        finally:
            target.close()
        if not row or row[0] != "ok":
            raise BackupError(f"backup to {dest} failed integrity_check: {row[0] if row else row}")
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)  # the rename itself is durable before anyone relies on it
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return dest


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it survives a power cut (best effort)."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def pre_migrate_name(from_version: int, to_version: int, now: datetime | None = None) -> str:
    return f"pre-migrate-{from_version}-{to_version}-{utc_stamp(now)}Z.db"


def prune_pre_migrate(
    directory: Path, keep: int = PRE_MIGRATE_KEEP, protect_below: int | None = None
) -> list[Path]:
    """Delete all but the newest ``keep`` pre-migrate backups in ``directory``.

    Only files whose entire name matches :data:`PRE_MIGRATE_RE` are candidates; anything
    else in the directory (C6's daily backups, manual files) is never touched. Newest is by
    the UTC stamp in the name, not mtime (a copied file keeps neither). Returns what was deleted.

    ``protect_below`` (the database's current version): also keep the newest backup whose
    ``<from>`` is below it, whatever its ``<to>``. Why: migrations commit one by one, so a run
    that fails partway leaves the database between versions, and every automatic retry (each
    command, each timer, and later merges that raise the target) backs up the half-migrated
    state. Without this, a few retries would prune the only copy from before the upgrade, the
    one an older checkout can still open.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for entry in directory.iterdir():
        m = PRE_MIGRATE_RE.match(entry.name)
        if m and entry.is_file() and not entry.is_symlink():
            found.append((m.group(3), entry.name, entry, int(m.group(1)), int(m.group(2))))
    found.sort(reverse=True)
    protected = None
    if protect_below is not None:
        older = [f for f in found if f[3] < protect_below]  # found is newest first
        if older:
            protected = older[0][2]
    removed = []
    for _, _, path, _, _ in found[keep:]:
        if path == protected:
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed.append(path)
    return removed


def pre_migrate_backup(
    conn: sqlite3.Connection,
    data_dir: Path,
    from_version: int,
    to_version: int,
    now: datetime | None = None,
) -> Path:
    """Back up before migrating ``from_version`` to ``to_version``, then prune to the newest 3."""
    directory = auto_dir(data_dir)
    dest = online_backup(conn, directory / pre_migrate_name(from_version, to_version, now))
    prune_pre_migrate(directory, protect_below=from_version)
    return dest


# ─── automatic and manual backups (C6) ──────────────────────────────────────

AUTO_RE = re.compile(r"^jobhunter-auto-(\d{8}T\d{6})Z\.db$")
AUTO_KEEP_DAILY = 7
AUTO_KEEP_WEEKLY = 4
MANUAL_SUBDIR = Path("backups")
# A stamp further ahead of the clock than this is not trusted to rank backups (see prune_auto).
FUTURE_SLACK = timedelta(days=1)


def auto_name(now: datetime | None = None) -> str:
    """``jobhunter-auto-YYYYMMDDTHHMMSSZ.db`` (UTC)."""
    return f"jobhunter-auto-{utc_stamp(now)}Z.db"


def manual_name(now: datetime | None = None, what: str | None = None) -> str:
    """``jobhunter-YYYYMMDDTHHMMSSZ.db``, or ``jobhunter-...Z-pre-<what>.db`` (UTC).

    Never matches :data:`AUTO_RE` or :data:`PRE_MIGRATE_RE`, so nothing ever prunes it.
    """
    suffix = f"-pre-{what}" if what else ""
    return f"jobhunter-{utc_stamp(now)}Z{suffix}.db"


def manual_dir(data_dir: Path) -> Path:
    """``<data dir>/backups``: the manual rollback points (never pruned)."""
    return Path(data_dir) / MANUAL_SUBDIR


def auto_backups(directory: Path) -> list[tuple[datetime, Path]]:
    """Regular files in ``directory`` whose whole name matches :data:`AUTO_RE` and whose stamp
    is a real date, newest first. Symlinks, directories and near-misses are left out."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found: list[tuple[datetime, Path]] = []
    for entry in directory.iterdir():
        m = AUTO_RE.match(entry.name)
        if not m or entry.is_symlink() or not entry.is_file():
            continue
        try:
            stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue  # the right shape but no real date: not ours to judge, never deleted
        found.append((stamp, entry))
    found.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return found


def auto_keep(
    stamps: list[datetime],
    daily: int = AUTO_KEEP_DAILY,
    weekly: int = AUTO_KEEP_WEEKLY,
) -> set[int]:
    """Indices into ``stamps`` (sorted newest first) of the backups 7 daily + 4 weekly keeps.

    Daily: the newest backup of each of the ``daily`` most recent UTC days that have one.
    Weekly: the newest of each of the ``weekly`` most recent ISO weeks that have one. The two
    are counted independently and the union is kept (as ``restic forget --keep-daily 7
    --keep-weekly 4`` does). Days and weeks without a backup do not use up a slot, so a machine
    that was off for a month still keeps its last 7 backups instead of pruning down to none.
    """
    keep: set[int] = set()
    buckets: tuple[tuple[int, Callable[[datetime], object]], ...] = (
        (daily, lambda s: s.date()),
        (weekly, lambda s: tuple(s.isocalendar())[:2]),
    )
    for width, key in buckets:
        seen: set[object] = set()
        for i, stamp in enumerate(stamps):
            bucket = key(stamp)
            if bucket in seen:
                continue
            if len(seen) >= width:
                break
            seen.add(bucket)
            keep.add(i)
    return keep


def prune_auto(
    directory: Path,
    now: datetime | None = None,
    daily: int = AUTO_KEEP_DAILY,
    weekly: int = AUTO_KEEP_WEEKLY,
    protect: Path | None = None,
) -> list[Path]:
    """Delete the automatic backups in ``directory`` that 7 daily + 4 weekly does not keep.

    Only regular files whose entire name matches :data:`AUTO_RE` are candidates: pre-migrate
    backups (C12's, pruned by :func:`prune_pre_migrate`), manual backups and anything else in
    the directory are never touched, and nothing outside ``directory`` is looked at. Ranking is
    by the UTC stamp in the name, not mtime. A file stamped more than a day in the future (the
    clock was wrong when it was written) is kept and takes no slot, so it cannot push real
    backups out. ``protect`` (the backup just written) is never deleted.
    Returns what was deleted.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    found = [(s, p) for s, p in auto_backups(directory) if s <= now + FUTURE_SLACK]
    keep = auto_keep([s for s, _ in found], daily, weekly)
    removed: list[Path] = []
    for i, (_, path) in enumerate(found):
        if i in keep or (protect is not None and path == Path(protect)):
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed.append(path)
    return removed


def auto_backup(
    conn: sqlite3.Connection, data_dir: Path, now: datetime | None = None
) -> tuple[Path, list[Path]]:
    """``db backup --auto``: write ``backups/auto/jobhunter-auto-<UTC>Z.db``, then prune.

    Returns the new backup and the pruned files. Nothing is pruned when the backup fails.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    directory = auto_dir(data_dir)
    dest = online_backup(conn, directory / auto_name(now))
    return dest, prune_auto(directory, now, protect=dest)


LEFTOVER_TEMP_RE = re.compile(r"^\.(jobhunter-auto-\d{8}T\d{6}Z|pre-migrate-.+Z)\.db\..+\.tmp$")
LEFTOVER_AGE = timedelta(hours=6)


def leftover_temp_files(directory: Path, now: datetime | None = None) -> list[Path]:
    """Temp files that a killed backup left in ``directory`` (older than 6 h, so not one being
    written now). Reported, never deleted: pruning only ever deletes the two exact backup
    name patterns (specs/018 Backups)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for entry in directory.iterdir():
        if not LEFTOVER_TEMP_RE.match(entry.name) or entry.is_symlink() or not entry.is_file():
            continue
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, UTC)
        if now - mtime > LEFTOVER_AGE:
            found.append(entry)
    return sorted(found)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count of every ordinary table in the main schema, by name (no FTS shadow or
    virtual tables, no ``sqlite_*``)."""
    names = [
        row[1]
        for row in conn.execute("PRAGMA main.table_list")
        if row[2] == "table" and not row[1].startswith("sqlite_")
    ]
    counts: dict[str, int] = {}
    for name in sorted(names):
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = conn.execute(f"SELECT count(*) FROM main.{quoted}").fetchone()[0]
    return counts
