"""Dedupe stage: group jobs into ``job_group`` (specs/004 "dedupe", 005, 012).

Passes, cheapest first: exact ``content_hash``, then near-duplicate (rapidfuzz title ratio plus
Jaccard over description word shingles), blocked on ``(primary state, normalized employer)``
inside a date window so the work is O(n*k). Groups are advisory and non-destructive: job rows
are never deleted, and ``split_job`` can undo a bad merge.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from rapidfuzz import fuzz

from jobhunter.pipeline.listing import _txn, to_iso

SHINGLE_WORDS = 5
_GENERIC = re.compile(
    r"\b(?:dept|department|division|div|office|bureau|of|the|and|inc|incorporated|llc|ltd|"
    r"corp|corporation|co|company)\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WORD = re.compile(r"[a-z0-9]+")
_COMPLETENESS_RANK = {"full": 2, "pasted": 1, "partial": 0}


@dataclass
class DedupeResult:
    created: int = 0  # new groups created
    joined: int = 0  # jobs added to an existing group
    exact: int = 0  # joins by content_hash
    near: int = 0  # joins by near-duplicate match


def normalize_employer(name: str | None) -> str | None:
    """Lowercase, drop punctuation and generic words ('dept of', 'inc', 'llc', ...)."""
    if not name:
        return None
    s = _NON_ALNUM.sub(" ", name.lower().replace("&", " and "))
    s = " ".join(_GENERIC.sub(" ", s).split())
    return s or None


def shingles(text: str | None, n: int = SHINGLE_WORDS) -> frozenset[str]:
    words = _WORD.findall((text or "").lower())
    if not words:
        return frozenset()
    if len(words) < n:
        return frozenset([" ".join(words)])
    return frozenset(" ".join(words[i : i + n]) for i in range(len(words) - n + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _when(row: sqlite3.Row) -> datetime | None:
    for key in ("posted_at", "first_seen_at"):
        v = row[key]
        if v:
            try:
                dt = datetime.fromisoformat(v)
            except ValueError:
                continue
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _primary_state(conn: sqlite3.Connection, job_id: int) -> str | None:
    r = conn.execute(
        "SELECT state FROM job_locations WHERE job_id = ? ORDER BY is_primary DESC, id LIMIT 1",
        (job_id,),
    ).fetchone()
    return r["state"] if r else None


def _canonical_key(row: sqlite3.Row) -> tuple[int, int, int]:
    rank = _COMPLETENESS_RANK.get(row["description_completeness"], 0)
    return (rank, len(row["description_text"] or ""), -row["id"])


def _refresh_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Recompute member_count and canonical member (full + longest description wins)."""
    rows = conn.execute(
        "SELECT id, description_completeness, description_text FROM job WHERE job_group_id = ?",
        (group_id,),
    ).fetchall()
    if not rows:
        conn.execute("UPDATE job_group SET member_count = 0 WHERE id = ?", (group_id,))
        return
    best = max(rows, key=_canonical_key)
    conn.execute(
        "UPDATE job_group SET member_count = ?, canonical_job_id = ? WHERE id = ?",
        (len(rows), best["id"], group_id),
    )


def _new_group(conn: sqlite3.Connection, method: str, confidence: float, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, confidence, created_at) "
        "VALUES (NULL, 0, ?, ?, ?)",
        (method, confidence, now),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _assign(conn: sqlite3.Connection, job_id: int, group_id: int) -> None:
    conn.execute(
        "UPDATE job SET job_group_id = ?, stage = 'grouped' WHERE id = ?", (group_id, job_id)
    )


@dataclass
class _Cand:
    id: int
    group_id: int
    when: datetime | None
    title: str
    shingles: frozenset[str]


