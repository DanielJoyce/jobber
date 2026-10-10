"""Reviewing packet documents: versions, confirmations, edits, Mark ready (specs/017).

**Mark ready is gated on the current version's ``check_report`` alone**: the packet becomes
``ready`` only when the current resume version (and the current letter, if there is one) has
every item passing or confirmed. Editing never clears a badge: the checker re-runs on the
edited text and a confirmation carries forward only while an item's text is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jobhunter.apply import editor, factcheck
from jobhunter.apply.generator import current_doc_id, write_version
from jobhunter.core import db


class ReviewError(ValueError):
    """Refused; ``str()`` is the message for the page."""


@dataclass
class Version:
    id: int
    packet_id: int
    kind: str
    version: int
    question_key: str
    origin: str
    runner: str | None
    model: str | None
    cost_usd: float | None
    api_equiv_usd: float | None
    created_at: str
    doc: dict[str, Any]
    report: dict[str, Any]
    body_md: str

    @property
    def lines(self) -> dict[str, str]:
        return (self.doc.get("context") or {}).get("lines") or {}

    @property
    def ok(self) -> bool:
        return bool(self.report.get("ok"))

    @property
    def unsupported(self) -> int:
        return len(factcheck.unsupported(self.report))


def _version(row: sqlite3.Row) -> Version:
    return Version(
        id=row["id"],
        packet_id=row["packet_id"],
        kind=row["kind"],
        version=row["version"],
        question_key=row["question_key"],
        origin=row["origin"],
        runner=row["runner"],
        model=row["model"],
        cost_usd=row["cost_usd"],
        api_equiv_usd=row["api_equiv_usd"],
        created_at=row["created_at"],
        doc=json.loads(row["doc_json"]),
        report=json.loads(row["check_report"]),
        body_md=row["body_md"],
    )


def get_version(conn: sqlite3.Connection, doc_id: int) -> Version | None:
    row = conn.execute("SELECT * FROM packet_document WHERE id = ?", (doc_id,)).fetchone()
    return _version(row) if row else None


def versions(
    conn: sqlite3.Connection, packet_id: int, kind: str, question_key: str = ""
) -> list[Version]:
    rows = conn.execute(
        "SELECT * FROM packet_document WHERE packet_id = ? AND kind = ? AND question_key = ? "
        "ORDER BY version DESC",
        (packet_id, kind, question_key),
    ).fetchall()
    return [_version(r) for r in rows]


def question_drafts(conn: sqlite3.Connection, packet_id: int) -> list[Version]:
    """The latest version of each question draft on the packet, newest first."""
    rows = conn.execute(
        "SELECT d.* FROM packet_document d WHERE d.packet_id = ? AND d.kind = 'question_draft' "
        "AND d.version = (SELECT max(x.version) FROM packet_document x WHERE x.packet_id = "
        "d.packet_id AND x.kind = d.kind AND x.question_key = d.question_key) ORDER BY d.id DESC",
        (packet_id,),
    ).fetchall()
    return [_version(r) for r in rows]


def posting_of(conn: sqlite3.Connection, packet_id: int) -> tuple[str, str, str]:
    """(posting text, employer, title) of the packet's group, as the checker reads them."""
    row = conn.execute(
        "SELECT coalesce(j.description_text, '') AS text, "
        "coalesce(j.employer, j.agency_raw, '') AS employer, coalesce(j.title, '') AS title "
        "FROM application_packet p JOIN application a ON a.id = p.application_id "
        "JOIN job_group g ON g.id = a.job_group_id JOIN job j ON j.id = g.canonical_job_id "
        "WHERE p.id = ?",
        (packet_id,),
    ).fetchone()
    if row is None:
        raise ReviewError("no such packet")
    return row["text"], row["employer"], row["title"]


def _require_current(conn: sqlite3.Connection, packet_id: int, doc_id: int) -> Version:
    v = get_version(conn, doc_id)
    if v is None or v.packet_id != packet_id:
        raise ReviewError("no such document on this packet")
    if current_doc_id(conn, packet_id, v.kind, v.question_key) != doc_id:
        raise ReviewError("a newer version exists; reload the page and edit that one")
    return v


