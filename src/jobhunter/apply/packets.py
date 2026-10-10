"""Application packets (specs/017 "Phase 1: packets", phase 1a).

A packet hangs off an ``application``, never a ``job_group``, so the nightly group merges only
have to re-point it (``dedupe_url._merge_packets``). One live packet per application; abandoned
ones are kept as history.

**Prepare** opens (or reuses) the live packet for a job group: it shortlists the group, puts its
application at ``preparing`` (creating one if needed, with an append-only event) and returns the
packet id. It never fetches, scores or spends anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from jobhunter.console.tracking import PREPARE_NOTE
from jobhunter.core import db
from jobhunter.core.manual_sources import PASTE_MANUAL

# Application statuses Prepare moves to 'preparing'; later ones are left where they are.
_BEFORE_PREPARING = ("interested",)


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def live_packet_id(conn: sqlite3.Connection, group_id: int) -> int | None:
    """The live (not abandoned) packet of the group's application, if any."""
    row = conn.execute(
        "SELECT p.id FROM application_packet p JOIN application a ON a.id = p.application_id "
        "WHERE a.job_group_id = ? AND p.status != 'abandoned'",
        (group_id,),
    ).fetchone()
    return int(row[0]) if row else None


def live_packet_for_application(conn: sqlite3.Connection, app_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM application_packet WHERE application_id = ? AND status != 'abandoned'",
        (app_id,),
    ).fetchone()
    return int(row[0]) if row else None


def prepare_in_txn(conn: sqlite3.Connection, group_id: int, now: datetime) -> int:
    """``prepare`` without its own transaction (the caller holds one)."""
    if conn.execute("SELECT 1 FROM job_group WHERE id = ?", (group_id,)).fetchone() is None:
        raise KeyError(f"no such job group: {group_id}")
    at = _iso(now)
    app = conn.execute(
        "SELECT id, status FROM application WHERE job_group_id = ?", (group_id,)
    ).fetchone()
    if app is None:
        app_id = int(
            conn.execute(
                "INSERT INTO application (job_group_id, status, created_at, updated_at) "
                "VALUES (?, 'preparing', ?, ?)",
                (group_id, at, at),
            ).lastrowid
            or 0
        )
        moved = True
    else:
        app_id = int(app["id"])
        moved = app["status"] in _BEFORE_PREPARING
        if moved:
            conn.execute(
                "UPDATE application SET status = 'preparing', updated_at = ? WHERE id = ?",
                (at, app_id),
            )
    if moved:
        conn.execute(
            "INSERT INTO application_event (application_id, at, status, note, source) "
            "VALUES (?, ?, 'preparing', ?, 'manual')",
            (app_id, at, PREPARE_NOTE),
        )
    # Preparing a packet is a shortlist: the row leaves the inbox like a pressed `s`. A
    # dismissal is overridden (the user chose this job after all); 'applied' is kept. A job
    # already applied to (or further) is not newly shortlisted: no label is written for it.
    shortlisting = app is None or app["status"] in ("interested", "preparing")
    if shortlisting:
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, 'interesting', ?) "
            "ON CONFLICT (job_group_id) DO UPDATE SET label = 'interesting', "
            "labeled_at = excluded.labeled_at WHERE label.label = 'not_interesting'",
            (group_id, at),
        )
    live = live_packet_for_application(conn, app_id)
    if live is not None:
        return live
    cur = conn.execute(
        "INSERT INTO application_packet (application_id, status, created_at, updated_at) "
        "VALUES (?, 'draft', ?, ?)",
        (app_id, at, at),
    )
    return int(cur.lastrowid or 0)


def prepare(conn: sqlite3.Connection, group_id: int, now: datetime) -> int:
    """Open the group's live packet (creating application and packet as needed); its id."""
    with db.transaction(conn):
        return prepare_in_txn(conn, group_id, now)


@dataclass
class Packet:
    id: int
    status: str
    created_at: str
    updated_at: str
    application_id: int
    app_status: str
    group_id: int
    title: str
    employer: str
    source_key: str
    url: str
    apply_url: str | None
    has_description: bool

    @property
    def pasted(self) -> bool:
        return self.source_key == PASTE_MANUAL


def get_packet(conn: sqlite3.Connection, packet_id: int) -> Packet | None:
    row = conn.execute(
        "SELECT p.*, a.status AS app_status, a.job_group_id AS group_id, j.title, j.employer, "
        "j.agency_raw, j.source_key, j.url, j.apply_url, "
        "(coalesce(trim(j.description_text), '') != '') AS has_text "
        "FROM application_packet p JOIN application a ON a.id = p.application_id "
        "JOIN job_group g ON g.id = a.job_group_id JOIN job j ON j.id = g.canonical_job_id "
        "WHERE p.id = ?",
        (packet_id,),
    ).fetchone()
    if row is None:
        return None
    return Packet(
        id=row["id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        application_id=row["application_id"],
        app_status=row["app_status"],
        group_id=row["group_id"],
        title=row["title"],
        employer=row["employer"] or row["agency_raw"] or "employer not stated",
        source_key=row["source_key"],
        url=row["url"],
        apply_url=row["apply_url"],
        has_description=bool(row["has_text"]),
    )
