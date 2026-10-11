"""``jobhunter db restore PATH`` (specs/018-containers.md#restore, ticket C6).

Host only; the CLI refuses in container mode, holds the run lock (C5) and checks that nothing
else has the database open before calling :func:`restore`, which:

1. copies the backup through the SQLite online backup API into a staging file **inside the
   data directory** (so it carries the directory's SELinux label), with ``integrity_check``
   (C12's :func:`jobhunter.core.backup.online_backup`);
2. refuses a backup holding a migration this code does not know (the C12 guard) or one with
   no jobhunter schema at all; an older schema is allowed and reported;
3. backs up the current database under a manual name (``backups/jobhunter-<UTC>Z-pre-restore.db``,
   never pruned);
4. writes the staged copy into the live path **through the backup API**, never a rename or
   copy over the file: other connections (and the ``-wal``/``-shm`` files) stay coherent and
   see the restored content on their next read;
5. runs ``integrity_check`` on the live database and reports row counts before and after.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from jobhunter.core import backup, db
from jobhunter.ops.migrate_paths import other_holders
from jobhunter.xdg import mkdir_private

BUSY_TIMEOUT_MS = 30_000
STOP_HINT = (
    "Stop the console and timers first. Containers: systemctl --user stop jobhunter-console "
    "jobhunter-ctr-run.timer jobhunter-ctr-collect.timer jobhunter-ctr-verify.timer (and wait "
    "for any running jobhunter-ctr-* service). Host: Ctrl-C `jobhunter console`, and "
    "systemctl --user stop jobhunter-run.timer jobhunter-collect.timer jobhunter-verify.timer"
)


class RestoreError(RuntimeError):
    """The restore was refused or failed; the message says what state things are in."""


@dataclass
class RestoreResult:
    db_path: Path
    source: Path
    safety_backup: Path | None  # the current database, saved before it was overwritten
    source_version: int
    code_version: int
    before: dict[str, int] | None  # live row counts before (None: no database, or unreadable)
    after: dict[str, int]  # live row counts after
    expected: dict[str, int]  # row counts in the backup
    notes: list[str] = field(default_factory=list)

    @property
    def counts_match(self) -> bool:
        return self.after == self.expected


def console_processes(proc_root: Path = Path("/proc")) -> list[str]:
    """Running ``jobhunter console`` processes as "pid (cmdline)", host or container.

    Why a cmdline scan as well as open files: the console opens a database connection per
    request, so between requests it holds no file open. Rootless containers' processes are
    visible in the host's /proc. Linux only; elsewhere this finds nothing.
    """
    found: list[str] = []
    if not proc_root.is_dir():
        return found
    me = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if is_console_argv(argv):
            found.append(f"{entry.name} ({' '.join(argv)[:120]})")
    return found


def is_console_argv(argv: list[str]) -> bool:
    """``jobhunter console ...``, ``python /path/to/jobhunter console`` or ``python -m jobhunter
    console``: the first argument named ``jobhunter`` is followed somewhere by ``console``."""
    for i, arg in enumerate(argv):
        if Path(arg).name == "jobhunter":
            return "console" in argv[i + 1 :]
    return False


def busy_report(db_path: Path, proc_root: Path = Path("/proc")) -> list[str]:
    """Why the database is not idle: other processes with it open, and running consoles."""
    db_path = Path(db_path)
    who = [
        f"{h} has the database open"
        for h in other_holders([db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")])
    ]
    who += [f"console running: {c}" for c in console_processes(proc_root)]
    return who


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only (never creates it, never writes to it)."""
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _live_connect(path: Path) -> sqlite3.Connection:
    """The live database, without the schema guard: a newer live database is normal when
    rolling back to a pre-migrate backup."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and a.samefile(b)
    except OSError:
        return False


def _stage(source: Path, data_dir: Path, now: datetime) -> Path:
    """Copy ``source`` into a checked staging file inside ``data_dir``."""
    staging = Path(data_dir) / f".restore-{backup.utc_stamp(now)}Z.db"
    try:
        src = _ro_connect(source)
    except sqlite3.Error as exc:
        raise RestoreError(f"cannot open {source}: {exc}") from exc
    try:
        backup.online_backup(src, staging)
    except backup.BackupError as exc:  # not SQLite, unreadable, or fails integrity_check
        raise RestoreError(f"{source} is not a usable backup: {exc}. Nothing was changed.") from exc
    finally:
        src.close()
    return staging


def _check_schema(staged: sqlite3.Connection, source: Path) -> tuple[int, int]:
    """Refuse a backup this code cannot run; return (backup version, code version)."""
    applied = db.applied_versions(staged)
    if not applied:
        raise RestoreError(
            f"{source} has no jobhunter schema (no schema_version table). Nothing was changed."
        )
    known = {v for v, _, _ in db._load_migrations()}
    unknown = sorted(applied - known)
    code = max(known, default=0)
    if unknown:
        raise RestoreError(
            f"{source} is newer than this jobhunter (code has {code}, backup has "
            f"{max(applied)}; unknown migration(s): {', '.join(map(str, unknown))}). Update "
            "this checkout first. Nothing was changed."
        )
    return max(applied), code


def restore(
    source: Path,
    db_path: Path,
    data_dir: Path,
    *,
    now: datetime | None = None,
) -> RestoreResult:
    """Restore ``source`` into the live database at ``db_path``. See the module docstring.

    The caller holds the run lock and has checked that nothing else uses the database.
    ``data_dir`` is where the staging file and the safety backup go (``db.owning_data_dir``).
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    source = Path(source).expanduser().resolve()
    db_path = Path(db_path)
    if not source.is_file():
        raise RestoreError(f"no backup file at {source}")
    if any(_same_file(source, Path(f"{db_path}{s}")) for s in ("", "-wal", "-shm")):
        raise RestoreError(f"{source} is the live database itself; pick a backup file")
    mkdir_private(data_dir)
    staging = _stage(source, data_dir, now)
    try:
        staged = _ro_connect(staging)
        try:
            source_version, code_version = _check_schema(staged, source)
            expected = backup.table_counts(staged)
            return _write_live(
                staged, source, db_path, data_dir, now, source_version, code_version, expected
            )
        finally:
            staged.close()
    finally:
        with contextlib.suppress(FileNotFoundError):
            staging.unlink()