def confirm(
    conn: sqlite3.Connection, packet_id: int, doc_id: int, ckey: str, now: datetime
) -> None:
    """**This is true, keep it** on one unsupported item of the current version. Recorded in
    its ``check_report``; the text is not touched, so no new version is written."""
    with db.transaction(conn):
        v = _require_current(conn, packet_id, doc_id)
        items = v.report.get("items") or []
        if not any(i.get("ckey") == ckey and i.get("reasons") for i in items):
            raise ReviewError("nothing to confirm there")
        confirmed = list(v.report.get("confirmed") or [])
        if ckey not in confirmed:
            confirmed.append(ckey)
        for i in items:
            if i.get("reasons") and i.get("ckey") in confirmed:
                i["status"] = "confirmed"
        v.report["confirmed"] = confirmed
        v.report["ok"] = bool(items) and all(i["status"] != "unsupported" for i in items)
        conn.execute(
            "UPDATE packet_document SET check_report = ? WHERE id = ?",
            (json.dumps(v.report), doc_id),
        )
        conn.execute(
            "UPDATE application_packet SET updated_at = ? WHERE id = ?",
            (now.astimezone(UTC).isoformat(), packet_id),
        )


def save_edit(
    conn: sqlite3.Connection,
    packet_id: int,
    doc_id: int,
    form: Mapping[str, str],
    now: datetime,
) -> int:
    """**Save**: a new ``edited`` version from the editor form; returns its id."""
    v = _require_current(conn, packet_id, doc_id)
    try:
        doc, ticked = editor.doc_from_form(v.doc, form)
    except editor.EditError as exc:
        raise ReviewError(str(exc)) from exc
    posting, employer, title = posting_of(conn, packet_id)
    new_id, _ver, _rep = write_version(
        conn,
        packet_id,
        doc,
        origin="edited",
        now=now,
        posting=posting,
        employer=employer,
        title=title,
        question_key=v.question_key,
        extra_confirmed=ticked,
    )
    return new_id


def restore(
    conn: sqlite3.Connection, packet_id: int, doc_id: int, line_id: str, now: datetime
) -> int:
    """Restore an omitted resume line in one click (a new ``edited`` version)."""
    v = _require_current(conn, packet_id, doc_id)
    try:
        doc = editor.restore_line(v.doc, line_id.strip().upper())
    except editor.EditError as exc:
        raise ReviewError(str(exc)) from exc
    posting, employer, title = posting_of(conn, packet_id)
    new_id, _ver, _rep = write_version(
        conn,
        packet_id,
        doc,
        origin="edited",
        now=now,
        posting=posting,
        employer=employer,
        title=title,
    )
    return new_id


def ready_blockers(conn: sqlite3.Connection, packet_id: int) -> list[str]:
    """Why the packet cannot be marked ready (empty: it can)."""
    row = conn.execute(
        "SELECT status, resume_doc_id, cover_doc_id FROM application_packet WHERE id = ?",
        (packet_id,),
    ).fetchone()
    if row is None:
        return ["no such packet"]
    out: list[str] = []
    if row["status"] == "abandoned":
        out.append("this packet was abandoned")
    if row["resume_doc_id"] is None:
        out.append("there is no resume version yet (generate one or use the base resume)")
    for col, name in (("resume_doc_id", "resume"), ("cover_doc_id", "cover letter")):
        if row[col] is None:
            continue
        v = get_version(conn, row[col])
        if v is not None and not v.ok:
            n = v.unsupported
            out.append(
                f"the {name} (v{v.version}) has {n} unsupported line{'' if n == 1 else 's'}: "
                "fix or confirm each"
                if n
                else f"the {name} (v{v.version}) has nothing checked yet"
            )
    return out


def mark_ready(conn: sqlite3.Connection, packet_id: int, now: datetime) -> None:
    with db.transaction(conn):
        blockers = ready_blockers(conn, packet_id)
        if blockers:
            raise ReviewError("Not ready: " + "; ".join(blockers))
        at = now.astimezone(UTC).isoformat()
        conn.execute(
            "UPDATE application_packet SET status = 'ready', ready_at = ?, updated_at = ? "
            "WHERE id = ?",
            (at, at, packet_id),
        )
