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
_MIGRATION_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the project's PRAGMAs. Accepts ``":memory:"``."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: autocommit; transactions are explicit via transaction().
    conn = sqlite3.connect(str(path), isolation_level=None)
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
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version numbers")
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
        with transaction(conn):
            for stmt in _split_statements(script):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
        applied.append(version)
    return applied
