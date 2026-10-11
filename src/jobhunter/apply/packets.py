"""Application packets (specs/017 "Phase 1: packets", phase 1a).

A packet hangs off an ``application``, never a ``job_group``, so the nightly group merges only
have to re-point it (``dedupe_url._merge_packets``). One live packet per application; abandoned
ones are kept as history.

**Prepare** opens (or reuses) the live packet for a job group: it shortlists the group, puts its
application at ``preparing`` (creating one if needed, with an append-only event) and returns the
packet id. It never fetches, scores or spends anything.

**Closing the loop** (phase 1d): :func:`attach_sent_packet` records which packet was sent, on
every path that writes an ``applied`` event (``tracking.add_event`` and the "Did you apply?"
*Yes* in ``console/detail.py::answer_prompt``), inside that event's transaction.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jobhunter.console.tracking import (
    PREPARE_NOTE,
    TrackingError,
    rebuild_status,
    validate_attachment_path,
)
from jobhunter.core import db
from jobhunter.pipeline.dedupe import normalize_employer
from jobhunter.pipeline.dedupe_xstate import normalize_title

logger = logging.getLogger(__name__)

PACKET_REF_PREFIX = "packet:"
# Statuses that mean the application went out ("sent" is not stored: specs/017 "Data model").
SENT_STATUSES = ("applied", "acknowledged", "screening", "interview", "offer")

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
            "INSERT INTO application_event (application_id, at, status, note, source) "
            "VALUES (?, ?, 'preparing', ?, 'manual')",
            (app_id, at, PREPARE_NOTE),
        )
        if app is not None:
            # The cache comes from the log, the same way every reader derives it.
            rebuild_status(conn, app_id)
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
    score_on_request: bool = False

    @property
    def pasted(self) -> bool:
        """Scored only on request (specs/017 "Scored on request: a group flag")."""
        return self.score_on_request


def get_packet(conn: sqlite3.Connection, packet_id: int) -> Packet | None:
    row = conn.execute(
        "SELECT p.*, a.status AS app_status, a.job_group_id AS group_id, j.title, j.employer, "
        "j.agency_raw, j.source_key, j.url, j.apply_url, g.score_on_request, "
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
        score_on_request=bool(row["score_on_request"]),
    )


# ─── closing the loop (phase 1d) ────────────────────────────────────────────


def packet_ref(packet_id: int, version: int) -> str:
    """``application.resume_version`` for a sent packet: ``packet:<id>/resume/v<n>``."""
    return f"{PACKET_REF_PREFIX}{packet_id}/resume/v{version}"


def attach_sent_packet(
    conn: sqlite3.Connection, app_id: int, data_dir: Path | str | None = None
) -> bool:
    """Record the application's ``ready`` packet as what was sent. True when one was attached.

    Called inside the transaction that writes an ``applied`` event, on every path. Sets
    ``application.resume_version`` to the packet's resume version (unless you typed your own),
    ``cover_letter_path`` to the rendered letter (when empty), and adds ``attachment`` rows for
    the rendered files, which must pass ``tracking.validate_attachment_path`` (outside the
    tracked tree). ``rendered_path`` is relative to the data dir (phase 1c writes
    ``packets/<id>/resume-v<n>.pdf``, the versioned copy, which later exports never replace),
    so ``data_dir`` resolves it; without one a relative path is skipped. Idempotent: an
    application already carrying a packet ref is left as it is, so a later version of the
    packet can never replace what was sent. Commits nothing.
    """
    app = conn.execute(
        "SELECT resume_version, cover_letter_path FROM application WHERE id = ?", (app_id,)
    ).fetchone()
    if app is None or (app["resume_version"] or "").startswith(PACKET_REF_PREFIX):
        return False
    pk = conn.execute(
        "SELECT id, resume_doc_id, cover_doc_id FROM application_packet "
        "WHERE application_id = ? AND status = 'ready'",
        (app_id,),
    ).fetchone()
    if pk is None or pk["resume_doc_id"] is None:
        return False
    docs = {
        kind: conn.execute(
            "SELECT version, rendered_path FROM packet_document WHERE id = ?", (doc_id,)
        ).fetchone()
        for kind, doc_id in (("resume", pk["resume_doc_id"]), ("cover_letter", pk["cover_doc_id"]))
        if doc_id is not None
    }
    at = _iso(datetime.now(UTC))
    if not (app["resume_version"] or "").strip():
        conn.execute(
            "UPDATE application SET resume_version = ?, updated_at = ? WHERE id = ?",
            (packet_ref(pk["id"], docs["resume"]["version"]), at, app_id),
        )
    for kind, doc in docs.items():
        if doc is None or not doc["rendered_path"]:
            continue  # not exported yet (phase 1c renders the files)
        _attach_file(conn, app_id, pk["id"], kind, doc["rendered_path"], data_dir, at)
    return True


def _attach_file(
    conn: sqlite3.Connection,
    app_id: int,
    packet_id: int,
    kind: str,
    rendered_path: str,
    data_dir: Path | str | None,
    at: str,
) -> bool:
    """One ``attachment`` row for a rendered packet file (and ``cover_letter_path`` when
    empty). False when it is skipped (no data dir, or the path fails validation)."""
    stored = Path(rendered_path)
    if not stored.is_absolute():
        if data_dir is None:
            logger.warning("packet %s: %s not attached: no data dir given", packet_id, kind)
            return False
        stored = Path(data_dir).expanduser() / stored
    try:
        path = str(validate_attachment_path(str(stored)))
    except TrackingError as exc:
        logger.warning("packet %s: %s not attached: %s", packet_id, kind, exc)
        return False
    dup = conn.execute(
        "SELECT 1 FROM attachment WHERE application_id = ? AND path = ?", (app_id, path)
    ).fetchone()
    if dup is None:
        conn.execute(
            "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (?, ?, ?, ?)",
            (app_id, kind, path, at),
        )
    if kind == "cover_letter":
        conn.execute(
            "UPDATE application SET cover_letter_path = ? WHERE id = ? "
            "AND trim(coalesce(cover_letter_path, '')) = ''",
            (path, app_id),
        )
    return True


def attach_late_export(conn: sqlite3.Connection, packet_id: int, data_dir: Path | str) -> int:
    """Attach files exported **after** the application was marked applied with this packet
    (950d5eb (6)): ``attach_sent_packet`` ran then, and found nothing rendered.

    Only what was sent: the resume version the application's ``packet:`` ref names, and the
    current letter while the packet is still ``ready`` with that same resume (a new version
    puts the packet back to draft). Returns the number of files attached. Commits nothing.
    """
    pk = conn.execute(
        "SELECT p.id, p.status, p.application_id, p.resume_doc_id, p.cover_doc_id, "
        "a.resume_version FROM application_packet p JOIN application a "
        "ON a.id = p.application_id WHERE p.id = ?",
        (packet_id,),
    ).fetchone()
    if pk is None:
        return 0
    ref = pk["resume_version"] or ""
    prefix = f"{PACKET_REF_PREFIX}{packet_id}/resume/v"
    if not ref.startswith(prefix) or not ref[len(prefix) :].isdigit():
        return 0
    sent = int(ref[len(prefix) :])
    at = _iso(datetime.now(UTC))
    n = 0
    resume = conn.execute(
        "SELECT rendered_path FROM packet_document WHERE packet_id = ? AND kind = 'resume' "
        "AND version = ?",
        (packet_id, sent),
    ).fetchone()
    if resume is not None and resume["rendered_path"]:
        n += _attach_file(
            conn, pk["application_id"], packet_id, "resume", resume["rendered_path"], data_dir, at
        )
    current = conn.execute(
        "SELECT version FROM packet_document WHERE id = ?", (pk["resume_doc_id"],)
    ).fetchone()
    if pk["status"] == "ready" and current is not None and current["version"] == sent:
        letter = conn.execute(
            "SELECT rendered_path FROM packet_document WHERE id = ?", (pk["cover_doc_id"],)
        ).fetchone()
        if letter is not None and letter["rendered_path"]:
            n += _attach_file(
                conn,
                pk["application_id"],
                packet_id,
                "cover_letter",
                letter["rendered_path"],
                data_dir,
                at,
            )
    return n


@dataclass(frozen=True)
class AppliedBefore:
    app_id: int
    group_id: int
    title: str
    employer: str
    status: str
    applied_at: str | None


def already_applied(conn: sqlite3.Connection, p: Packet) -> list[AppliedBefore]:
    """Other applications to the same employer and title that went out (``applied`` or
    later, or with an applied date): the warning shown before Generate. Read only."""
    emp, tit = normalize_employer(p.employer), normalize_title(p.title)
    if not (emp and tit):
        return []
    out: list[AppliedBefore] = []
    rows = conn.execute(
        "SELECT a.id, a.job_group_id, a.status, a.applied_at, j.title, j.employer, j.agency_raw "
        "FROM application a JOIN job_group g ON g.id = a.job_group_id "
        "JOIN job j ON j.id = g.canonical_job_id WHERE a.id != ? "
        f"AND (a.applied_at IS NOT NULL OR a.status IN ({','.join('?' * len(SENT_STATUSES))}))",
        (p.application_id, *SENT_STATUSES),
    ).fetchall()
    for r in rows:
        employer = r["employer"] or r["agency_raw"] or ""
        if normalize_title(r["title"]) == tit and normalize_employer(employer) == emp:
            out.append(
                AppliedBefore(
                    r["id"], r["job_group_id"], r["title"], employer, r["status"], r["applied_at"]
                )
            )
    return out
