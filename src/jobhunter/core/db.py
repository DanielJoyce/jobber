"""SQLite connection, migrations, FTS triggers (specs/005-data-model.md).

Version skew (specs/018-containers.md#version-skew, ticket C12): every connection refuses a
database holding a migration this code does not know, so an old console or CLI stops the
moment a newer jobhunter moves the schema. ``migrate`` backs the database up before applying
anything (skipped for a new, version-0 database) and, while the deployment marker
``<data dir>/deployment.toml`` exists, refuses to migrate on its own: only an explicit
``jobhunter db migrate`` (run by ``jobhunter upgrade``) does.
"""

from __future__ import annotations

import functools
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from jobhunter.xdg import mkdir_private

MIGRATIONS_PACKAGE = "jobhunter.core.migrations"
FK_OFF_MARKER = "-- migrate: foreign_keys=off"
_MIGRATION_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")
DEPLOYMENT_MARKER = "deployment.toml"


class SchemaError(RuntimeError):
    """The database and this code disagree about the schema; the message says what to do."""


class SchemaTooNew(SchemaError):
    """The database holds migrations this code does not know (a newer jobhunter wrote it)."""

    def __init__(self, code_version: int, db_version: int, unknown: list[int]) -> None:
        self.code_version = code_version
        self.db_version = db_version
        self.unknown = unknown
        msg = (
            f"database is newer than this jobhunter (code has {code_version}, "
            f"database has {db_version})"
        )
        if db_version <= code_version:  # a lower-numbered migration from another branch
            msg += f"; unknown migration(s): {', '.join(map(str, unknown))}"
        super().__init__(
            msg + ". Update this checkout or image, or restore a backup that matches it."
        )


class MigrationsPending(SchemaError):
    """A container deployment's database needs migrating, and nothing migrates on its own."""

    def __init__(self, db_version: int, pending: list[int], marker: Path) -> None:
        self.db_version = db_version
        self.pending = pending
        self.marker = marker
        super().__init__(
            f"database schema is behind this jobhunter (database has {db_version}, code has "
            f"{code_version()}); {marker} marks a container deployment, where nothing migrates "
            "on its own: run `jobhunter upgrade`"
        )


class MigrationFailed(SchemaError):
    """A pre-migrate backup or a migration failed; nothing after it was applied."""


def owning_data_dir(db_path: Path, data_dir: Path) -> Path:
    """Where ``db_path``'s marker and ``backups/auto`` live: ``data_dir`` when the database is
    inside it (the default layout, and ``/data`` in a container), else the database's own
    directory, so migrating a copy elsewhere never writes or prunes the real backups."""
    db_dir = Path(db_path).resolve().parent
    owner = Path(data_dir).resolve()
    return owner if db_dir.is_relative_to(owner) else db_dir


def connect(path: Path | str, *, check_schema: bool = True) -> sqlite3.Connection:
    """Open a connection with the project's PRAGMAs. Accepts ``":memory:"``.

    A database created here is owner-only (0600; SQLite gives its -wal and -shm the same mode)
    in directories created owner-only (0700): it holds mail, contacts and applications.

    Raises :class:`SchemaTooNew` (after closing the connection) when the database holds a
    migration this code does not know. ``check_schema=False`` is for read-only reporting
    (``jobhunter version``) only.
    """
    if str(path) != ":memory:":
        mkdir_private(Path(path).parent)
        if not Path(path).exists():
            # An empty file is a valid empty database; creating it first sets the mode.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    # isolation_level=None: autocommit; transactions are explicit via transaction().
    # check_same_thread=False: FastAPI opens a request's connection in one threadpool
    # thread and may run the endpoint in another. Each connection is still owned by a
    # single request or CLI process and never used concurrently.
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    if check_schema:
        try:
            check_not_newer(conn)
        except BaseException:
            conn.close()
            raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; ROLLBACK on exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _load_migrations() -> list[tuple[int, str, str]]:
    """The migrations this process ships, read from disk once per process.

    Why once: the host install is editable, so the migrations folder is the live checkout. A
    long-running console must judge "known" by the code it loaded, not by files a later
    fast-forward put on disk; otherwise it keeps writing after another process applies
    them, and a bad file on disk would break it mid-run.
    """
    return list(_migrations_snapshot())


@functools.cache
def _migrations_snapshot() -> tuple[tuple[int, str, str], ...]:
    return tuple(_scan_migrations())


def _scan_migrations() -> list[tuple[int, str, str]]:
    """Read the migrations folder now (uncached)."""
    found: list[tuple[int, str, str]] = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), m.group(2), entry.read_text(encoding="utf-8")))
    found.sort()
    by_version: dict[int, list[str]] = {}
    for v, name, _ in found:
        by_version.setdefault(v, []).append(f"{v:04d}_{name}.sql")
    clashes = [", ".join(files) for files in by_version.values() if len(files) > 1]
    if clashes:
        # Parallel branches that each add a migration with the next number merge cleanly in
        # git; renumber all but one of these files to an unused version.
        raise RuntimeError(f"duplicate migration version numbers: {'; '.join(clashes)}")
    return found


