"""Mail proposals (specs/007 "Optional: Gmail matching"): list, accept, dismiss.

Proposals come from ``jobhunter mail match``. Accepting is the only way one changes an
application, and it goes through ``tracking.add_event`` with source 'email'. Dismissing records
the decision and changes nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from jobhunter.console import tracking as t

MANUAL_SOURCE = "email-manual"


@dataclass
class ProposalView:
    id: int
    kind: str
    action: str
    status: str
    confidence: float
    received_at: str
    sender: str
    subject: str
    snippet: str
    matched: list[str]
    app_id: int | None
    group_id: int | None
    job_title: str | None
    job_employer: str | None
    parsed_employer: str | None
    parsed_title: str | None


def _view(r: sqlite3.Row) -> ProposalView:
    ev = json.loads(r["evidence"])
    job = ev.get("job") or {}
    parsed = ev.get("parsed") or {}
    return ProposalView(
        id=r["id"],
        kind=r["kind"],
        action=r["proposed_action"],
        status=r["proposed_status"],
        confidence=r["confidence"],
        received_at=r["received_at"],
        sender=ev.get("sender", ""),
        subject=ev.get("subject", ""),
        snippet=ev.get("snippet", ""),
        matched=ev.get("matched", []),
        app_id=r["application_id"],
        group_id=r["job_group_id"],
        job_title=job.get("title"),
        job_employer=job.get("employer"),
        parsed_employer=parsed.get("employer"),
        parsed_title=parsed.get("title"),
    )


def pending(conn: sqlite3.Connection, limit: int | None = None) -> list[ProposalView]:
    sql = "SELECT * FROM mail_proposal WHERE state = 'pending' ORDER BY received_at DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [_view(r) for r in conn.execute(sql).fetchall()]


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM mail_proposal WHERE state = 'pending'").fetchone()[0]


def _get_pending(conn: sqlite3.Connection, pid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM mail_proposal WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise t.TrackingError(f"no such proposal {pid}")
    if row["state"] != "pending":
        raise t.TrackingError(f"proposal {pid} is already {row['state']}")
    return row


def _decide(conn: sqlite3.Connection, pid: int, state: str, now: datetime) -> None:
    conn.execute(
        "UPDATE mail_proposal SET state = ?, decided_at = ? WHERE id = ?",
        (state, now.astimezone(UTC).isoformat(), pid),
    )
    conn.commit()


def dismiss(conn: sqlite3.Connection, pid: int, now: datetime) -> None:
    row = _get_pending(conn, pid)
    if row["kind"] == "rejection":
        # The user says this email did not reject them: drop the rejection row read from it,
        # so it stops hiding the posting (bug d28c8de). Manual rows are never touched.
        conn.execute(
            "DELETE FROM rejection WHERE gmail_message_id = ? AND source = 'email'",
            (row["gmail_message_id"],),
        )
    _decide(conn, pid, "dismissed", now)


def _manual_group(
    conn: sqlite3.Connection, message_id: str, employer: str, title: str, at: str
) -> int:
    """A minimal job + group for an application with no known posting (applied elsewhere)."""
    conn.execute(
        "INSERT OR IGNORE INTO source (key, class, name, family, tier, entry, policy, status) "
        "VALUES (?, 'C', 'Added from email', 'manual', 'manual', 'gmail', 'manual', 'manual')",
        (MANUAL_SOURCE,),
    )
    cur = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, first_seen_at, "
        "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (MANUAL_SOURCE, message_id, f"gmail:{message_id}", title, employer, at, at),
    )
    job_id = cur.lastrowid
    g = conn.execute(
        "INSERT INTO job_group (canonical_job_id, method, created_at) VALUES (?, 'manual', ?)",
        (job_id, at),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (g.lastrowid, job_id))
    return int(g.lastrowid or 0)


def accept(
    conn: sqlite3.Connection,
    pid: int,
    now: datetime,
    employer: str | None = None,
    title: str | None = None,
) -> int:
    """Apply a pending proposal; returns the application id. ``employer``/``title`` only
    matter for a create with no matched job."""
    row = _get_pending(conn, pid)
    ev = json.loads(row["evidence"])
    note = f"from email: {ev.get('subject', '')}"[:200]
    at = row["received_at"]
    now_s = now.astimezone(UTC).isoformat()
    if row["proposed_action"] == "add_event":
        app_id = row["application_id"]
        t.add_event(conn, app_id, row["proposed_status"], note, at, source="email")
        if row["kind"] == "rejection":  # accepting it confirms the rejection row too
            conn.execute(
                "UPDATE rejection SET state = 'confirmed' WHERE gmail_message_id = ?",
                (row["gmail_message_id"],),
            )
    else:
        gid = row["job_group_id"]
        if gid is None:
            parsed = ev.get("parsed") or {}
            gid = _manual_group(
                conn,
                row["gmail_message_id"],
                (employer or parsed.get("employer") or "Unknown employer").strip(),
                (title or parsed.get("title") or "Unknown role").strip(),
                now_s,
            )
        existing = conn.execute(
            "SELECT id FROM application WHERE job_group_id = ?", (gid,)
        ).fetchone()
        if existing:
            app_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO application (job_group_id, status, applied_at, created_at, "
                "updated_at) VALUES (?, 'applied', ?, ?, ?)",
                (gid, at, now_s, now_s),
            )
            app_id = int(cur.lastrowid or 0)
        conn.commit()
        t.add_event(conn, app_id, "applied", note, at, source="email")
    _decide(conn, pid, "accepted", now)
    return int(app_id)
