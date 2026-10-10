"""Application tracking (specs/007 "Application tracking details").

Events are the truth: ``application_event`` is append-only and ``application.status`` is a
rebuildable cache of the latest event. This module owns the data layer for /pipeline and
/followups: status derivation, staleness, kanban grouping, follow-up nudges, contacts and
attachments. Attachments are local file paths only; files are never read, copied or uploaded.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunter.pipeline.locations import load_job_group_locations, location_summary

BOARD_STATUSES = (
    "interested",
    "preparing",
    "applied",
    "acknowledged",
    "screening",
    "interview",
    "offer",
)
CLOSED_STATUSES = ("rejected", "withdrawn", "no_response", "closed")
ALL_STATUSES = BOARD_STATUSES + CLOSED_STATUSES
ATTACHMENT_KINDS = ("resume", "cover_letter", "transcript", "assessment", "other")

# Days in a status after which a card goes amber. Config-free on purpose: these are product
# defaults from specs/007 ("applied with no acknowledgement for 21 days is the state that
# actually needs your attention"); the other thresholds shrink as the process gets closer to an
# offer. Statuses not listed (interested, preparing, closed lane) are never stale.
STALE_DAYS = {"applied": 21, "acknowledged": 14, "screening": 10, "interview": 7, "offer": 5}
NUDGE_DAYS = STALE_DAYS["applied"]
SNOOZE_DAYS = 3


class TrackingError(ValueError):
    """Bad input to a tracking operation (maps to HTTP 422 in the routes)."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def parse_ts(value: str) -> datetime:
    """Parse an ISO timestamp or date; naive values are taken as UTC."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def application_exists(conn: sqlite3.Connection, app_id: int) -> bool:
    return conn.execute("SELECT 1 FROM application WHERE id = ?", (app_id,)).fetchone() is not None


# --- events and derived status -------------------------------------------------------------


def rebuild_status(conn: sqlite3.Connection, application_id: int) -> str:
    """Set application.status (and applied_at) from the event log; returns the status."""
    row = conn.execute(
        "SELECT status FROM application_event WHERE application_id = ? "
        "ORDER BY at DESC, id DESC LIMIT 1",
        (application_id,),
    ).fetchone()
    if row is None:
        raise TrackingError(f"application {application_id} has no events")
    applied = conn.execute(
        "SELECT MIN(at) FROM application_event WHERE application_id = ? AND status = 'applied'",
        (application_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE application SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
        (row["status"], applied, _iso(datetime.now(UTC)), application_id),
    )
    return row["status"]


def add_event(
    conn: sqlite3.Connection,
    app_id: int,
    status: str,
    note: str | None = None,
    at: datetime | str | None = None,
    source: str = "manual",
) -> int:
    """Append an event and rebuild the status. Past events are never touched."""
    if status not in ALL_STATUSES:
        raise TrackingError(f"unknown status {status!r}")
    if not application_exists(conn, app_id):
        raise TrackingError(f"no such application {app_id}")
    if at is None:
        at = datetime.now(UTC)
    try:
        at_s = _iso(at) if isinstance(at, datetime) else _iso(parse_ts(at))
    except ValueError as exc:
        raise TrackingError(f"bad date {at!r}") from exc
    cur = conn.execute(
        "INSERT INTO application_event (application_id, at, status, note, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (app_id, at_s, status, (note or "").strip() or None, source),
    )
    rebuild_status(conn, app_id)
    conn.commit()
    return int(cur.lastrowid or 0)


def events(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    """Full history, newest first."""
    return conn.execute(
        "SELECT * FROM application_event WHERE application_id = ? ORDER BY at DESC, id DESC",
        (app_id,),
    ).fetchall()


def status_since(conn: sqlite3.Connection, app_id: int) -> datetime | None:
    """When the current status began: the first event of the latest run of the same status."""
    since: str | None = None
    current: str | None = None
    for ev in events(conn, app_id):
        if current is None:
            current = ev["status"]
        if ev["status"] != current:
            break
        since = ev["at"]
    return parse_ts(since) if since else None


def days_in_status(conn: sqlite3.Connection, app_id: int, now: datetime) -> int:
    since = status_since(conn, app_id)
    return max(0, (now - since).days) if since else 0


def is_stale(status: str, days: int) -> bool:
    limit = STALE_DAYS.get(status)
    return limit is not None and days >= limit


# --- pipeline board ------------------------------------------------------------------------


@dataclass
class Card:
    app_id: int
    group_id: int
    title: str
    employer: str
    location: str
    status: str
    days: int
    stale: bool
    resume_version: str | None
    next_action: str | None
    next_action_at: str | None
    # Why: two postings with the same title and employer looked like one job listed twice.
    source: str = ""
    posted: str | None = None


@dataclass
class Pipeline:
    columns: dict[str, list[Card]] = field(default_factory=dict)
    closed: list[Card] = field(default_factory=list)


def card(conn: sqlite3.Connection, app_id: int, now: datetime) -> Card | None:
    row = conn.execute(
        "SELECT a.*, j.id AS job_id, j.title, j.employer, j.agency_raw, j.location_scope, "
        "j.posted_at, s.name AS source_name "
        "FROM application a JOIN job_group g ON g.id = a.job_group_id "
        "JOIN job j ON j.id = g.canonical_job_id LEFT JOIN source s ON s.key = j.source_key "
        "WHERE a.id = ?",
        (app_id,),
    ).fetchone()
    if row is None:
        return None
    days = days_in_status(conn, app_id, now)
    return Card(
        app_id=row["id"],
        group_id=row["job_group_id"],
        title=row["title"],
        employer=row["employer"] or row["agency_raw"] or "employer not stated",
        location=location_summary(
            load_job_group_locations(conn, row["job_id"]), row["location_scope"]
        ),
        status=row["status"],
        days=days,
        stale=is_stale(row["status"], days),
        resume_version=row["resume_version"],
        next_action=row["next_action"],
        next_action_at=row["next_action_at"],
        source=row["source_name"] or "",
        posted=(row["posted_at"] or "")[:10] or None,
    )


def pipeline(conn: sqlite3.Connection, now: datetime) -> Pipeline:
    """Cards grouped by status; closed-lane statuses collapse into ``closed``."""
    out = Pipeline(columns={s: [] for s in BOARD_STATUSES})
    for r in conn.execute("SELECT id FROM application ORDER BY created_at, id").fetchall():
        c = card(conn, r["id"], now)
        if c is None:
            continue
        if c.status in out.columns:
            out.columns[c.status].append(c)
        else:
            out.closed.append(c)
    return out


def lane_of(status: str) -> str:
    return status if status in BOARD_STATUSES else "closed"


# --- follow-ups ----------------------------------------------------------------------------


@dataclass
class FollowUp:
    kind: str  # 'action' | 'nudge'
    app_id: int
    title: str
    employer: str
    text: str
    due: datetime
    overdue_days: int


def followups(conn: sqlite3.Connection, now: datetime) -> list[FollowUp]:
    """Due/overdue next actions plus 21-day 'applied' nudges, oldest first."""
    items: list[FollowUp] = []
    for r in conn.execute(
        "SELECT id, status, next_action, next_action_at FROM application "
        "WHERE next_action_at IS NOT NULL AND next_action_at != ''"
    ).fetchall():
        if r["status"] in CLOSED_STATUSES:
            continue
        raw = r["next_action_at"].strip()
        due = parse_ts(raw)
        # Date-only values are due for the whole of that day.
        not_yet = due.date() > now.date() if len(raw) <= 10 else due > now
        if not_yet:
            continue
        items.append(_item("action", r["id"], r["next_action"] or "follow up", due, now, conn))
    for r in conn.execute("SELECT id FROM application WHERE status = 'applied'").fetchall():
        since = status_since(conn, r["id"])
        if since is None or now - since < timedelta(days=NUDGE_DAYS):
            continue
        text = f"applied {(now - since).days} days ago, no reply: follow up or mark no_response"
        items.append(_item("nudge", r["id"], text, since + timedelta(days=NUDGE_DAYS), now, conn))
    items.sort(key=lambda i: (i.due, i.app_id))
    return items


def _item(
    kind: str, app_id: int, text: str, due: datetime, now: datetime, conn: sqlite3.Connection
) -> FollowUp:
    c = card(conn, app_id, now)
    assert c is not None
    return FollowUp(
        kind=kind,
        app_id=app_id,
        title=c.title,
        employer=c.employer,
        text=text,
        due=due,
        overdue_days=max(0, (now.date() - due.date()).days),
    )


def set_next_action(
    conn: sqlite3.Connection, app_id: int, action: str | None, at: str | None
) -> None:
    """Set or clear the next action. ``at`` is a date or timestamp (validated)."""
    at = (at or "").strip() or None
    if at:
        try:
            parse_ts(at)
        except ValueError as exc:
            raise TrackingError(f"bad date {at!r}") from exc
    action = (action or "").strip() or None
    cur = conn.execute(
        "UPDATE application SET next_action = ?, next_action_at = ?, updated_at = ? WHERE id = ?",
        (action, at, _iso(datetime.now(UTC)), app_id),
    )
    if cur.rowcount == 0:
        raise TrackingError(f"no such application {app_id}")
    conn.commit()


def done(conn: sqlite3.Connection, app_id: int) -> None:
    set_next_action(conn, app_id, None, None)


def snooze(conn: sqlite3.Connection, app_id: int, now: datetime, days: int = SNOOZE_DAYS) -> None:
    row = conn.execute("SELECT next_action FROM application WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise TrackingError(f"no such application {app_id}")
    when = (now + timedelta(days=days)).date().isoformat()
    set_next_action(conn, app_id, row["next_action"], when)


def update_details(
    conn: sqlite3.Connection, app_id: int, resume_version: str | None, external_ref: str | None
) -> None:
    cur = conn.execute(
        "UPDATE application SET resume_version = ?, external_ref = ?, updated_at = ? WHERE id = ?",
        (
            (resume_version or "").strip() or None,
            (external_ref or "").strip() or None,
            _iso(datetime.now(UTC)),
            app_id,
        ),
    )
    if cur.rowcount == 0:
        raise TrackingError(f"no such application {app_id}")
    conn.commit()


# --- contacts ------------------------------------------------------------------------------

CONTACT_FIELDS = ("name", "role", "email", "phone", "note")


def contacts(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contact WHERE application_id = ? ORDER BY id", (app_id,)
    ).fetchall()


def _clean(fields: dict[str, str | None]) -> list[str | None]:
    return [(fields.get(k) or "").strip() or None for k in CONTACT_FIELDS]


def add_contact(conn: sqlite3.Connection, app_id: int, **fields: str | None) -> int:
    vals = _clean(fields)
    if not any(vals):
        raise TrackingError("a contact needs at least one field")
    if not application_exists(conn, app_id):
        raise TrackingError(f"no such application {app_id}")
    cur = conn.execute(
        "INSERT INTO contact (application_id, name, role, email, phone, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (app_id, *vals),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_contact(conn: sqlite3.Connection, contact_id: int, **fields: str | None) -> None:
    cur = conn.execute(
        "UPDATE contact SET name = ?, role = ?, email = ?, phone = ?, note = ? WHERE id = ?",
        (*_clean(fields), contact_id),
    )
    if cur.rowcount == 0:
        raise TrackingError(f"no such contact {contact_id}")
    conn.commit()


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> None:
    conn.execute("DELETE FROM contact WHERE id = ?", (contact_id,))
    conn.commit()


# --- attachments ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git_tracked(path: Path, repo_root: Path) -> bool | None:
    """True/False from git, None if git cannot answer."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode == 0:
        return True
    return False if res.returncode == 1 else None


