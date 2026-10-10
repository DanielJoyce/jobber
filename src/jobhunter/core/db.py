"""SQLite connection, migrations, FTS triggers (specs/005-data-model.md)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

MIGRATIONS_PACKAGE = "jobhunter.core.migrations"
FK_OFF_MARKER = "-- migrate: foreign_keys=off"
_MIGRATION_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the project's PRAGMAs. Accepts ``":memory:"``."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
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


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Returns versions applied."""
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
                    raise RuntimeError(
                        f"migration {version}_{name} left {len(bad)} foreign key violation(s)"
                    )
                conn.execute(
                    "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(UTC).isoformat()),
                )
        finally:
            if fk_off:
                conn.execute("PRAGMA foreign_keys=ON")
        applied.append(version)
    return applied
