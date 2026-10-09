"""Dedupe by resolved application URL (specs/004 "dedupe", specs/015).

The same opening often arrives from several sources with different text but one application
target. ``normalize_apply_url`` reduces a URL to a comparison key; ``merge_by_apply_url`` merges
job groups that share a key into the oldest group. Non-destructive: job rows are only re-pointed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

from jobhunter.pipeline.ats_rules import AtsRule, is_http_url, match_ats, unwrap
from jobhunter.pipeline.dedupe import _refresh_group
from jobhunter.pipeline.listing import _txn, to_iso

# gh_jid is deliberately absent: on employer-hosted Greenhouse embeds it IS the job id.
_TRACKING_PARAMS = frozenset(
    {"src", "source", "ref", "refid", "gh_src", "iis", "iisn", "mode", "trk", "fbclid", "gclid"}
)
_APPLY_SEGMENTS = ("apply", "application")
# ATS whose posting path (through the job id) alone identifies the job; the rest is dropped.
_TRUNCATE_TO_POSTING = frozenset({"greenhouse", "lever", "ashby", "neogov", "icims"})
_MAX_UNWRAP = 3


def _strip_apply_suffix(path: str) -> str:
    segs = path.split("/")
    for i in range(len(segs) - 1, 0, -1):
        if segs[i] in _APPLY_SEGMENTS:
            return "/".join(segs[:i])
    return path


def _ats_form(rule: AtsRule, url: str, path: str, query: str) -> tuple[str, str]:
    """(path, query) in the ATS posting form, apply suffix removed."""
    if not rule.is_posting(url):
        return path, query
    if rule.name in _TRUNCATE_TO_POSTING:
        m = rule.posting.search(path)
        if m:
            return path[: m.end()], ""
    return _strip_apply_suffix(path), query


def normalize_apply_url(url: str | None) -> str | None:
    """Comparison key for an application URL, or None if it is not an http(s) URL."""
    if not url or not url.strip():
        return None
    url = url.strip()
    for _ in range(_MAX_UNWRAP):
        inner = unwrap(url)
        if inner is None:
            break
        url = inner
    if not is_http_url(url):
        return None
    p = urlsplit(url)
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if p.port and (scheme, p.port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{p.port}"
    pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(pairs))
    path = p.path.rstrip("/")
    rule = match_ats(url)
    if rule is not None:
        path, query = _ats_form(rule, url, path, query)
        path = path.rstrip("/")
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def _identifies_job(key: str) -> bool:
    """False for keys that point at a site or board home rather than one posting."""
    p = urlsplit(key)
    if not p.path and not p.query:
        return False
    rule = match_ats(key)
    return rule is None or rule.is_posting(key)


@dataclass
class MergeResult:
    keys: int = 0  # shared keys that triggered a merge
    groups_merged: int = 0  # groups absorbed into a survivor
    jobs_moved: int = 0
    scores_dropped: int = 0
    scores_moved: int = 0
    labels_merged: int = 0
    applications_merged: int = 0


_LABEL_RANK = {"not_interesting": 0, "interesting": 1, "applied": 2}
_APP_ORDER = (
    "interested", "preparing", "applied", "acknowledged", "screening", "interview",
    "offer", "rejected", "withdrawn", "no_response", "closed",
)  # fmt: skip
_APP_RANK = {s: i for i, s in enumerate(_APP_ORDER)}
_LINK_RANK = {"live": 0, "expired": 1}


def _collect_keys(conn: sqlite3.Connection) -> dict[str, set[int]]:
    """key -> group ids. Manual groups (user splits) are excluded so a split sticks."""
    keys: dict[str, set[int]] = {}

    def add(raw: str | None, gid: int) -> None:
        key = normalize_apply_url(raw)
        if key and _identifies_job(key):
            keys.setdefault(key, set()).add(gid)

    for r in conn.execute(
        "SELECT j.apply_url, j.job_group_id FROM job j JOIN job_group g ON g.id = j.job_group_id "
        "WHERE j.apply_url IS NOT NULL AND g.method != 'manual'"
    ):
        add(r[0], r[1])
    for r in conn.execute(
        "SELECT a.final_url, a.job_group_id FROM apply_link a "
        "JOIN job_group g ON g.id = a.job_group_id "
        "WHERE a.status IN ('live', 'expired') AND a.final_url IS NOT NULL "
        "AND g.method != 'manual'"
    ):
        add(r[0], r[1])
    return keys


def _components(keys: dict[str, set[int]]) -> tuple[list[list[int]], int]:
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    shared = 0
    for gids in keys.values():
        if len(gids) < 2:
            continue
        shared += 1
        first, *rest = sorted(gids)
        for g in rest:
            parent[find(g)] = find(first)
    comps: dict[int, list[int]] = {}
    for g in list(parent):
        comps.setdefault(find(g), []).append(g)
    return [sorted(c) for c in comps.values() if len(c) > 1], shared


def merge_by_apply_url(conn: sqlite3.Connection, *, now: datetime) -> MergeResult:
    """Merge job groups that share a normalized application URL into the oldest of them."""
    res = MergeResult()
    comps, res.keys = _components(_collect_keys(conn))
    if not comps:
        return res
    with _txn(conn):
        for comp in comps:
            ordered = conn.execute(
                f"SELECT id FROM job_group WHERE id IN ({','.join('?' * len(comp))}) "
                "ORDER BY created_at, id",
                comp,
            ).fetchall()
            survivor, *absorbed = (r[0] for r in ordered)
            for gid in absorbed:
                _absorb(conn, survivor, gid, now, res)
            _refresh_group(conn, survivor)
            conn.execute(
                "UPDATE job_group SET method = 'apply_url', confidence = 1.0 WHERE id = ?",
                (survivor,),
            )
    return res


def _absorb(conn: sqlite3.Connection, s: int, a: int, now: datetime, res: MergeResult) -> None:
    res.jobs_moved += conn.execute(
        "UPDATE job SET job_group_id = ? WHERE job_group_id = ?", (s, a)
    ).rowcount
    _merge_apply_link(conn, s, a)
    for table in ("apply_click", "score_batch_item"):
        conn.execute(f"UPDATE {table} SET job_group_id = ? WHERE job_group_id = ?", (s, a))
    _merge_scores(conn, s, a, res)
    _merge_label(conn, s, a, res)
    _merge_application(conn, s, a, now, res)
    # canonical_job_id of the absorbed group points at a job that still exists; just delete it.
    conn.execute("DELETE FROM job_group WHERE id = ?", (a,))
    res.groups_merged += 1


def _merge_apply_link(conn: sqlite3.Connection, s: int, a: int) -> None:
    rows = {
        r["job_group_id"]: r
        for r in conn.execute("SELECT * FROM apply_link WHERE job_group_id IN (?, ?)", (s, a))
    }
    if a not in rows:
        return
    if s in rows and _LINK_RANK.get(rows[a]["status"], 9) >= _LINK_RANK.get(rows[s]["status"], 9):
        conn.execute("DELETE FROM apply_link WHERE job_group_id = ?", (a,))
        return
    # survivor has none, or the absorbed link is strictly better (live over expired/unresolved)
    conn.execute("DELETE FROM apply_link WHERE job_group_id = ?", (s,))
    conn.execute("UPDATE apply_link SET job_group_id = ? WHERE job_group_id = ?", (s, a))


def _merge_scores(conn: sqlite3.Connection, s: int, a: int, res: MergeResult) -> None:
    rows = conn.execute(
        "SELECT id, tier, prompt_version, scoring_version, model, input_rev FROM fit_score "
        "WHERE job_group_id = ?",
        (a,),
    ).fetchall()
    for r in rows:
        dup = conn.execute(
            "SELECT 1 FROM fit_score WHERE job_group_id = ? AND tier = ? AND prompt_version = ? "
            "AND scoring_version = ? AND model = ? AND input_rev = ?",
            (s, r["tier"], r["prompt_version"], r["scoring_version"], r["model"], r["input_rev"]),
        ).fetchone()
        if dup:
            conn.execute("DELETE FROM fit_score WHERE id = ?", (r["id"],))
            res.scores_dropped += 1
        else:
            conn.execute("UPDATE fit_score SET job_group_id = ? WHERE id = ?", (s, r["id"]))
            res.scores_moved += 1


def _merge_label(conn: sqlite3.Connection, s: int, a: int, res: MergeResult) -> None:
    la = conn.execute("SELECT * FROM label WHERE job_group_id = ?", (a,)).fetchone()
    if la is None:
        return
    ls = conn.execute("SELECT * FROM label WHERE job_group_id = ?", (s,)).fetchone()
    if ls is None:
        conn.execute("UPDATE label SET job_group_id = ? WHERE job_group_id = ?", (s, a))
        res.labels_merged += 1
        return
    # One label per group: the stronger one wins (applied > interesting > not_interesting;
    # ties go to the most recent). The loser is kept as text in the note.
    rank_s = (_LABEL_RANK[ls["label"]], ls["labeled_at"])
    rank_a = (_LABEL_RANK[la["label"]], la["labeled_at"])
    win, lose = (la, ls) if rank_a > rank_s else (ls, la)
    if win["label"] != lose["label"] or lose["note"]:
        extra = f"merged label {lose['label']}" + (f" ({lose['note']})" if lose["note"] else "")
        note = f"{win['note']}; {extra}" if win["note"] else extra
        conn.execute(
            "UPDATE label SET note = ? WHERE job_group_id = ?", (note, win["job_group_id"])
        )
    conn.execute("DELETE FROM label WHERE job_group_id = ?", (lose["job_group_id"],))
    if win is la:
        conn.execute("UPDATE label SET job_group_id = ? WHERE job_group_id = ?", (s, a))
    res.labels_merged += 1


def _merge_application(
    conn: sqlite3.Connection, s: int, a: int, now: datetime, res: MergeResult
) -> None:
    aa = conn.execute("SELECT * FROM application WHERE job_group_id = ?", (a,)).fetchone()
    if aa is None:
        return
    sa = conn.execute("SELECT * FROM application WHERE job_group_id = ?", (s,)).fetchone()
    if sa is None:
        conn.execute("UPDATE application SET job_group_id = ? WHERE job_group_id = ?", (s, a))
        return
    res.applications_merged += 1
    # Keep the more advanced application; ties keep the survivor's (the older group's).
    win, lose = (aa, sa) if _APP_RANK[aa["status"]] > _APP_RANK[sa["status"]] else (sa, aa)
    for table in ("application_event", "contact", "attachment"):
        conn.execute(
            f"UPDATE {table} SET application_id = ? WHERE application_id = ?",
            (win["id"], lose["id"]),
        )
    detail = f"status {lose['status']}"
    if lose["applied_at"]:
        detail += f", applied {lose['applied_at']}"
    conn.execute(
        "INSERT INTO application_event (application_id, at, status, note, source) "
        "VALUES (?, ?, ?, ?, 'manual')",
        (win["id"], to_iso(now), win["status"], f"Merged duplicate application ({detail})"),
    )
    conn.execute("DELETE FROM application WHERE id = ?", (lose["id"],))
    conn.execute(
        "UPDATE application SET job_group_id = ?, applied_at = COALESCE(applied_at, ?), "
        "resume_version = COALESCE(resume_version, ?), external_ref = COALESCE(external_ref, ?), "
        "next_action = COALESCE(next_action, ?), next_action_at = COALESCE(next_action_at, ?), "
        "updated_at = ? WHERE id = ?",
        (
            s,
            lose["applied_at"],
            lose["resume_version"],
            lose["external_ref"],
            lose["next_action"],
            lose["next_action_at"],
            to_iso(now),
            win["id"],
        ),
    )