def validate_attachment_path(raw: str, repo_root: Path = REPO_ROOT) -> Path:
    """Resolve a local path; it must exist, be a file, and not be tracked by the repo.

    Files inside the repo directory are allowed only when git says they are untracked
    (``data/``, ``resume/`` are gitignored); if git cannot answer, they are refused.
    """
    raw = (raw or "").strip()
    if not raw:
        raise TrackingError("path is required")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise TrackingError(f"no such file: {path}")
    root = repo_root.resolve()
    if path.is_relative_to(root) and _git_tracked(path, root) is not False:
        raise TrackingError(f"refusing a path inside the repository's tracked tree: {path}")
    return path


def attachments(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachment WHERE application_id = ? ORDER BY id", (app_id,)
    ).fetchall()


def add_attachment(
    conn: sqlite3.Connection,
    app_id: int,
    kind: str,
    path: str,
    now: datetime | None = None,
    repo_root: Path = REPO_ROOT,
) -> int:
    """Record a local path (never uploaded or copied)."""
    if kind not in ATTACHMENT_KINDS:
        raise TrackingError(f"unknown attachment kind {kind!r}")
    if not application_exists(conn, app_id):
        raise TrackingError(f"no such application {app_id}")
    resolved = validate_attachment_path(path, repo_root)
    cur = conn.execute(
        "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (?, ?, ?, ?)",
        (app_id, kind, str(resolved), _iso(now or datetime.now(UTC))),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> None:
    """Removes the record only; the file on disk is untouched."""
    conn.execute("DELETE FROM attachment WHERE id = ?", (attachment_id,))
    conn.commit()
