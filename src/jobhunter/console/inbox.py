"""Inbox data layer: untriaged job groups bucketed by fit (specs/007 "/inbox", 006 buckets).

Buckets, overall and the dimension numbers are recomputed on read from the stored model
dimensions and the *current* profile, so preference edits re-sort the inbox for free.

A posting the employer already rejected you for is left out; a job at an employer that
rejected you for another role recently carries a flag (``core/rejections``).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunter.core import rejections
from jobhunter.core.models import Bucket
from jobhunter.pipeline.locations import jobs_in_state, load_job_group_locations, location_summary
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import Profile

BUCKETS = [b.value for b in Bucket]
BUCKET_TITLES: dict[str, tuple[str, str]] = {
    "A": ("BULLSEYE", "apply, minimal tailoring"),
    "B": ("STRONG", "apply, tailor to the gaps"),
    "C": ("STRETCH UP", "apply if you want the jump"),
    "D": ("LATERAL", "apply selectively"),
    "E": ("DOWNLEVEL", "only if the trade is worth it"),
    "F": ("STALE MATCH", "matched skills you last used years ago"),
    "G": ("MISMATCH", "not your field"),
}
SOURCE_BADGE = {"C": "federal", "A": "state", "B": "state-employer"}


@dataclass
class InboxItem:
    group_id: int
    job_id: int
    bucket: str
    overall: int
    title: str
    employer: str | None
    location: str
    salary: str
    salary_stated: bool
    employment_type: str | None
    remote: str | None
    posted: str | None
    url: str
    source_name: str
    source_badge: str
    recency_skills: int
    raw_skills: int
    seniority: int
    domain: int
    comp: int | None
    location_fit: int
    shape_flags: list[str] = field(default_factory=list)
    evidence_unverified: bool = False
    partial: bool = False
    why: dict[str, str] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    stale_skills: list[str] = field(default_factory=list)
    labeled: str | None = None
    employer_rejection: str | None = None  # "rejected for <title> on <date>" (other role)


@dataclass
class Inbox:
    buckets: dict[str, list[InboxItem]]
    counts: dict[str, int]
    stale_summary: list[tuple[str, int]]
    shown: list[str]
    weekly_cost: float | None = None


def _money(x: float) -> str:
    if x >= 1000:
        return f"${round(x / 1000, 1):g}k"
    return f"${x:g}"


def salary_text(row: Any) -> str:
    """ "$118k-$142k", "$45-$60/hr", or "salary not stated" (never $0 or blank)."""
    lo, hi = row["salary_min"], row["salary_max"]
    if not row["salary_stated"] or (not lo and not hi):
        return "salary not stated"
    a, b = (lo or hi), (hi or lo)
    text = _money(a) if a == b else f"{_money(a)}\u2013{_money(b)}"
    period = row["salary_period"]
    if period and period != "year":
        text += f"/{'hr' if period == 'hour' else period}"
    return text


def posted_age(posted_at: str | None, now: datetime) -> str | None:
    if not posted_at:
        return None
    try:
        when = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    days = (now - when).days
    return "today" if days < 1 else f"{days}d"


def _json(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _strs(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


_SQL = """
SELECT fs.dimensions, fs.blockers, fs.shape_flags, fs.evidence_unverified,
       j.*, j.id AS job_id, g.id AS group_id, l.label AS label_value,
       s.name AS source_name, s.class AS source_class
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
JOIN source s ON s.key = j.source_key
JOIN fit_score fs ON fs.id = (
    SELECT f2.id FROM fit_score f2 WHERE f2.job_group_id = g.id
    ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1)