def group_pending(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    window_days: int = 90,
    title_threshold: float = 92,
    desc_threshold: float = 0.85,
) -> DedupeResult:
    """Group normalized jobs that have no ``job_group_id``; advance them to stage 'grouped'."""
    res = DedupeResult()
    pending = conn.execute(
        "SELECT * FROM job WHERE stage = 'normalized' AND job_group_id IS NULL ORDER BY id"
    ).fetchall()
    if not pending:
        return res
    now_iso = to_iso(now)
    window = timedelta(days=window_days)

    # Block index over already-grouped jobs: (state, employer) -> candidates.
    blocks: dict[tuple[str | None, str], list[_Cand]] = {}

    def block_key(row: sqlite3.Row) -> tuple[str | None, str] | None:
        emp = normalize_employer(row["employer"] or row["agency_raw"])
        if emp is None:
            return None
        return (_primary_state(conn, row["id"]), emp)

    def add_cand(job_id: int) -> None:
        row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        key = block_key(row)
        if key is None or not row["description_text"]:
            return
        blocks.setdefault(key, []).append(
            _Cand(
                row["id"],
                row["job_group_id"],
                _when(row),
                row["title"],
                shingles(row["description_text"]),
            )
        )

    for r in conn.execute("SELECT id FROM job WHERE job_group_id IS NOT NULL").fetchall():
        add_cand(r["id"])

    with _txn(conn):
        touched: set[int] = set()
        for job in pending:
            jid = job["id"]
            if conn.execute("SELECT job_group_id FROM job WHERE id = ?", (jid,)).fetchone()[0]:
                continue  # already grouped as the partner of an earlier exact match
            # (a) exact content hash
            other = None
            if job["content_hash"]:
                other = conn.execute(
                    "SELECT id, job_group_id FROM job WHERE content_hash = ? AND id != ? "
                    "ORDER BY (job_group_id IS NULL), id LIMIT 1",
                    (job["content_hash"], jid),
                ).fetchone()
            if other is not None:
                gid = other["job_group_id"]
                if gid is None:
                    gid = _new_group(conn, "exact_hash", 1.0, now_iso)
                    _assign(conn, other["id"], gid)
                    res.created += 1
                _assign(conn, jid, gid)
                res.joined += 1
                res.exact += 1
                touched.add(gid)
                add_cand(jid)
                continue

            # (b) near duplicate within the (state, employer) block
            best: tuple[float, _Cand] | None = None
            key = block_key(job)
            if key is not None and job["description_text"]:
                sh = shingles(job["description_text"])
                when = _when(job)
                for c in blocks.get(key, ()):
                    if when and c.when and abs(when - c.when) > window:
                        continue
                    t = fuzz.token_set_ratio(job["title"], c.title)
                    if t < title_threshold:
                        continue
                    d = jaccard(sh, c.shingles)
                    if d < desc_threshold:
                        continue
                    sim = (t / 100 + d) / 2
                    if best is None or sim > best[0]:
                        best = (sim, c)
            if best is not None:
                sim, cand = best
                gid = cand.group_id
                _assign(conn, jid, gid)
                conn.execute(
                    "UPDATE job_group SET method = CASE WHEN method = 'manual' THEN method "
                    "ELSE 'near_dupe' END, confidence = MIN(COALESCE(confidence, 1.0), ?) "
                    "WHERE id = ?",
                    (sim, gid),
                )
                res.joined += 1
                res.near += 1
            else:
                # (c) singleton; 'exact_hash' is the closest allowed method (trivially exact)
                gid = _new_group(conn, "exact_hash", 1.0, now_iso)
                _assign(conn, jid, gid)
                res.created += 1
            touched.add(gid)
            add_cand(jid)

        for gid in touched:
            _refresh_group(conn, gid)
    return res


def group_members(conn: sqlite3.Connection, group_id: int) -> list[sqlite3.Row]:
    """Members of a group, canonical first, then by id."""
    return conn.execute(
        "SELECT j.* FROM job j JOIN job_group g ON g.id = j.job_group_id "
        "WHERE g.id = ? ORDER BY (j.id = g.canonical_job_id) DESC, j.id",
        (group_id,),
    ).fetchall()


def split_job(conn: sqlite3.Connection, job_id: int, *, now: datetime) -> int:
    """Move one job into its own new 'manual' group; returns the new group id.

    Non-destructive: no job rows are deleted. A job already alone in its group is left as is.
    """
    row = conn.execute("SELECT job_group_id FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"no such job: {job_id}")
    old = row["job_group_id"]
    if old is not None:
        n = conn.execute("SELECT COUNT(*) FROM job WHERE job_group_id = ?", (old,)).fetchone()[0]
        if n <= 1:
            return old
    with _txn(conn):
        gid = _new_group(conn, "manual", 1.0, to_iso(now))
        conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, job_id))
        _refresh_group(conn, gid)
        if old is not None:
            _refresh_group(conn, old)
    return gid
