"""Per-packet answers: the only writer of ``packet_answer`` (specs/017 "Never-store list").

Phase 1b stores the inputs drafting needs here: employer notes (``notes:employer``, sent with a
cover letter or a "Why us?" draft, numbered ``N1``-``N3``) and a behavioral question's story
facts (``story:<question key>``, numbered ``S1``-``S3``). Every write runs the never-store
match on the label first and raises :class:`NeverStore` with nothing written. Phase 1d adds
Save as answer (``q:<label>``), reuse and Promote to /prefs on top of this same function.
"""

from __future__ import annotations

import sqlite3

from jobhunter.apply import labels

NOTES_KEY = "notes:employer"
MAX_LINES = 3
MAX_LINE_CHARS = 500


class NeverStore(ValueError):
    """A write refused by the never-store list. ``str()`` is the message for the page."""


def story_key(question: str) -> str:
    return f"story:{labels.question_key(question)}"


def clean_lines(raw: str | list[str]) -> list[str]:
    """One to three non-empty lines, each trimmed and capped."""
    items = raw.splitlines() if isinstance(raw, str) else list(raw)
    out = [" ".join(x.split())[:MAX_LINE_CHARS] for x in items if x and x.strip()]
    if len(out) > MAX_LINES:
        raise ValueError(f"at most {MAX_LINES} lines, please")
    return out


def save_packet_answer(
    conn: sqlite3.Connection,
    packet_id: int,
    field_key: str,
    label: str,
    value: str,
    source: str = "user",
) -> None:
    """Insert or replace one ``packet_answer``. Raises :class:`NeverStore` on a match of the
    label (or the question inside a ``story:``/``q:`` key) against the never-store list."""
    for text in (label, field_key.partition(":")[2]):
        if (m := labels.never_store(text)) is not None:
            raise NeverStore(f"'{label}' is on the never-store list ({m.category}). {labels.YOURS}")
    if source not in ("draft", "user"):
        raise ValueError(f"bad source {source!r}")
    conn.execute(
        "INSERT INTO packet_answer (packet_id, field_key, label, value, source) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (packet_id, field_key) DO UPDATE SET "
        "label = excluded.label, value = excluded.value, source = excluded.source",
        (packet_id, field_key, label, value, source),
    )


def get_lines(conn: sqlite3.Connection, packet_id: int, field_key: str) -> list[str]:
    row = conn.execute(
        "SELECT value FROM packet_answer WHERE packet_id = ? AND field_key = ?",
        (packet_id, field_key),
    ).fetchone()
    return [x for x in (row[0] or "").splitlines() if x.strip()] if row else []


def employer_notes(conn: sqlite3.Connection, packet_id: int) -> list[str]:
    return get_lines(conn, packet_id, NOTES_KEY)


def story_facts(conn: sqlite3.Connection, packet_id: int, question: str) -> list[str]:
    return get_lines(conn, packet_id, story_key(question))
