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

from jobhunter.core.manual_sources import EMAIL_MANUAL, PASTE_MANUAL
from jobhunter.pipeline.board_ids import board_key
from jobhunter.pipeline.listing import _txn, from_iso, to_iso
from jobhunter.pipeline.posting_urls import is_posting_url

SHINGLE_WORDS = 5
_GENERIC = re.compile(
    r"\b(?:dept|department|division|div|office|bureau|of|the|and|inc|incorporated|llc|ltd|"
    r"limited|lp|llp|plc|corp|corporation|co|company)\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WORD = re.compile(r"[a-z0-9]+")
_COMPLETENESS_RANK = {"full": 2, "pasted": 1, "partial": 0}
# A Score now in progress (apply/score.py) leaves this prefilter claim on the canonical job.
USER_REQUESTED_REASONS = '["user-requested"]'
CLAIM_TTL = timedelta(minutes=15)


@dataclass
class DedupeResult:
    created: int = 0  # new groups created
    joined: int = 0  # jobs added to an existing group
    exact: int = 0  # joins by content_hash
    near: int = 0  # joins by near-duplicate match
    board: int = 0  # joins by a LinkedIn / Indeed job id
    url: int = 0  # joins to a captured or pasted posting by a one-posting URL key
    on_request_kept: int = 0  # joins into a group that stays scored on request
    on_request_cleared: int = 0  # joins that let the nightly run score the group once


def normalize_employer(name: str | None) -> str | None:
    """Lowercase, drop punctuation and generic words ('dept of', 'inc', 'llc', ...)."""
    if not name:
        return None
    # Fold "L.L.C." / "Inc." / "O'Brien" before punctuation becomes a word break.
    s = name.lower().replace("&", " and ").replace(".", "").replace("'", "").replace("\u2019", "")
    s = _NON_ALNUM.sub(" ", s)
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


def keeps_on_request(conn: sqlite3.Connection, group_id: int, now: datetime) -> bool:
    """Whether a scored-on-request group stays on request when an ingested copy joins it.

    It does when it is scored, being scored (a live Score now claim or an open batch item), or
    applied to (an application, so also any packet). Otherwise the nightly run may score it
    once (specs/017 "A later ingest of a captured posting").
    """
    one = conn.execute(
        "SELECT 1 FROM fit_score WHERE job_group_id = ? "
        "UNION ALL SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id "
        "WHERE i.job_group_id = ? AND b.collected_at IS NULL "
        "UNION ALL SELECT 1 FROM application WHERE job_group_id = ? LIMIT 1",
        (group_id, group_id, group_id),
    ).fetchone()
    if one is not None:
        return True
    for r in conn.execute(
        "SELECT p.evaluated_at FROM prefilter_result p JOIN job j ON j.id = p.job_id "
        "WHERE j.job_group_id = ? AND p.reasons = ?",
        (group_id, USER_REQUESTED_REASONS),
    ):
        try:
            if now - from_iso(r[0]) < CLAIM_TTL:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _after_join(conn: sqlite3.Connection, group_id: int, now: datetime, res: DedupeResult) -> None:
    """The scored-on-request rule for an ingested job that just joined ``group_id``."""
    row = conn.execute(
        "SELECT score_on_request FROM job_group WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None or not row[0]:
        return
    if keeps_on_request(conn, group_id, now):
        res.on_request_kept += 1
        return
    conn.execute("UPDATE job_group SET score_on_request = 0 WHERE id = ?", (group_id,))
    # The pasted job sat at 'normalized' so no nightly stage touched it; as an ordinary member
    # it is grouped, so the prefilter evaluates it if it stays canonical.
    conn.execute(
        "UPDATE job SET stage = 'grouped' WHERE job_group_id = ? AND stage = 'normalized'",
        (group_id,),
    )
    res.on_request_cleared += 1


def _url_keys(*urls: str | None) -> set[str]:
    """One-posting URL keys (``normalize_apply_url`` that ``_identifies_job``) of these URLs."""
    from jobhunter.pipeline.dedupe_url import _identifies_job, normalize_apply_url

    keys: set[str] = set()
    for u in urls:
        try:
            k = normalize_apply_url(u)
        except ValueError:
            continue
        if k and _identifies_job(k):
            keys.add(k)
    return keys


def _board_keys(*urls: str | None) -> set[str]:
    return {k for u in urls if (k := board_key(u))}


def _board_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Board job id -> group: ``job_board_ref`` first, then stored board URLs (any group)."""
    index: dict[str, int] = {}
    for r in conn.execute(
        "SELECT r.board, r.board_id, j.job_group_id FROM job_board_ref r "
        "JOIN job j ON j.id = r.job_id WHERE j.job_group_id IS NOT NULL ORDER BY r.seen_at"
    ):
        index.setdefault(f"{r[0]}:{r[1]}", int(r[2]))
    for r in conn.execute(
        "SELECT url, apply_url, page_url, job_group_id FROM job WHERE job_group_id IS NOT NULL "
        "AND (url LIKE '%linkedin.%' OR url LIKE '%indeed.%' OR apply_url LIKE '%linkedin.%' "
        "OR apply_url LIKE '%indeed.%' OR page_url LIKE '%linkedin.%' "
        "OR page_url LIKE '%indeed.%') ORDER BY id"
    ):
        for k in _board_keys(r[0], r[1], r[2]):
            index.setdefault(k, int(r[3]))
    return index


def _captured_url_index(conn: sqlite3.Connection) -> dict[str, int]:
    """One-posting URL key -> group, for pasted and captured jobs (``paste-manual``).

    The captured job's chosen URL and its page URL are both keys; its apply URL and apply link
    only when they certainly name one posting (``posting_urls``), never a careers path.
    A key two groups share maps to neither.
    """
    seen: dict[str, set[int]] = {}
    for r in conn.execute(
        "SELECT j.url, j.apply_url, j.page_url, a.start_url, a.final_url, j.job_group_id "
        "FROM job j LEFT JOIN apply_link a ON a.job_group_id = j.job_group_id "
        "WHERE j.source_key = ? AND j.job_group_id IS NOT NULL",
        (PASTE_MANUAL,),
    ):
        applies = [u for u in (r[1], r[3], r[4]) if is_posting_url(u)]
        for k in _url_keys(r[0], r[2], *applies):
            seen.setdefault(k, set()).add(int(r[5]))
    return {k: next(iter(g)) for k, g in seen.items() if len(g) == 1}


def group_pending(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    window_days: int = 90,
    title_threshold: float = 92,
    desc_threshold: float = 0.85,
) -> DedupeResult:
    """Group normalized jobs that have no ``job_group_id``; advance them to stage 'grouped'.

    Passes, in order: a LinkedIn / Indeed job id (``job_board_ref`` or a stored board URL, to
    any group); a one-posting URL key equal to a captured or pasted job's (specs/017 1e); exact
    ``content_hash``; near duplicate. Employer and title alone never join. A job that joins a
    group scored only on request leaves it on request when that group is scored, being scored
    or applied to, and otherwise clears the flag so the nightly run scores it once.
    """
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

    # Pasted and captured postings are match targets (specs/017 "A later ingest of a captured
    # posting"): the scored-on-request flag on their group, not the canonical job's source,
    # keeps the nightly run from paying for them. Groups made from mail confirmations
    # (email-manual) carry no posting text or URL to match on and stay out.
    for r in conn.execute(
        "SELECT id FROM job WHERE job_group_id IS NOT NULL AND source_key != ?", (EMAIL_MANUAL,)
    ).fetchall():
        add_cand(r["id"])
    boards = _board_index(conn)
    captured = _captured_url_index(conn)

    def joined(jid: int, gid: int, keys: set[str]) -> None:
        _after_join(conn, gid, now, res)
        touched.add(gid)
        add_cand(jid)
        for k in keys:
            boards.setdefault(k, gid)

    with _txn(conn):
        touched: set[int] = set()
        for job in pending:
            jid = job["id"]
            if conn.execute("SELECT job_group_id FROM job WHERE id = ?", (jid,)).fetchone()[0]:
                continue  # already grouped as the partner of an earlier exact match
            mine = _board_keys(job["url"], job["apply_url"])
            # (0) a board job id seen before (an alert, a capture, a Link them)
            gid = next((boards[k] for k in sorted(mine) if k in boards), None)
            if gid is not None:
                res.board += 1
            else:
                # (0b) the URL of a captured or pasted posting, one posting only
                urls = sorted(_url_keys(job["url"], job["apply_url"]))
                gid = next((captured[k] for k in urls if k in captured), None)
                if gid is not None:
                    res.url += 1
            if gid is not None:
                _assign(conn, jid, gid)
                res.joined += 1
                joined(jid, gid, mine)
                continue
            # (a) exact content hash
            other = None
            if job["content_hash"]:
                other = conn.execute(
                    "SELECT id, job_group_id FROM job WHERE content_hash = ? AND id != ? "
                    "AND source_key != ? ORDER BY (job_group_id IS NULL), id LIMIT 1",
                    (job["content_hash"], jid, EMAIL_MANUAL),
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
                joined(jid, gid, mine)
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
                joined(jid, gid, mine)
                continue
            # (c) singleton; 'exact_hash' is the closest allowed method (trivially exact)
            gid = _new_group(conn, "exact_hash", 1.0, now_iso)
            _assign(conn, jid, gid)
            res.created += 1
            touched.add(gid)
            add_cand(jid)
            for k in mine:
                boards.setdefault(k, gid)

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