def code_version() -> int:
    """The highest migration this code ships."""
    migrations = _load_migrations()
    return migrations[-1][0] if migrations else 0


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Versions recorded in ``schema_version`` (empty for a new database)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not exists:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def check_not_newer(conn: sqlite3.Connection) -> None:
    """Raise :class:`SchemaTooNew` if the database holds a migration this code lacks."""
    done = applied_versions(conn)
    if not done:
        return
    known = {v for v, _, _ in _load_migrations()}
    unknown = sorted(done - known)
    if unknown:
        raise SchemaTooNew(max(known, default=0), max(done), unknown)


def pending_versions(conn: sqlite3.Connection) -> list[int]:
    """Migrations this code has that the database has not applied, in order."""
    done = applied_versions(conn)
    return [v for v, _, _ in _load_migrations() if v not in done]


def database_file(conn: sqlite3.Connection) -> Path | None:
    """The main database's file, or None for an in-memory or temporary database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def deployment_marker(data_dir: Path) -> Path | None:
    """``<data dir>/deployment.toml`` if it exists (``container preflight`` writes it, C13).

    Its presence alone is the signal: a marker that cannot be parsed still stops automatic
    migration, which is the safe direction.
    """
    marker = Path(data_dir) / DEPLOYMENT_MARKER
    return marker if marker.exists() else None


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 if none."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def _split_statements(script: str) -> list[str]:
    """Split a script into statements (sqlite3.complete_statement handles trigger bodies)."""
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf)
            buf = ""
    if buf.strip():
        statements.append(buf)
    return statements


def migrate(
    conn: sqlite3.Connection,
    *,
    data_dir: Path | None = None,
    explicit: bool = False,
    now: datetime | None = None,
    on_backup: Callable[[Path], object] | None = None,
) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Returns versions applied.

    ``data_dir`` locates the deployment marker and ``backups/auto``; it defaults to the
    database file's directory (the same place unless ``db_path`` is overridden on its own).
    Before anything is applied:

    - a database holding an unknown migration raises :class:`SchemaTooNew`, writing nothing;
    - with the deployment marker present and ``explicit`` false, :class:`MigrationsPending`;
    - a file database that already has a schema is backed up through the online backup API
      to ``backups/auto/pre-migrate-<from>-<to>-<UTC>Z.db`` (newest 3 kept). A new database
      (version 0) or an in-memory one is not backed up.
    """
    check_not_newer(conn)
    pending = pending_versions(conn)
    if not pending:
        return []
    path = database_file(conn)
    if path is not None:  # an in-memory database has nothing to protect
        data_dir = path.parent if data_dir is None else data_dir
        from_version = current_version(conn)
        if not explicit and (marker := deployment_marker(data_dir)):
            raise MigrationsPending(from_version, pending, marker)
        if from_version > 0:
            from jobhunter.core import backup

            try:
                saved = backup.pre_migrate_backup(conn, data_dir, from_version, code_version(), now)
            except backup.BackupError as exc:
                raise MigrationFailed(
                    f"pre-migrate backup failed, so nothing was migrated: {exc}"
                ) from exc
            if on_backup is not None:
                on_backup(saved)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    # Apply every migration not yet recorded, not just those above the max. Parallel
    # branches can merge a lower-numbered migration after a higher one has shipped.
    done = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    applied: list[int] = []
    for version, name, script in _load_migrations():
        if version in done:
            continue
        # Table rebuilds (SQLite cannot alter a CHECK) need foreign_keys=OFF, and that PRAGMA is
        # a no-op inside a transaction. A migration opts in with FK_OFF_MARKER; we switch it off
        # before BEGIN, verify with foreign_key_check before COMMIT, and always restore it.
        fk_off = FK_OFF_MARKER in script
        if fk_off:
            conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with transaction(conn):
                for stmt in _split_statements(script):
                    conn.execute(stmt)
                if fk_off and (bad := conn.execute("PRAGMA foreign_key_check").fetchall()):
                    raise MigrationFailed(
                        f"migration {version}_{name} left {len(bad)} foreign key violation(s)"
                    )
                conn.execute(
                    "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            raise MigrationFailed(
                f"migration {version:04d}_{name} failed: {exc}; the database stays at version "
                f"{current_version(conn)} (earlier migrations in this run are kept); restore "
                "the pre-migrate backup in backups/auto or fix the migration"
            ) from exc
        finally:
            if fk_off:
                conn.execute("PRAGMA foreign_keys=ON")
        applied.append(version)
    return applied
