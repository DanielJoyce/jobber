"""Data layer for /sources, /rejected, /search and /costs (specs/007, 003, 006)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunter.console.inbox import BUCKETS, salary_text
from jobhunter.core.models import SourceRow
from jobhunter.pipeline.locations import load_job_group_locations, location_summary
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.prefilter import rejected_summary
from jobhunter.scoring.profile import Profile

# ─── /sources ───────────────────────────────────────────────────────────────

STATUS_ORDER = ["broken", "suspect", "blocked", "manual", "disabled", "ok"]
STATUS_ICON = {
    "ok": "✓",
    "suspect": "⚠",
    "broken": "✗",
    "blocked": "⛔",
    "manual": "✋",
    "disabled": "-",
}
POLICY_STATUS = {"enabled": "ok", "blocked": "blocked", "manual": "manual", "disabled": "disabled"}
ROBOTS_TEXT = {
    "disallow_all": "disallows generic agents",
    "disallow": "disallows generic agents",
    "partial": "disallows part of the site",
    "unknown": "could not be determined",
}


@dataclass
class SourceHealth:
    key: str
    name: str
    state: str | None
    family: str
    tier: str
    policy: str
    status: str
    icon: str
    note: str
    last_ok: str | None
    jobs_7d: int
    min_per_week: int | None
    below_floor: bool
    backlog: int
    error_rate: str
    robots: str
    robots_checked: str | None
    nlx_covered: bool


@dataclass
class RunRow:
    id: int
    started_at: str
    duration: str
    exit_status: str | None
    counts: str


def _short(ts: str | None) -> str | None:
    return ts[:16].replace("T", " ") if ts else None


def _date(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def _duration(start: str, end: str | None) -> str:
    if not end:
        return "running"
    try:
        secs = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return "?"
    m, s = divmod(max(secs, 0), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _counts_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(data, dict):
        return raw
    parts = []
    for stage, val in data.items():
        if isinstance(val, dict):
            inner = ", ".join(f"{k} {v}" for k, v in val.items() if isinstance(v, int | float))
            parts.append(f"{stage}: {inner}" if inner else stage)
        else:
            parts.append(f"{stage}: {val}")
    return "; ".join(parts)


def source_health(conn: sqlite3.Connection, registry: Iterable[SourceRow]) -> list[SourceHealth]:
    """One row per registry source, non-ok first (broken, suspect, blocked, manual, ...)."""
    rows = list(registry)
    db_src = {r["key"]: r for r in conn.execute("SELECT * FROM source")}
    state = {r["source_key"]: r for r in conn.execute("SELECT * FROM source_state")}
    backlog = {
        r["source_key"]: r["n"]
        for r in conn.execute(
            "SELECT source_key, COUNT(*) AS n FROM job WHERE needs_resolve = 1 "
            "AND (description_text IS NULL OR description_text = '') GROUP BY source_key"
        )
    }
    nlx = db_src.get("us-nlx")
    nlx_ok = nlx is not None and nlx["status"] == "ok"
    out: list[SourceHealth] = []
    for reg in rows:
        d = db_src.get(reg.key)
        st = state.get(reg.key)
        status = d["status"] if d else POLICY_STATUS[reg.policy.value]
        policy = d["policy"] if d else reg.policy.value
        checked = _date(d["robots_checked"]) if d else None
        if checked is None and reg.robots.checked:
            checked = reg.robots.checked.date().isoformat()
        robots = (d["robots_status"] if d else None) or reg.robots.status
        note = (d["status_note"] if d else None) or ""
        covered = False
        if status == "blocked" or policy == "blocked":
            what = ROBOTS_TEXT.get(robots, f"is {robots}")
            note = f"blocked: robots.txt {what} (checked {checked or 'never'})" + (
                f" — {note}" if note else ""
            )
            if reg.state and reg.family != "nlx" and nlx_ok:
                covered = True
            else:
                note += " — needs your decision"
        runs = conn.execute(
            "SELECT status FROM run_source WHERE source_key = ? ORDER BY run_id DESC LIMIT 10",
            (reg.key,),
        ).fetchall()
        errors = sum(1 for r in runs if r["status"] != "ok")
        floor = reg.expect.get("min_jobs_per_week")
        jobs_7d = st["jobs_last_7d"] if st else 0
        out.append(
            SourceHealth(
                key=reg.key,
                name=reg.name,
                state=reg.state,
                family=reg.family,
                tier=reg.tier.value,
                policy=policy,
                status=status,
                icon=STATUS_ICON.get(status, "?"),
                note=note,
                last_ok=_short(st["last_ok_at"]) if st else None,
                jobs_7d=jobs_7d,
                min_per_week=floor if isinstance(floor, int) else None,
                below_floor=isinstance(floor, int) and floor > 0 and jobs_7d < floor,
                backlog=backlog.get(reg.key, 0),
                error_rate=f"{errors}/{len(runs)}" if runs else "no runs",
                robots=robots,
                robots_checked=checked,
                nlx_covered=covered,
            )
        )
    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    out.sort(key=lambda s: (order.get(s.status, 0), s.key))
    return out


def recent_runs(conn: sqlite3.Connection, limit: int = 15) -> list[RunRow]:
    return [
        RunRow(
            r["id"],
            _short(r["started_at"]) or "",
            _duration(r["started_at"], r["ended_at"]),
            r["exit_status"],
            _counts_text(r["counts"]),
        )
        for r in conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,))
    ]


# ─── /rejected ──────────────────────────────────────────────────────────────

REJECTED_LIMIT = 500


@dataclass
class RejectedJob:
    group_id: int | None
    title: str
    employer: str
    location: str
    salary: str
    reasons: list[str]
    salary_from_text: bool = False


@dataclass
class Rejected:
    summary: dict[str, int]
    total: int
    jobs: list[RejectedJob]


def rejected(conn: sqlite3.Connection, profile: Profile, reason: str | None = None) -> Rejected:
    version = profile.filter_version
    summary = rejected_summary(conn, version)
    rows = conn.execute(
        "SELECT j.*, p.reasons FROM prefilter_result p JOIN job j ON j.id = p.job_id "
        "WHERE p.filter_version = ? AND p.passed = 0 ORDER BY j.posted_at DESC, j.id",
        (version,),
    ).fetchall()
    jobs: list[RejectedJob] = []
    total = 0
    for r in rows:
        reasons = json.loads(r["reasons"])
        if reason and reason not in reasons:
            continue
        total += 1
        if len(jobs) < REJECTED_LIMIT:
            jobs.append(
                RejectedJob(
                    group_id=r["job_group_id"],
                    title=r["title"],
                    employer=r["employer"] or r["agency_raw"] or "employer not stated",
                    location=location_summary(
                        load_job_group_locations(conn, r["id"]), r["location_scope"]
                    ),
                    salary=salary_text(r),
                    reasons=reasons,
                    salary_from_text=r["salary_source"] == "text",
                )
            )
    return Rejected(summary, total, jobs)


# ─── /search ────────────────────────────────────────────────────────────────

SEARCH_FETCH_LIMIT = 200


def fts_query(text: str) -> str | None:
    """Quote every whitespace-separated term as an FTS5 string so operators lose their meaning.

    Terms without a letter or digit (for example a lone parenthesis) are dropped: they would
    tokenize to nothing. Returns None when nothing searchable is left.
    """
    terms = [t.replace('"', "") for t in text.split()]
    terms = [t for t in terms if re.search(r"\w", t)]
    return " ".join(f'"{t}"' for t in terms) or None


@dataclass
class SearchHit:
    job_id: int
    group_id: int | None
    title: str
    employer: str
    location: str
    salary: str
    posted: str | None
    source_name: str
    bucket: str | None
    dismissed: bool


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    searched: bool = False
    error: str | None = None
    capped: bool = False


def search(
    conn: sqlite3.Connection,
    profile: Profile | None,
    q: str,
    *,
    state: str | None = None,
    source_class: str | None = None,
    bucket_from: str | None = None,
    bucket_to: str | None = None,
    salary_floor: float | None = None,
    posted_after: str | None = None,
) -> SearchResult:
    if not q.strip():
        return SearchResult()
    match = fts_query(q)
    if match is None:
        return SearchResult(searched=True, error="Nothing searchable in that query.")
    where = ["job_fts MATCH ?"]
    params: list[Any] = [match]
    if state:
        where.append("EXISTS (SELECT 1 FROM job_locations l WHERE l.job_id = j.id AND l.state = ?)")
        params.append(state.upper())
    if source_class:
        where.append("s.class = ?")
        params.append(source_class.upper())
    if salary_floor:
        annual = (
            "CASE j.salary_period WHEN 'hour' THEN 2080 WHEN 'day' THEN 260 WHEN 'week' THEN 52 "
            "WHEN 'month' THEN 12 ELSE 1 END"
        )
        where.append(f"MAX(COALESCE(j.salary_max, 0), COALESCE(j.salary_min, 0)) * {annual} >= ?")
        params.append(salary_floor)
    if posted_after:
        where.append("j.posted_at >= ?")
        params.append(posted_after)
    sql = (
        "SELECT j.*, s.name AS source_name, lb.label AS label_value, fs.dimensions, fs.blockers "
        "FROM job_fts JOIN job j ON j.id = job_fts.rowid JOIN source s ON s.key = j.source_key "
        "LEFT JOIN label lb ON lb.job_group_id = j.job_group_id "
        "LEFT JOIN fit_score fs ON fs.id = (SELECT f2.id FROM fit_score f2 "
        "  WHERE f2.job_group_id = j.job_group_id "
        "  ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1) "
        f"WHERE {' AND '.join(where)} ORDER BY job_fts.rank LIMIT ?"
    )
    try:
        rows = conn.execute(sql, [*params, SEARCH_FETCH_LIMIT]).fetchall()
    except sqlite3.OperationalError as exc:
        return SearchResult(searched=True, error=f"Could not run that search ({exc}).")
    lo = BUCKETS.index(bucket_from) if bucket_from in BUCKETS else 0
    hi = BUCKETS.index(bucket_to) if bucket_to in BUCKETS else len(BUCKETS) - 1
    if lo > hi:
        lo, hi = hi, lo
    ranged = bucket_from in BUCKETS or bucket_to in BUCKETS
    hits: list[SearchHit] = []
    for r in rows:
        locs = load_job_group_locations(conn, r["id"])
        bucket = None
        if profile is not None and r["dimensions"] is not None:
            bucket = compute_row(r, r, locs, profile).bucket.value
        if ranged and (bucket is None or not lo <= BUCKETS.index(bucket) <= hi):
            continue
        hits.append(
            SearchHit(
                job_id=r["id"],
                group_id=r["job_group_id"],
                title=r["title"],
                employer=r["employer"] or r["agency_raw"] or "employer not stated",
                location=location_summary(locs, r["location_scope"]),
                salary=salary_text(r),
                posted=_date(r["posted_at"]),
                source_name=r["source_name"],
                bucket=bucket,
                dismissed=r["label_value"] == "not_interesting",
            )
        )
    return SearchResult(hits, True, None, len(rows) >= SEARCH_FETCH_LIMIT)


# ─── /costs ─────────────────────────────────────────────────────────────────

RANGES = (7, 30, 90)


@dataclass
class Costs:
    days: int
    total: float
    daily: list[tuple[str, float]]
    weekly: list[tuple[str, float]]
    by_model: list[dict[str, Any]]
    today: float
    last7: float
    daily_cap: float
    weekly_cap: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_rate: float | None
    zero_cache_batches: list[str]
    shortlisted: int
    per_shortlisted: float | None
    max_daily: float


def costs(
    conn: sqlite3.Connection, now: datetime, days: int, daily_cap: float, weekly_cap: float
) -> Costs:
    today = now.astimezone(UTC).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    spend = conn.execute(
        "SELECT day, model, tier, calls, input_tokens, output_tokens, cost_usd "
        "FROM llm_spend WHERE day >= ? ORDER BY day",
        (since,),
    ).fetchall()
    daily: dict[str, float] = {
        (today - timedelta(days=i)).isoformat(): 0.0 for i in range(days - 1, -1, -1)
    }
    weekly: dict[str, float] = {}
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    for r in spend:
        if r["day"] in daily:
            daily[r["day"]] += r["cost_usd"]
        d = datetime.fromisoformat(r["day"]).date()
        monday = (d - timedelta(days=d.weekday())).isoformat()
        weekly[monday] = weekly.get(monday, 0.0) + r["cost_usd"]
        m = by_model.setdefault(
            (r["model"], r["tier"]),
            {
                "model": r["model"],
                "tier": r["tier"],
                "calls": 0,
                "input": 0,
                "output": 0,
                "cost": 0.0,
            },
        )
        m["calls"] += r["calls"]
        m["input"] += r["input_tokens"] or 0
        m["output"] += r["output_tokens"] or 0
        m["cost"] += r["cost_usd"]
    total = sum(daily.values())
    last7_since = (today - timedelta(days=6)).isoformat()
    last7 = sum(r["cost_usd"] for r in spend if r["day"] >= last7_since)
    fs = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o, "
        "COALESCE(SUM(cache_read_tokens), 0) AS c FROM fit_score "
        "WHERE substr(created_at, 1, 10) >= ?",
        (since,),
    ).fetchone()
    denom = fs["i"] + fs["c"]
    batches = [
        r["batch_id"]
        for r in conn.execute(
            "SELECT batch_id FROM fit_score WHERE batch_id IS NOT NULL "
            "AND substr(created_at, 1, 10) >= ? GROUP BY batch_id "
            "HAVING COUNT(*) > 1 AND SUM(COALESCE(input_tokens, 0)) > 0 "
            "AND SUM(COALESCE(cache_read_tokens, 0)) = 0 ORDER BY batch_id",
            (since,),
        )
    ]
    shortlisted = conn.execute(
        "SELECT COUNT(*) FROM label WHERE label = 'interesting' AND substr(labeled_at, 1, 10) >= ?",
        (since,),
    ).fetchone()[0]
    return Costs(
        days=days,
        total=total,
        daily=list(daily.items()),
        weekly=sorted(weekly.items()),
        by_model=sorted(by_model.values(), key=lambda m: -m["cost"]),
        today=daily.get(today.isoformat(), 0.0),
        last7=last7,
        daily_cap=daily_cap,
        weekly_cap=weekly_cap,
        input_tokens=fs["i"],
        output_tokens=fs["o"],
        cache_read_tokens=fs["c"],
        cache_rate=fs["c"] / denom if denom else None,
        zero_cache_batches=batches,
        shortlisted=shortlisted,
        per_shortlisted=total / shortlisted if shortlisted else None,
        max_daily=max(daily.values(), default=0.0),
    )
