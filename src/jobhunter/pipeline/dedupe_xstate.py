"""Cross-state dedupe: one posting syndicated to several state boards (bug 4558795).

``group_pending`` blocks on ``(state, employer)``, so the NLx copies of one posting (one per
state site, each with its own GUID, apply URL and a truncated or re-wrapped description) never
meet. This pass blocks on ``(normalized employer, normalized title)`` with no state, and merges
groups in a block whose canonical descriptions are near-identical (shingle Jaccard, or
containment of the shorter text in the longer to tolerate truncated copies) or that share a
normalized apply URL. The oldest group survives. Manual (user-split) groups are never touched.
Non-destructive: job rows are only re-pointed, and each keeps its own ``job_locations``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from jobhunter.core.models import LocationScope
from jobhunter.pipeline.ats_rules import match_ats
from jobhunter.pipeline.dedupe import _refresh_group, jaccard, normalize_employer, shingles
from jobhunter.pipeline.dedupe_url import (
    MergeResult,
    _absorb,
    _identifies_job,
    normalize_apply_url,
)
from jobhunter.pipeline.listing import _txn
from jobhunter.pipeline.locations import REMOTE_SCOPES

log = logging.getLogger(__name__)

MAX_BLOCK = 200
JACCARD_THRESHOLD = 0.9
CONTAINMENT_THRESHOLD = 0.85
MIN_CONTAINMENT_SHINGLES = 20  # a tiny snippet is "contained" in everything

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Some sources store newlines as a literal "n" ("software.nnJoin", " n * Partner").
_LITERAL_N = re.compile(r"(?<=[.:;!?])n+(?=[A-Z])|(?<=\s)n(?=\s)")


def normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    s = _NON_ALNUM.sub(" ", title.lower().replace("&", " and ")).strip()
    return s or None


@dataclass
class XStateResult(MergeResult):
    blocks: int = 0  # blocks with at least two groups
    blocks_skipped: int = 0  # oversized blocks left alone
    would_merge: int = 0  # groups that merge (or would, on a dry run)


@dataclass
class _G:
    id: int
    created_at: str
    shingles: frozenset[str]
    apply_keys: frozenset[str]


def _posting_hosts(keys: frozenset[str]) -> dict[str, str]:
    """key -> host label, for keys whose path names a posting on that host.

    Known ATS rules label by ATS name (all its hosts); other hosts label by hostname, and only
    when the path is non-trivial (a wrapper like ``aplitrak.com/?adid=...`` identifies nothing).
    """
    out: dict[str, str] = {}
    for k in keys:
        rule = match_ats(k)
        if rule is not None:
            out[k] = rule.name
            continue
        p = urlsplit(k)
        if p.path.strip("/"):
            out[k] = (p.hostname or "").lower()
    return out


def _distinct_requisitions(a: _G, b: _G) -> bool:
    """True if both groups apply on the same host to different postings.

    Different announcement/job ids on one ATS or board (USAJOBS, USA Staffing, Workday,
    Greenhouse, ...) are separate requisitions even when the text is boilerplate-identical.
    Cross-host differences (state boards, wrappers) prove nothing and are ignored.
    """
    ha, hb = _posting_hosts(a.apply_keys), _posting_hosts(b.apply_keys)
    return any(ka != kb and na == nb for ka, na in ha.items() for kb, nb in hb.items())


def _similar(a: _G, b: _G, jaccard_min: float, containment_min: float) -> bool:
    if a.apply_keys & b.apply_keys:
        return True
    if _distinct_requisitions(a, b):
        return False
    if jaccard(a.shingles, b.shingles) >= jaccard_min:
        return True
    small, big = sorted((a.shingles, b.shingles), key=len)
    return (
        len(small) >= MIN_CONTAINMENT_SHINGLES and len(small & big) / len(small) >= containment_min
    )


def _blocks(conn: sqlite3.Connection) -> dict[tuple[str, str], list[_G]]:
    rows = conn.execute(
        "SELECT g.id AS gid, g.created_at, c.title, c.employer, c.agency_raw, "
        "c.description_text FROM job_group g JOIN job c ON c.id = g.canonical_job_id "
        "WHERE g.method != 'manual'"
    ).fetchall()
    keys: dict[int, set[str]] = {}
    for r in conn.execute(
        "SELECT j.apply_url, j.job_group_id FROM job j JOIN job_group g ON g.id = j.job_group_id "
        "WHERE j.apply_url IS NOT NULL AND g.method != 'manual'"
    ):
        k = normalize_apply_url(r[0])
        if k and _identifies_job(k):
            keys.setdefault(r[1], set()).add(k)
    for r in conn.execute(
        "SELECT a.final_url, a.job_group_id FROM apply_link a "
        "JOIN job_group g ON g.id = a.job_group_id "
        "WHERE a.status IN ('live', 'expired') AND a.final_url IS NOT NULL "
        "AND g.method != 'manual'"
    ):
        k = normalize_apply_url(r[0])
        if k and _identifies_job(k):
            keys.setdefault(r[1], set()).add(k)
    blocks: dict[tuple[str, str], list[_G]] = {}
    for r in rows:
        emp = normalize_employer(r["employer"] or r["agency_raw"])
        title = normalize_title(r["title"])
        if emp is None or title is None:
            continue
        text = _LITERAL_N.sub(" ", r["description_text"] or "")
        blocks.setdefault((emp, title), []).append(
            _G(r["gid"], r["created_at"], shingles(text), frozenset(keys.get(r["gid"], ())))
        )
    return {k: v for k, v in blocks.items() if len(v) > 1}


def _components(groups: list[_G], jaccard_min: float, containment_min: float) -> list[list[_G]]:
    parent = list(range(len(groups)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    members: dict[int, list[int]] = {i: [i] for i in range(len(groups))}
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            ri, rj = find(i), find(j)
            if ri == rj or not _similar(groups[i], groups[j], jaccard_min, containment_min):
                continue
            # Never let a chain of links bridge two distinct requisitions.
            if any(
                _distinct_requisitions(groups[x], groups[y])
                for x in members[ri]
                for y in members[rj]
            ):
                continue
            parent[rj] = ri
            members[ri].extend(members.pop(rj))
    comps: dict[int, list[_G]] = {}
    for i, g in enumerate(groups):
        comps.setdefault(find(i), []).append(g)
    return [sorted(c, key=lambda g: (g.created_at, g.id)) for c in comps.values() if len(c) > 1]


def _rescope(conn: sqlite3.Connection, group_id: int) -> None:
    """Set the merged group's jobs to multi_state (remote_us if any member is remote)."""
    members = conn.execute(
        "SELECT location_scope FROM job WHERE job_group_id = ?", (group_id,)
    ).fetchall()
    remote = {s.value for s in REMOTE_SCOPES}
    if any(m["location_scope"] in remote for m in members):
        scope = LocationScope.remote_us
    else:
        states = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT l.state FROM job_locations l JOIN job j ON j.id = l.job_id "
                "WHERE j.job_group_id = ? AND l.state IS NOT NULL",
                (group_id,),
            )
        }
        if len(states) < 2:
            return
        scope = LocationScope.multi_state
    conn.execute(
        "UPDATE job SET location_scope = ? WHERE job_group_id = ?", (scope.value, group_id)
    )


