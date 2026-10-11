"""``jobhunter db restore PATH`` (specs/018-containers.md#restore, ticket C6).

Host only; the CLI refuses in container mode, holds the run lock (C5), holds the console
lock exclusive (refused while any console holds it shared, in any PID namespace) and checks
/proc for other processes using the database before calling :func:`restore`, which:

1. copies the backup file (and its ``-wal``, if any) byte for byte into a private temp
   directory **inside the data directory** (so it carries the directory's SELinux label, and
   a backup on a read-only snapshot or mount works and gets no ``-shm`` litter), then through
   the SQLite online backup API into a staging file, with ``integrity_check`` (C12's
   :func:`jobhunter.core.backup.online_backup`);
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
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobhunter.core import backup, db
from jobhunter.ops.migrate_paths import other_holders
from jobhunter.xdg import mkdir_private

BUSY_TIMEOUT_MS = 30_000
STAGING_PREFIX = ".restore-"
_STAGING_RE = re.compile(r"^\.restore-[A-Za-z0-9_]+$")
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
    """``jobhunter console ...``, ``python /path/to/jobhunter console``, ``python -m jobhunter
    console`` or ``python -m jobhunter.cli console``: the first argument naming jobhunter is
    followed somewhere by ``console``."""
    for i, arg in enumerate(argv):
        if Path(arg).name == "jobhunter" or (
            arg == "jobhunter.cli" and i > 0 and argv[i - 1] == "-m"
        ):
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


def clean_stale_staging(data_dir: Path) -> list[Path]:
    """Remove staging directories an interrupted restore left in ``data_dir`` (only names made
    by :func:`restore`). Safe only while holding the run lock, as the CLI does."""
    removed: list[Path] = []
    if not Path(data_dir).is_dir():
        return removed
    for entry in Path(data_dir).iterdir():
        if _STAGING_RE.match(entry.name) and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    return removed


def _stage(source: Path, workdir: Path) -> Path:
    """Copy ``source`` (and its -wal) into ``workdir``, then into a checked staging file there.

    The byte copy means SQLite never opens the user's file itself: a backup on a read-only
    path works even when its header says WAL, and nothing (-shm, -wal) is created beside it.
    """
    copy = workdir / "source.db"
    try:
        shutil.copyfile(source, copy)
        wal = Path(f"{source}-wal")
        if wal.is_file():
            shutil.copyfile(wal, Path(f"{copy}-wal"))
    except OSError as exc:
        if exc.filename is not None and str(workdir) in str(exc.filename):
            raise RestoreError(
                f"cannot write the staging copy in {workdir.parent} ({exc}); is the disk full? "
                "Nothing was changed."
            ) from exc
        raise RestoreError(f"cannot read {source}: {exc}. Nothing was changed.") from exc
    try:
        src = sqlite3.connect(str(copy), isolation_level=None)
    except sqlite3.Error as exc:
        raise RestoreError(f"cannot open {source}: {exc}. Nothing was changed.") from exc
    try:
        return backup.online_backup(src, workdir / "staged.db")
    except (backup.BackupError, OSError) as exc:  # not SQLite, or fails integrity_check
        raise RestoreError(f"{source} is not a usable backup: {exc}. Nothing was changed.") from exc
    finally:
        src.close()


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
    on_safety: Callable[[Path], object] | None = None,
) -> RestoreResult:
    """Restore ``source`` into the live database at ``db_path``. See the module docstring.

    The caller holds the run lock and has checked that nothing else uses the database.
    ``data_dir`` is where the staging directory and the safety backup go
    (``db.owning_data_dir``). ``on_safety(path)`` is called as soon as the current database
    is saved, before the live database is touched, so the caller can tell the user even if
    the restore is then interrupted.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    source = Path(source).expanduser().resolve()
    db_path = Path(db_path)
    if not source.is_file():
        raise RestoreError(f"no backup file at {source}")
    if any(_same_file(source, Path(f"{db_path}{s}")) for s in ("", "-wal", "-shm")):
        raise RestoreError(f"{source} is the live database itself; pick a backup file")
    mkdir_private(data_dir)
    workdir = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=data_dir))  # 0700
    try:
        staged = _live_connect(_stage(source, workdir))
        try:
            source_version, code_version = _check_schema(staged, source)
            expected = backup.table_counts(staged)
            return _write_live(
                staged,
                source,
                db_path,
                data_dir,
                now,
                source_version,
                code_version,
                expected,
                on_safety,
            )
        finally:
            staged.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _free_name(directory: Path, name: str) -> Path:
    """``directory/name``, or ``...-2.db``, ``...-3.db`` if taken: an earlier safety copy (two
    restores in one second) is never replaced."""
    stem = name.removesuffix(".db")
    candidate, n = directory / name, 1
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = directory / f"{stem}-{n}.db"
    return candidate


def _check_page_size(staged: sqlite3.Connection, live: sqlite3.Connection, db_path: Path) -> None:
    """SQLite cannot back up into a WAL database with a different page size (it reports a
    misleading "readonly database"); say what is going on instead, before anything changes."""
    try:
        mode = live.execute("PRAGMA journal_mode").fetchone()[0]
        theirs = staged.execute("PRAGMA page_size").fetchone()[0]
        ours = live.execute("PRAGMA page_size").fetchone()[0]
    except sqlite3.DatabaseError:
        return  # a damaged live database: the safety backup step reports it
    if mode == "wal" and theirs != ours:
        raise RestoreError(
            f"the backup's page size ({theirs}) differs from the live database's ({ours}), and "
            "SQLite cannot restore into a WAL database with a different page size. Stop "
            f"everything, move {db_path} and its -wal and -shm files aside, and run this again "
            "(a missing database is restored into a new file). Nothing was changed."
        )


def _write_live(
    staged: sqlite3.Connection,
    source: Path,
    db_path: Path,
    data_dir: Path,
    now: datetime,
    source_version: int,
    code_version: int,
    expected: dict[str, int],
    on_safety: Callable[[Path], object] | None,
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
            _check_page_size(staged, live, db_path)
            with contextlib.suppress(sqlite3.DatabaseError):
                before = backup.table_counts(live)
            dest = _free_name(backup.manual_dir(data_dir), backup.manual_name(now, "restore"))
            try:
                safety = backup.online_backup(live, dest)
            except (backup.BackupError, sqlite3.DatabaseError) as exc:
                raise RestoreError(
                    f"could not back up the current database first, so nothing was restored: "
                    f"{exc}. If it is damaged beyond backing up, stop everything, move "
                    f"{db_path} and its -wal and -shm files aside by hand, and run this again "
                    "(a missing database is restored into a new file)."
                ) from exc
            if on_safety is not None:
                on_safety(safety)
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

    where = f" The database before this restore is saved at {safety}." if safety else ""
    check = _live_connect(db_path)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise RestoreError(
                f"integrity_check after restoring into {db_path}: {row[0] if row else row}.{where}"
            )
        after = backup.table_counts(check)
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"checking {db_path} after the restore failed: {exc}.{where}") from exc
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