def _write_live(
    staged: sqlite3.Connection,
    source: Path,
    db_path: Path,
    data_dir: Path,
    now: datetime,
    source_version: int,
    code_version: int,
    expected: dict[str, int],
) -> RestoreResult:
    notes: list[str] = []
    existed = db_path.exists() and db_path.stat().st_size > 0
    if not db_path.exists():
        mkdir_private(db_path.parent)
        os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))
    live = _live_connect(db_path)
    try:
        before: dict[str, int] | None = None
        safety: Path | None = None
        if existed:
            with contextlib.suppress(sqlite3.DatabaseError):
                before = backup.table_counts(live)
            dest = backup.manual_dir(data_dir) / backup.manual_name(now, "restore")
            try:
                safety = backup.online_backup(live, dest)
            except (backup.BackupError, sqlite3.DatabaseError) as exc:
                raise RestoreError(
                    f"could not back up the current database first, so nothing was restored: "
                    f"{exc}. If it is damaged beyond backing up, stop everything, move "
                    f"{db_path} and its -wal and -shm files aside by hand, and run this again "
                    "(a missing database is restored into a new file)."
                ) from exc
        try:
            # pages=-1: one step, so the whole copy happens under one write lock and no other
            # connection can interleave a write; it waits (busy_timeout) for a current writer.
            staged.backup(live, pages=-1)
            if live.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                live.execute("PRAGMA journal_mode=WAL")  # match db.connect()
        except sqlite3.Error as exc:
            where = f"; the database before this restore is saved at {safety}" if safety else ""
            raise RestoreError(f"restore into {db_path} failed: {exc}{where}") from exc
    finally:
        live.close()

    check = _live_connect(db_path)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            where = f" The database before this restore is saved at {safety}." if safety else ""
            raise RestoreError(
                f"integrity_check after restoring into {db_path}: {row[0] if row else row}.{where}"
            )
        after = backup.table_counts(check)
    finally:
        check.close()

    if source_version < code_version:
        marker = db.deployment_marker(data_dir)
        if marker:
            notes.append(
                f"The backup is at schema {source_version} and this code has {code_version}. "
                f"{marker} marks a container deployment, so nothing migrates it on its own: "
                "it stays at that schema until you run `jobhunter upgrade` (or check out the "
                "matching commit)."
            )
        else:
            notes.append(
                f"The backup is at schema {source_version} and this code has {code_version}: "
                "the next jobhunter command migrates it (after a pre-migrate backup)."
            )
    result = RestoreResult(
        db_path=db_path,
        source=source,
        safety_backup=safety,
        source_version=source_version,
        code_version=code_version,
        before=before,
        after=after,
        expected=expected,
        notes=notes,
    )
    if not result.counts_match:
        notes.append(
            "Row counts differ from the backup's: something wrote to the database right after "
            "the restore (was it still running?)."
        )
    return result


def format_counts(result: RestoreResult) -> list[str]:
    """``table  before -> after`` lines, one per table in either database."""
    before = result.before or {}
    names = sorted(set(before) | set(result.after))
    width = max((len(n) for n in names), default=0)
    lines = []
    for name in names:
        b = before.get(name, "-") if result.before is not None else "?"
        lines.append(f"  {name:<{width}}  {b} -> {result.after.get(name, '-')}")
    return lines