def merge_cross_state(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    dry_run: bool = False,
    jaccard_min: float = JACCARD_THRESHOLD,
    containment_min: float = CONTAINMENT_THRESHOLD,
    max_block: int = MAX_BLOCK,
) -> XStateResult:
    """Merge groups of the same posting across states into the oldest group of each cluster."""
    res = XStateResult()
    plans: list[list[_G]] = []
    for (emp, title), groups in _blocks(conn).items():
        res.blocks += 1
        if len(groups) > max_block:
            res.blocks_skipped += 1
            log.warning(
                "cross-state dedupe: skipping block (%r, %r) with %d groups (max %d)",
                emp,
                title,
                len(groups),
                max_block,
            )
            continue
        plans.extend(_components(groups, jaccard_min, containment_min))
    res.would_merge = sum(len(c) - 1 for c in plans)
    if dry_run or not plans:
        return res
    with _txn(conn):
        for comp in plans:
            survivor, *absorbed = (g.id for g in comp)
            for gid in absorbed:
                _absorb(conn, survivor, gid, now, res)
            _refresh_group(conn, survivor)
            conn.execute(
                "UPDATE job_group SET method = 'near_dupe', "
                "confidence = MIN(COALESCE(confidence, 1.0), ?) WHERE id = ?",
                (jaccard_min, survivor),
            )
            _rescope(conn, survivor)
    return res
