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
from datetime import UTC, datetime
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
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return dest


def pre_migrate_name(from_version: int, to_version: int, now: datetime | None = None) -> str:
    return f"pre-migrate-{from_version}-{to_version}-{utc_stamp(now)}Z.db"


def prune_pre_migrate(directory: Path, keep: int = PRE_MIGRATE_KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` pre-migrate backups in ``directory``.

    Only files whose entire name matches :data:`PRE_MIGRATE_RE` are candidates; anything
    else in the directory (C6's daily backups, manual files) is never touched. Newest is by
    the UTC stamp in the name, not mtime (a copied file keeps neither). Returns what was deleted.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for entry in directory.iterdir():
        m = PRE_MIGRATE_RE.match(entry.name)
        if m and entry.is_file() and not entry.is_symlink():
            found.append((m.group(3), entry.name, entry))
    found.sort(reverse=True)
    removed = []
    for _, _, path in found[keep:]:
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
    prune_pre_migrate(directory)
    return dest
