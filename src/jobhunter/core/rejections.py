"""Rejections from employers (specs/007 "Gmail matching", specs/006 "Employer rejections").

Not the ``/rejected`` page or the prefilter's "rejected": those are jobs *we* filtered out.
A row here is a recorded fact that an employer turned the candidate down, from a rejection
email (``jobhunter mail match``) or entered by hand on ``/rejections``.

Two uses in scoring, both decided in Python with no model call:

* **Same posting** (``RejectionIndex.same_posting``): the group the rejection matched, or a
  group at the same normalized employer with the same title (``same_title``). Such a group is
  never sent to a scorer (no credits spent) and is kept out of the inbox.
* **Same employer, different role, within N days** (``RejectionIndex.prior``): the job is
  still scored, and the prompt carries one neutral sentence of context. Pay and location stay
  in Python; the bucket is not capped (a flag is shown instead).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_WINDOW_DAYS = 90

_NONWORD = re.compile(r"[^a-z0-9]+")
CORP_WORDS = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "the", "plc", "lp"}


def tokens(s: str) -> list[str]:
    return [t for t in _NONWORD.split((s or "").lower()) if t]


def employer_tokens(name: str) -> list[str]:
    """Lowercase word tokens with corporate suffixes dropped (unless nothing else is left)."""
    toks = tokens(name)
    return [t for t in toks if t not in CORP_WORDS] or toks


def employer_norm(name: str | None) -> str:
    """``"Contoso Corporation"`` -> ``"contoso"``; empty when there is nothing to match on."""
    return " ".join(employer_tokens(name or ""))


def title_norm(title: str | None) -> str:
    return " ".join(tokens(title or ""))


# Words that never distinguish two postings. Everything else must match: a team, specialty
# or level word ("Cloud" vs "Core", "Search" vs "Research", "Engineer II" vs "III", "Leader")
# makes a different role. A fuzzy ratio (token_sort_ratio >= 90) was too loose on short
# titles and hid different roles at the same employer for good (bug d28c8de).
FILLER_WORDS = {"a", "an", "and", "at", "for", "in", "of", "on", "the", "to", "with"}


def _title_words(title: str) -> list[str]:
    out = []
    for t in title.split():
        if t in FILLER_WORDS:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # "Engineers" is "Engineer"
        out.append(t)
    return out


def same_title(a: str | None, b: str | None) -> bool:
    """The same posting title: the same words in any order, ignoring case, punctuation,
    plurals, filler words and spacing ("Full Stack" is "Fullstack")."""
    ta, tb = title_norm(a), title_norm(b)
    if not ta or not tb:
        return False
    wa, wb = _title_words(ta), _title_words(tb)
    return sorted(wa) == sorted(wb) or "".join(wa) == "".join(wb)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Rejection:
    id: int
    received_at: str
    employer: str | None
    employer_norm: str
    title: str | None
    job_group_id: int | None
    application_id: int | None
    source: str

    @property
    def day(self) -> str:
        return (self.received_at or "")[:10]

    def fact(self) -> str:
        """The one neutral sentence a scorer sees for a same-employer, different-role job."""
        role = self.title or "a different role"
        return f"candidate was rejected by this employer for {role} on {self.day}"


def _row(r: Mapping[str, Any]) -> Rejection:
    return Rejection(
        id=r["id"],
        received_at=r["received_at"],
        employer=r["employer"],
        employer_norm=r["employer_norm"] or "",
        title=r["title"],
        job_group_id=r["job_group_id"],
        application_id=r["application_id"],
        source=r["source"],
    )


def load(conn: sqlite3.Connection) -> list[Rejection]:
    rows = conn.execute("SELECT * FROM rejection ORDER BY received_at DESC, id DESC").fetchall()
    return [_row(r) for r in rows]


def record(
    conn: sqlite3.Connection,
    *,
    received_at: str,
    employer: str | None,
    title: str | None,
    source: str,
    now: datetime,
    gmail_message_id: str | None = None,
    thread_id: str | None = None,
    job_group_id: int | None = None,
    application_id: int | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> int | None:
    """Insert one rejection; None when this Gmail message already has a row."""
    if source not in ("email", "manual"):
        raise ValueError(f"bad rejection source {source!r}")  # OR IGNORE would hide the CHECK
    employer = (employer or "").strip() or None
    title = (title or "").strip() or None
    cur = conn.execute(
        "INSERT OR IGNORE INTO rejection (gmail_message_id, thread_id, received_at, employer, "
        "employer_norm, title, job_group_id, application_id, source, evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            gmail_message_id,
            thread_id,
            received_at,
            employer,
            employer_norm(employer) or None,
            title,
            job_group_id,
            application_id,
            source,
            json.dumps(dict(evidence or {})),
            now.astimezone(UTC).isoformat(),
        ),
    )
    return cur.lastrowid if cur.rowcount else None


class RejectionIndex:
    """All rejections, indexed for the two scoring questions. Cheap: rejections are few."""

    def __init__(
        self,
        rejections: Iterable[Rejection],
        now: datetime | None = None,
        days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        self.rejections = list(rejections)
        self.now = now or datetime.now(UTC)
        self.days = days
        self.direct: dict[int, Rejection] = {}
        self.by_employer: dict[str, list[Rejection]] = defaultdict(list)
        for r in self.rejections:
            if r.job_group_id is not None:
                self.direct.setdefault(r.job_group_id, r)
            if r.employer_norm:
                self.by_employer[r.employer_norm].append(r)

    @classmethod
    def load(
        cls, conn: sqlite3.Connection, now: datetime | None = None, days: int = DEFAULT_WINDOW_DAYS
    ) -> RejectionIndex:
        return cls(load(conn), now, days)

    def __bool__(self) -> bool:
        return bool(self.rejections)

    def same_posting(self, group_id: int | None, employer: str | None, title: str | None):
        """The rejection for this very posting, or None. No time window: it stays rejected."""
        if group_id is not None and group_id in self.direct:
            return self.direct[group_id]
        for r in self.by_employer.get(employer_norm(employer), ()):
            if same_title(r.title, title):
                return r
        return None

    def prior(self, group_id: int | None, employer: str | None, title: str | None):
        """The latest same-employer rejection within ``days``, for a different posting."""
        if self.same_posting(group_id, employer, title) is not None:
            return None
        since = self.now - timedelta(days=self.days)
        for r in self.by_employer.get(employer_norm(employer), ()):  # newest first
            when = _parse(r.received_at)
            if when is not None and since <= when <= self.now + timedelta(days=1):
                return r
        return None

    def employers(self) -> list[str]:
        return sorted(self.by_employer)


def _job_employer(row: Mapping[str, Any]) -> str | None:
    keys = row.keys() if hasattr(row, "keys") else ()
    emp = row["employer"] if "employer" in keys else None
    if not emp and "agency_raw" in keys:
        emp = row["agency_raw"]
    return emp


def rejected_group_ids(conn: sqlite3.Connection, index: RejectionIndex | None = None) -> set[int]:
    """Groups that are a posting the candidate was rejected for (never scored, not in inbox).

    Directly matched groups, plus groups at a rejecting employer whose canonical title is a
    ``same_title`` match. A ``LIKE`` on the employer's longest token narrows the scan in SQL first.
    """
    index = index if index is not None else RejectionIndex.load(conn)
    out = set(index.direct)
    for emp, rejs in index.by_employer.items():
        if not any(r.title for r in rejs):
            continue
        key = max(emp.split(), key=len)
        rows = conn.execute(
            "SELECT g.id AS gid, j.title, COALESCE(j.employer, j.agency_raw, '') AS emp "
            "FROM job_group g JOIN job j ON j.id = g.canonical_job_id "
            "WHERE COALESCE(j.employer, j.agency_raw, '') LIKE ?",
            (f"%{key}%",),
        ).fetchall()
        for row in rows:
            if employer_norm(row["emp"]) != emp:
                continue
            if any(same_title(r.title, row["title"]) for r in rejs):
                out.add(row["gid"])
    return out


def rejected_json(conn: sqlite3.Connection) -> str:
    """``rejected_group_ids`` as a JSON array, for ``g.id NOT IN (SELECT value FROM
    json_each(:rejected))`` in the eligibility queries."""
    return json.dumps(sorted(rejected_group_ids(conn)))


def annotate(
    conn: sqlite3.Connection,
    rows: Iterable[MutableMapping[str, Any]],
    *,
    now: datetime | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    index: RejectionIndex | None = None,
) -> None:
    """Set ``row["prior_rejection"]`` (the neutral fact) on job rows at a rejecting employer.

    Rows need ``group_id`` (or ``job_group_id``), ``employer``/``agency_raw`` and ``title``.
    """
    index = index if index is not None else RejectionIndex.load(conn, now, days)
    if not index:
        return
    for row in rows:
        gid = row.get("group_id", row.get("job_group_id"))
        hit = index.prior(gid, _job_employer(row), row.get("title"))
        if hit is not None:
            row["prior_rejection"] = hit.fact()


def unmatched_email_count(conn: sqlite3.Connection) -> int:
    """Rejection emails that matched no job group (applied for outside jobhunter)."""
    return conn.execute(
        "SELECT COUNT(*) FROM rejection WHERE source = 'email' AND job_group_id IS NULL"
    ).fetchone()[0]


def listing(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Rows for the /rejections page, newest first, with the matched job's title if any."""
    rows = conn.execute(
        "SELECT r.*, j.title AS job_title FROM rejection r "
        "LEFT JOIN job_group g ON g.id = r.job_group_id "
        "LEFT JOIN job j ON j.id = g.canonical_job_id "
        "ORDER BY r.received_at DESC, r.id DESC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            ev = json.loads(r["evidence"] or "{}")
        except ValueError:
            ev = {}
        out.append({**dict(r), "evidence": ev if isinstance(ev, dict) else {}})
    return out
