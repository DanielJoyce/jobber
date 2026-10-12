"""The ``achievement_setting`` key/value table (specs/021 "Data", migration 0035).

Keys: ``enabled``, ``notify``, ``hidden_categories``, ``backfilled_at`` (read and written by
the achievements module, A2) and ``mail_setup_at`` (written by ``jobhunter mail setup``, A1).
Stored in the database, not ``config.toml`` (read-only in the container) and not
``preferences.yaml`` (it feeds ``filter_version``).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

MAIL_SETUP_AT = "mail_setup_at"


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM achievement_setting WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def put(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    """Upsert one setting (a single statement: atomic on its own or inside a caller's
    transaction)."""
    conn.execute(
        "INSERT INTO achievement_setting (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now.astimezone(UTC).isoformat()),
    )


def record_mail_setup(conn: sqlite3.Connection, now: datetime) -> None:
    """``jobhunter mail setup`` succeeded (not ``--dry-run``). The first success is kept, so
    the achievement's crossing date is when you first did it; re-runs change nothing."""
    at = now.astimezone(UTC).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO achievement_setting (key, value, updated_at) VALUES (?, ?, ?)",
        (MAIL_SETUP_AT, at, at),
    )