LEFT JOIN label l ON l.job_group_id = g.id
"""


def _employer_flag(index: rejections.RejectionIndex | None, row: sqlite3.Row) -> str | None:
    if not index:
        return None
    hit = index.prior(row["group_id"], row["employer"] or row["agency_raw"], row["title"])
    if hit is None:
        return None
    return f"employer rejected you for {hit.title or 'another role'} on {hit.day}"


def _item(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    profile: Profile,
    now: datetime,
    index: rejections.RejectionIndex | None = None,
) -> InboxItem:
    locs = load_job_group_locations(conn, row["job_id"])
    fit = compute_row(row, row, locs, profile)
    dims = _json(row["dimensions"], {})
    if not isinstance(dims, dict):
        dims = {}
    why = {
        name: str(dims[name]["why"])
        for name in ("skills", "seniority", "domain")
        if isinstance(dims.get(name), dict) and dims[name].get("why")
    }
    seniority = dims.get("seniority")
    domain = dims.get("domain")
    return InboxItem(
        group_id=row["group_id"],
        job_id=row["job_id"],
        bucket=fit.bucket.value,
        overall=fit.overall,
        title=row["title"],
        employer=row["employer"] or row["agency_raw"],
        location=location_summary(locs, row["location_scope"]),
        salary=salary_text(row),
        salary_stated=bool(row["salary_stated"]),
        employment_type=None if row["employment_type"] == "unknown" else row["employment_type"],
        remote=None if row["remote"] == "unknown" else row["remote"],
        posted=posted_age(row["posted_at"], now),
        url=row["apply_url"] or row["url"],  # the human page when `url` is a JSON endpoint
        source_name=row["source_name"],
        source_badge=SOURCE_BADGE.get(row["source_class"], "state"),
        recency_skills=fit.recency_weighted_skills,
        raw_skills=fit.raw_skills,
        seniority=int(seniority.get("score", 0)) if isinstance(seniority, dict) else 0,
        domain=int(domain.get("score", 0)) if isinstance(domain, dict) else 0,
        comp=fit.comp,
        location_fit=fit.location,
        shape_flags=_strs(_json(row["shape_flags"], [])),
        evidence_unverified=bool(row["evidence_unverified"]),
        partial=row["description_completeness"] == "partial",
        why=why,
        blockers=fit.blockers,
        stale_skills=_strs(dims.get("stale_skills")),
        labeled=row["label_value"],
        employer_rejection=_employer_flag(index, row),
    )


def _state_groups(conn: sqlite3.Connection, profile: Profile, state: str) -> set[int]:
    usps = state.strip().upper()
    allowed = profile.hard.states_allowed
    remote = allowed == "all" or usps in allowed
    rows = jobs_in_state(conn, usps, include_remote=remote)
    return {r["job_group_id"] for r in rows if r["job_group_id"] is not None}


def weekly_cost(conn: sqlite3.Connection, now: datetime) -> float | None:
    since = (now - timedelta(days=7)).date().isoformat()
    row = conn.execute("SELECT SUM(cost_usd) FROM llm_spend WHERE day >= ?", (since,)).fetchone()
    return row[0]


def inbox_items(
    conn: sqlite3.Connection,
    profile: Profile,
    *,
    state: str | None = None,
    bucket: str | None = None,
    include_triaged: bool = False,
    limit: int = 500,
    now: datetime | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> Inbox:
    """Group untriaged job groups by bucket A..G (G only when ``bucket="G"``).

    ``counts`` cover every group matching the state filter; ``limit`` caps rows per bucket.
    Postings an employer already rejected you for are left out unless ``include_triaged``.
    """
    now = now or datetime.now(UTC)
    where = "" if include_triaged else " WHERE l.job_group_id IS NULL"
    rows = conn.execute(_SQL + where).fetchall()
    keep = _state_groups(conn, profile, state) if state else None
    index = rejections.RejectionIndex.load(conn, now, rejection_days)
    rejected = rejections.rejected_group_ids(conn, index) if index and not include_triaged else ()
    buckets: dict[str, list[InboxItem]] = {b: [] for b in BUCKETS}
    for row in rows:
        if keep is not None and row["group_id"] not in keep:
            continue
        if row["group_id"] in rejected:
            continue
        item = _item(conn, row, profile, now, index)
        buckets[item.bucket].append(item)
    for items in buckets.values():
        items.sort(key=lambda i: (-i.overall, i.group_id))
    counts = {b: len(v) for b, v in buckets.items()}
    stale: Counter[str] = Counter()
    for item in buckets["F"]:
        stale.update(set(item.stale_skills))
    shown = [bucket] if bucket in BUCKETS else [b for b in BUCKETS if b != "G"]
    return Inbox(
        buckets={b: buckets[b][:limit] if b in shown else [] for b in BUCKETS},
        counts=counts,
        stale_summary=stale.most_common(5),
        shown=shown,
        weekly_cost=weekly_cost(conn, now),
    )


def inbox_item(conn: sqlite3.Connection, profile: Profile, group_id: int) -> InboxItem | None:
    """One group's row (regardless of label), for re-rendering after undo."""
    row = conn.execute(_SQL + " WHERE g.id = ?", (group_id,)).fetchone()
    if not row:
        return None
    now = datetime.now(UTC)
    return _item(conn, row, profile, now, rejections.RejectionIndex.load(conn, now))


def group_title(conn: sqlite3.Connection, group_id: int) -> str:
    row = conn.execute(
        "SELECT j.title FROM job_group g JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
        (group_id,),
    ).fetchone()
    return row["title"] if row else f"group {group_id}"


def group_exists(conn: sqlite3.Connection, group_id: int) -> bool:
    return conn.execute("SELECT 1 FROM job_group WHERE id = ?", (group_id,)).fetchone() is not None


# ─── label writes ───────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


BULK_MAX = 500
LABELS = ("interesting", "not_interesting")


def _write_label(conn: sqlite3.Connection, group_id: int, label: str, now: str) -> None:
    """Upsert one label (no transaction handling); ``interesting`` opens an application."""
    conn.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, ?, ?) "
        "ON CONFLICT (job_group_id) DO UPDATE SET label = excluded.label, "
        "labeled_at = excluded.labeled_at",
        (group_id, label, now),
    )
    if label == "interesting":
        has_app = conn.execute(
            "SELECT 1 FROM application WHERE job_group_id = ?", (group_id,)
        ).fetchone()
        if not has_app:
            cur = conn.execute(
                "INSERT INTO application (job_group_id, status, created_at, updated_at) "
                "VALUES (?, 'interested', ?, ?)",
                (group_id, now, now),
            )
            conn.execute(
                "INSERT INTO application_event (application_id, at, status, note, source) "
                "VALUES (?, ?, 'interested', 'shortlisted from inbox', 'manual')",
                (cur.lastrowid, now),
            )


def _remove_label(conn: sqlite3.Connection, group_id: int) -> None:
    """Delete the label, and the application if it never progressed past 'interested'."""
    conn.execute("DELETE FROM label WHERE job_group_id = ?", (group_id,))
    app = conn.execute(
        "SELECT id FROM application WHERE job_group_id = ? AND status = 'interested'",
        (group_id,),
    ).fetchone()
    if app:
        events = conn.execute(
            "SELECT COUNT(*) FROM application_event WHERE application_id = ?", (app["id"],)
        ).fetchone()[0]
        others = conn.execute(
            "SELECT (SELECT COUNT(*) FROM contact WHERE application_id = :a) + "
            "(SELECT COUNT(*) FROM attachment WHERE application_id = :a)",
            {"a": app["id"]},
        ).fetchone()[0]
        if events <= 1 and not others:
            conn.execute("DELETE FROM application_event WHERE application_id = ?", (app["id"],))
            conn.execute("DELETE FROM application WHERE id = ?", (app["id"],))


def _in_transaction(conn: sqlite3.Connection, work: Callable[[], None]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        work()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def set_label(conn: sqlite3.Connection, group_id: int, label: str) -> None:
    """Upsert the label; ``interesting`` also opens an application at 'interested'."""
    set_labels(conn, [group_id], label)


def set_labels(conn: sqlite3.Connection, group_ids: Sequence[int], label: str) -> None:
    """Label many groups in one transaction: all of them, or (on any error) none."""
    if label not in LABELS:
        raise ValueError(f"bad label {label!r}")
    now = _now()
    _in_transaction(conn, lambda: [_write_label(conn, g, label, now) for g in group_ids])


def undo_label(conn: sqlite3.Connection, group_id: int) -> None:
    """Remove the label, and the application if it never progressed past 'interested'."""
    undo_labels(conn, [group_id])


def undo_labels(conn: sqlite3.Connection, group_ids: Sequence[int]) -> None:
    """Undo many labels in one transaction."""
    _in_transaction(conn, lambda: [_remove_label(conn, g) for g in group_ids])


def existing_group_ids(conn: sqlite3.Connection, group_ids: Sequence[int]) -> set[int]:
    if not group_ids:
        return set()
    marks = ",".join("?" * len(group_ids))
    rows = conn.execute(f"SELECT id FROM job_group WHERE id IN ({marks})", tuple(group_ids))
    return {r[0] for r in rows}
