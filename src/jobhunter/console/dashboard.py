"""Today dashboard data layer (specs/013-dashboard.md): KPI tiles and per-state stats.

Every number is computed from the 005 tables on request; nothing is cached. Buckets are
recomputed from the latest stored fit score with ``scoring.buckets.compute_row`` so editing the
profile re-sorts the dashboard for free. All functions take an explicit ``now`` so tests can
pin the clock.

Day boundaries are UTC calendar days, compared on the ISO date prefix of stored timestamps.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from jobhunter.console.tracking import effective_events
from jobhunter.core import geo, rejections
from jobhunter.core.bucketnames import (
    BUCKET_TITLES,
    DEFAULT_GROUP,
    FIT_GROUP,
    group_name,
    parse_letters,
)
from jobhunter.core.manual_sources import EMAIL_MANUAL, PASTE_MANUAL
from jobhunter.core.models import Bucket, JobLocation
from jobhunter.core.textnorm import annualize
from jobhunter.pipeline.locations import REMOTE_SCOPES
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import Profile

RANGES: tuple[int, ...] = (7, 30, 90)
DEFAULT_RANGE = 7
REMOTE = "REMOTE"
RESPONSE_MIN_N = 5
RESPONSE_WINDOW_DAYS = 30
EMAIL_STALE_DAYS = 3

IN_FLIGHT = ("applied", "acknowledged", "screening", "interview")
# Statuses that count as an employer response. ``acknowledged`` is excluded: it is usually an
# automated receipt, not a person reading the application.
RESPONSE = ("screening", "interview", "offer", "rejected")
TERMINAL = ("rejected", "withdrawn", "no_response", "closed")
NOT_YET_APPLIED = ("interested", "preparing")
# Statuses that can only be reached after applying.
APPLIED_EVIDENCE = (
    "applied",
    "acknowledged",
    "screening",
    "interview",
    "offer",
    "rejected",
    "no_response",
)

# metric key -> (label, kind). kind drives break rounding and value formatting.
METRICS: dict[str, tuple[str, str]] = {
    "new_ab": (
        "New in selected buckets",
        "count",
    ),  # key stays: URLs and sort keys; label follows the buckets
    "scored": ("All scored jobs", "count"),
    "shortlisted": ("Shortlisted", "count"),
    "applied": ("Applied", "count"),
    "response_rate": ("Response rate", "rate"),
    "median_salary": ("Median offered salary", "money"),
    "col_adjusted": ("COL-adjusted salary", "money"),
}
DEFAULT_METRIC = "new_ab"

# Coverage status (013 "Status layer"): code -> (icon, label). Order is severity for sorting.
COVERAGE: dict[str, tuple[str, str]] = {
    "critical": ("⚠", "source broken"),
    "serious": ("⚠", "source suspect"),
    "email_stale": ("✉?", "email, no alerts in 3 days"),
    "email": ("✉", "email alerts only"),
    "direct": ("●", "collected directly"),
    "none": ("○", "not covered"),
}
MAIL_FAMILIES = ("mailalerts", "mail")

# Sortable state-table columns; "state" sorts by name.
SORT_KEYS: tuple[str, ...] = (
    "state",
    "new_ab",
    "scored",
    "shortlisted",
    "applied",
    "responses",
    "response_rate",
    "median_salary",
    "col_adjusted",
    "coverage",
    "applications",
    "last_ok_at",
)


def clamp_range(value: int | str | None) -> int:
    """Coerce a ``range`` query value to one of 7, 30, 90."""
    try:
        days = int(value) if value is not None else DEFAULT_RANGE
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return days if days in RANGES else DEFAULT_RANGE


def clamp_metric(value: str | None) -> str:
    return value if value in METRICS else DEFAULT_METRIC


# ─── time helpers ───────────────────────────────────────────────────────────


def _today(now: datetime) -> date:
    return now.astimezone(UTC).date() if now.tzinfo else now.date()


def _days(now: datetime, range_days: int) -> list[str]:
    """ISO dates of the ``range_days`` days ending today, oldest first."""
    today = _today(now)
    return [(today - timedelta(days=i)).isoformat() for i in range(range_days - 1, -1, -1)]


def _since(now: datetime, range_days: int) -> str:
    return _days(now, range_days)[0]


# ─── COL hook ───────────────────────────────────────────────────────────────

# TODO(BEA RPP): load the Bureau of Economic Analysis Regional Price Parities (all items, by
# state, annual) from a vendored data file such as specs/data/bea-rpp-<year>.json and return
# the index (US = 100). Until then COL-adjusted salary is None everywhere.
RPP: dict[str, float] = {}


def rpp_index(usps: str) -> float | None:
    """Regional Price Parity for a state (US = 100), or None when not loaded."""
    return RPP.get(usps)


def col_adjust(salary: float | None, usps: str) -> float | None:
    rpp = rpp_index(usps)
    if salary is None or not rpp:
        return None
    return salary / (rpp / 100.0)


# ─── job groups ─────────────────────────────────────────────────────────────


@dataclass
class GroupFact:
    group_id: int
    job_id: int
    first_seen: str
    scored: bool
    bucket: Bucket | None
    states: set[str] = field(default_factory=set)
    remote: bool = False
    salary: float | None = None  # annualized midpoint of a stated salary


_GROUPS_SQL = """
WITH g AS (
  SELECT job_group_id AS gid, MIN(first_seen_at) AS first_seen
  FROM job WHERE job_group_id IS NOT NULL
  GROUP BY job_group_id
  HAVING MIN(first_seen_at) >= :since
)
SELECT g.gid, g.first_seen, j.id AS job_id, j.salary_min, j.salary_max, j.salary_period,
       j.salary_stated, j.location_scope,
       fs.id AS fs_id, fs.dimensions, fs.blockers
FROM g
JOIN job_group jg ON jg.id = g.gid
JOIN job j ON j.id = jg.canonical_job_id
LEFT JOIN fit_score fs ON fs.id = (
  SELECT f2.id FROM fit_score f2 WHERE f2.job_group_id = g.gid
  ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1
)
ORDER BY g.gid
"""


def _locations(conn: sqlite3.Connection, job_ids: Iterable[int]) -> dict[int, list[JobLocation]]:
    ids = list(job_ids)
    out: dict[int, list[JobLocation]] = defaultdict(list)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            "SELECT job_id, state, city, is_primary FROM job_locations "
            f"WHERE job_id IN ({','.join('?' * len(chunk))}) ORDER BY is_primary DESC, id",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["job_id"]].append(
                JobLocation(state=r["state"], city=r["city"], is_primary=bool(r["is_primary"]))
            )
    return out


def _salary(row: sqlite3.Row) -> float | None:
    if not row["salary_stated"]:
        return None
    lo, hi = annualize(row["salary_min"], row["salary_max"], row["salary_period"])
    vals = [v for v in (lo, hi) if v]
    return sum(vals) / len(vals) if vals else None


def group_facts(conn: sqlite3.Connection, profile: Profile, since: str) -> list[GroupFact]:
    """Job groups first seen on or after ``since`` with their computed bucket and places."""
    rows = conn.execute(_GROUPS_SQL, {"since": since}).fetchall()
    locs = _locations(conn, (r["job_id"] for r in rows))
    facts: list[GroupFact] = []
    for r in rows:
        job_locs = locs.get(r["job_id"], [])
        bucket = None
        if r["fs_id"] is not None:
            bucket = compute_row(r, r, job_locs, profile).bucket
        facts.append(
            GroupFact(
                group_id=r["gid"],
                job_id=r["job_id"],
                first_seen=r["first_seen"],
                scored=r["fs_id"] is not None,
                bucket=bucket,
                states={loc.state for loc in job_locs if loc.state},
                remote=r["location_scope"] in {s.value for s in REMOTE_SCOPES},
                salary=_salary(r),
            )
        )
    return facts


def _group_places(
    conn: sqlite3.Connection, group_ids: Iterable[int]
) -> dict[int, tuple[set[str], bool]]:
    """group id -> (states of its canonical job, is remote scope)."""
    ids = sorted(set(group_ids))
    if not ids:
        return {}
    remote_scopes = {s.value for s in REMOTE_SCOPES}
    out: dict[int, tuple[set[str], bool]] = {}
    job_of: dict[int, int] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            "SELECT jg.id AS gid, j.id AS job_id, j.location_scope FROM job_group jg "
            "JOIN job j ON j.id = jg.canonical_job_id "
            f"WHERE jg.id IN ({','.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        for r in rows:
            out[r["gid"]] = (set(), r["location_scope"] in remote_scopes)
            job_of[r["gid"]] = r["job_id"]
    locs = _locations(conn, job_of.values())
    for gid, job_id in job_of.items():
        out[gid][0].update(loc.state for loc in locs.get(job_id, []) if loc.state)
    return out


# ─── applications ───────────────────────────────────────────────────────────


@dataclass
class AppFact:
    app_id: int
    group_id: int
    status: str
    applied_on: str | None  # ISO date, None if never applied
    responded: bool
    next_action_at: str | None
    events: list[tuple[str, str]]  # (at, status), oldest first


def application_facts(conn: sqlite3.Connection) -> list[AppFact]:
    newest_first: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for e in conn.execute(
        "SELECT application_id, at, status, note FROM application_event ORDER BY at DESC, id DESC"
    ):
        newest_first[e["application_id"]].append(e)
    out: list[AppFact] = []
    for a in conn.execute("SELECT * FROM application ORDER BY id"):
        # The same events tracking.rebuild_status reads, oldest first.
        evs = [
            (e["at"], e["status"])
            for e in reversed(effective_events(newest_first.get(a["id"], [])))
        ]
        applied = a["applied_at"]
        if applied is None:
            applied = next((at for at, st in evs if st == "applied"), None)
        statuses = {st for _, st in evs} | {a["status"]}
        # No applied date recorded: a status that only follows an application (acknowledged,
        # screening, rejected...) shows it happened, so date it from creation. Withdrawn or
        # closed alone does not: a shortlisted job dropped before applying is not an application.
        if applied is None and statuses & set(APPLIED_EVIDENCE):
            applied = a["created_at"]
        out.append(
            AppFact(
                app_id=a["id"],
                group_id=a["job_group_id"],
                status=evs[-1][1] if evs else a["status"],
                applied_on=applied[:10] if applied else None,
                responded=bool(statuses & set(RESPONSE)),
                next_action_at=a["next_action_at"],
                events=evs,
            )
        )
    return out


def _status_on(app: AppFact, day: str) -> str | None:
    """Latest event status at or before the end of ``day``; current status if no events."""
    if not app.events:
        return app.status if (app.applied_on or "") <= day else None
    current = None
    for at, st in app.events:
        if at[:10] <= day:
            current = st
    return current


# ─── KPIs ───────────────────────────────────────────────────────────────────


def kpis(
    conn: sqlite3.Connection,
    profile: Profile,
    range_days: int,
    now: datetime,
    weekly_cap_usd: float = 10.0,
    buckets: Sequence[str] = DEFAULT_GROUP,
) -> dict[str, Any]:
    """The five KPI tiles with their sparkline series (013 "KPI row").

    The "New ..." tile counts the selected ``buckets`` (default Bullseye + Strong).
    """
    chosen = {Bucket(b) for b in buckets}
    days = _days(now, range_days)
    today = days[-1]

    # New A+B, this range and the one before it (for the delta).
    prev_days = _days(now - timedelta(days=range_days), range_days)
    facts = group_facts(conn, profile, prev_days[0])
    ab_daily = dict.fromkeys(days, 0)
    prev_ab = 0
    for f in facts:
        if f.bucket not in chosen:
            continue
        day = f.first_seen[:10]
        if day in ab_daily:
            ab_daily[day] += 1
        elif day < days[0]:
            prev_ab += 1
    new_ab = sum(ab_daily.values())

    apps = application_facts(conn)
    in_flight = sum(1 for a in apps if a.status in IN_FLIGHT)
    in_flight_series = [sum(1 for a in apps if _status_on(a, d) in IN_FLIGHT) for d in days]

    open_apps = [a for a in apps if a.status not in TERMINAL and a.next_action_at]
    due = sum(1 for a in open_apps if (a.next_action_at or "")[:10] <= today)
    overdue = sum(1 for a in open_apps if (a.next_action_at or "")[:10] < today)

    resp = response_rate(apps, now, RESPONSE_WINDOW_DAYS)
    weeks = max(4, math.ceil(range_days / 7))
    resp_series: list[float | None] = []
    for w in range(weeks - 1, -1, -1):
        end = _today(now) - timedelta(days=7 * w)
        start = end - timedelta(days=6)
        cohort = [
            a for a in apps if a.applied_on and start.isoformat() <= a.applied_on <= end.isoformat()
        ]
        resp_series.append(sum(a.responded for a in cohort) / len(cohort) if cohort else None)

    spend_days = _days(now, max(range_days, 7))
    spend_rows = dict(
        conn.execute(
            "SELECT day, SUM(cost_usd) FROM llm_spend WHERE day >= ? GROUP BY day",
            (spend_days[0],),
        ).fetchall()
    )
    week_spend = sum(spend_rows.get(d, 0.0) or 0.0 for d in spend_days[-7:])
    spend_series = [round(spend_rows.get(d, 0.0) or 0.0, 4) for d in days]

    return {
        "range": range_days,
        "days": days,
        "new_ab": {
            "value": new_ab,
            "previous": prev_ab,
            "delta": new_ab - prev_ab,
            "buckets": list(buckets),
            "series": [ab_daily[d] for d in days],
        },
        "in_flight": {"value": in_flight, "series": in_flight_series},
        "followups": {"due": due, "overdue": overdue},
        "response_rate": {**resp, "series": resp_series},
        "llm_spend": {
            "week_usd": round(week_spend, 2),
            "cap_usd": weekly_cap_usd,
            "over_cap": week_spend > weekly_cap_usd,
            "series": spend_series,
        },
    }


def response_rate(apps: Sequence[AppFact], now: datetime, window_days: int) -> dict[str, Any]:
    since = _since(now, window_days)
    cohort = [a for a in apps if a.applied_on and a.applied_on >= since]
    n = len(cohort)
    k = sum(a.responded for a in cohort)
    return {"numerator": k, "denominator": n, "rate": (k / n) if n else None}


# ─── per-state stats ────────────────────────────────────────────────────────


NLX_KEY = "us-nlx"
NLX_NAME = "National Labor Exchange (NLx)"


@dataclass
class Coverage:
    status: str = "none"
    sources: list[str] = field(default_factory=list)
    last_ok_at: str | None = None
    via_nlx: bool = False


def coverage(conn: sqlite3.Connection, now: datetime) -> dict[str, Coverage]:
    """Coverage status per state, plus REMOTE from the national (state-less) sources.

    A state's own sources decide its status. National aggregators (NLx, USAJOBS) feed the
    REMOTE row. One exception (010 "NLx coverage"): while the national ``us-nlx`` source is
    enabled and ``ok``, NLx carries thousands of postings in robots-blocked states, so a state
    with no working board of its own counts as collected directly, flagged ``via_nlx``.
    A state's own broken or suspect source still wins, because that is a fault to surface.
    """
    rows = conn.execute(
        "SELECT s.key, s.state, s.name, s.family, s.policy, s.status, ss.last_ok_at "
        "FROM source s LEFT JOIN source_state ss ON ss.source_key = s.key "
        "WHERE s.policy != 'disabled' ORDER BY s.name"
    ).fetchall()
    mail_keys = [r["key"] for r in rows if r["family"] in MAIL_FAMILIES]
    recent_mail_states: set[str] = set()
    if mail_keys:
        since = (_today(now) - timedelta(days=EMAIL_STALE_DAYS)).isoformat()
        recent_mail_states = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT l.state FROM job j JOIN job_locations l ON l.job_id = j.id "
                f"WHERE j.source_key IN ({','.join('?' * len(mail_keys))}) "
                "AND j.first_seen_at >= ? AND l.state IS NOT NULL",
                [*mail_keys, since],
            )
        }

    by_state: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["family"] in MAIL_FAMILIES:
            continue
        by_state[r["state"] or REMOTE].append(r)

    nlx = next((r for r in rows if r["key"] == NLX_KEY), None)
    nlx_ok = nlx is not None and nlx["policy"] == "enabled" and nlx["status"] == "ok"

    out: dict[str, Coverage] = {}
    for key in (*geo.US_SUBDIVISIONS, REMOTE):
        srcs = by_state.get(key, [])
        enabled = [r for r in srcs if r["policy"] == "enabled"]
        oks = [r["last_ok_at"] for r in srcs if r["last_ok_at"]]
        cov = Coverage(sources=[r["name"] for r in srcs], last_ok_at=max(oks) if oks else None)
        if any(r["status"] == "broken" for r in enabled):
            cov.status = "critical"
        elif any(r["status"] == "suspect" for r in enabled):
            cov.status = "serious"
        elif any(r["status"] == "ok" for r in enabled):
            cov.status = "direct"
        elif srcs and all(r["policy"] == "blocked" for r in srcs):
            stale = bool(mail_keys) and key not in recent_mail_states
            cov.status = "email_stale" if stale else "email"
        if nlx_ok and key != REMOTE and cov.status in ("none", "email", "email_stale"):
            cov.status = "direct"
            cov.via_nlx = True
            cov.sources = [*cov.sources, NLX_NAME]
            cov.last_ok_at = max(filter(None, [cov.last_ok_at, nlx["last_ok_at"]]), default=None)
        out[key] = cov
    return out


def _empty_row(key: str) -> dict[str, Any]:
    st = geo.by_usps(key)
    return {
        "state": key,
        "name": st.name if st else "Remote (US)",
        "kind": st.kind if st else "remote",
        "fips": st.fips if st else None,
        "new_ab": 0,
        "by_bucket": {},
        "scored": 0,
        "shortlisted": 0,
        "applied": 0,
        "responses": 0,
        "response_rate": None,
        "median_salary": None,
        "col_adjusted": None,
        "applications": 0,
    }


def clamp_buckets(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    """The map's bucket selection from ``?buckets=A,B``; invalid or empty means the default."""
    return tuple(parse_letters(raw)) or DEFAULT_GROUP


def state_stats(
    conn: sqlite3.Connection,
    profile: Profile,
    metric: str,
    range_days: int,
    now: datetime,
    buckets: Sequence[str] = DEFAULT_GROUP,
) -> list[dict[str, Any]]:
    """Per-state rows; see :func:`state_stats_with_totals`."""
    return state_stats_with_totals(conn, profile, metric, range_days, now, buckets)[0]


def state_stats_with_totals(
    conn: sqlite3.Connection,
    profile: Profile,
    metric: str,
    range_days: int,
    now: datetime,
    buckets: Sequence[str] = DEFAULT_GROUP,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One row per subdivision in ``geo.US_SUBDIVISIONS`` plus REMOTE, each with ``value``.

    A job counts in every state it lists (011 display rules). A remote_us / nationwide /
    negotiable job also counts once in REMOTE, never spread across states.

    Every count (new, scored, shortlisted, applied, responses, applications) and the salary
    medians cover only groups in the selected ``buckets``; every row also
    carries ``by_bucket`` (letter -> count for all buckets) so a client can re-slice. The
    second return value is the distinct-group count per bucket (a multi-state job once).
    """
    chosen = {Bucket(b) for b in buckets}
    metric = clamp_metric(metric)
    since = _since(now, range_days)
    rows = {k: _empty_row(k) for k in (*geo.US_SUBDIVISIONS, REMOTE)}

    def places(states: set[str], remote: bool) -> list[str]:
        keys = [s for s in states if s in rows]
        if remote:
            keys.append(REMOTE)
        return keys

    salaries: dict[str, list[float]] = defaultdict(list)
    totals: dict[str, int] = {}
    all_facts = group_facts(conn, profile, "")
    bucket_of = {f.group_id: f.bucket for f in all_facts}
    for f in all_facts:
        if f.first_seen < since:
            continue
        if f.bucket is not None:
            totals[f.bucket.value] = totals.get(f.bucket.value, 0) + 1
        for key in places(f.states, f.remote):
            r = rows[key]
            r["scored"] += f.bucket in chosen
            if f.bucket is not None:
                r["by_bucket"][f.bucket.value] = r["by_bucket"].get(f.bucket.value, 0) + 1
            if f.bucket in chosen:
                r["new_ab"] += 1
                if f.salary is not None:
                    salaries[key].append(f.salary)

    shortlisted = [
        r["job_group_id"]
        for r in conn.execute(
            "SELECT job_group_id FROM label WHERE label = 'interesting' AND labeled_at >= ?",
            (since,),
        )
    ]
    apps = application_facts(conn)
    place_of = _group_places(conn, [*shortlisted, *(a.group_id for a in apps)])
    for gid in shortlisted:
        if bucket_of.get(gid) not in chosen:
            continue
        for key in places(*place_of.get(gid, (set(), False))):
            rows[key]["shortlisted"] += 1
    for a in apps:
        if bucket_of.get(a.group_id) not in chosen:
            continue
        keys = places(*place_of.get(a.group_id, (set(), False)))
        for key in keys:
            rows[key]["applications"] += 1
            if a.applied_on and a.applied_on >= since:
                rows[key]["applied"] += 1
                rows[key]["responses"] += a.responded

    cov = coverage(conn, now)
    for key, r in rows.items():
        if r["applied"] >= RESPONSE_MIN_N:
            r["response_rate"] = r["responses"] / r["applied"]
        if salaries.get(key):
            r["median_salary"] = round(median(salaries[key]))
        r["col_adjusted"] = col_adjust(r["median_salary"], key)
        c = cov[key]
        icon, label = COVERAGE[c.status]
        if c.via_nlx:
            label = f"{label} via NLx"
        r.update(
            via_nlx=c.via_nlx,
            coverage=c.status,
            coverage_icon=icon,
            coverage_label=label,
            sources=c.sources,
            last_ok_at=c.last_ok_at,
        )
        r["value"] = r[metric]
    return list(rows.values()), totals


# ─── class breaks ───────────────────────────────────────────────────────────


def _quantile(sorted_vals: Sequence[float], p: float) -> float:
    """Linear-interpolated quantile (same method as d3.quantile / R type 7)."""
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * p
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def class_breaks(values: Iterable[float | None], kind: str, classes: int = 5) -> list[float]:
    """Upper bounds of up to ``classes`` quantile classes over the positive values.

    Zero is not part of the quantiles (it has its own lightest step) and None is "no data".
    Duplicate bounds collapse, so few distinct values give fewer classes.
    """
    vals = sorted(float(v) for v in values if v is not None and v > 0)
    if not vals:
        return []
    out: list[float] = []
    for i in range(1, classes + 1):
        q = _quantile(vals, i / classes)
        if kind == "count":
            q = float(math.ceil(q))
        elif kind == "money":
            q = float(round(q, -3)) or q
        else:
            q = round(q, 3)
        if not out or q > out[-1]:
            out.append(q)
    return out


def map_payload(
    rows: Sequence[dict[str, Any]],
    metric: str,
    range_days: int,
    buckets: Sequence[str] = DEFAULT_GROUP,
    totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    metric = clamp_metric(metric)
    label, kind = METRICS[metric]
    picked = list(buckets)
    # Every metric covers only the selected buckets, so the label says which.
    label = f"{'New' if metric == 'new_ab' else label}: {group_name(picked)}"
    breaks = class_breaks((r["value"] for r in rows if r["state"] != REMOTE), kind)
    return {
        "metric": metric,
        "label": label,
        "kind": kind,
        "buckets": picked,
        "bucket_label": group_name(picked),
        "bucket_totals": {b: (totals or {}).get(b, 0) for b in BUCKET_TITLES},
        "fit_buckets": list(FIT_GROUP),
        "default_buckets": list(DEFAULT_GROUP),
        "range": range_days,
        "breaks": breaks,
        "states": list(rows),
    }


# ─── table sort ─────────────────────────────────────────────────────────────

_COVERAGE_ORDER = {code: i for i, code in enumerate(COVERAGE)}


def sort_rows(
    rows: Sequence[dict[str, Any]], sort: str | None, direction: str | None
) -> list[dict[str, Any]]:
    """Sort table rows by a column; None values always sort last. Ties break on name."""
    key = sort if sort in SORT_KEYS else "new_ab"
    desc = direction != "asc"
    by_name = sorted(rows, key=lambda r: r["name"])
    if key == "state":
        return list(reversed(by_name)) if desc else by_name

    def value(r: dict[str, Any]) -> Any:
        if key == "coverage":
            return -_COVERAGE_ORDER[r["coverage"]]  # desc = most severe first
        return r[key]

    present = [r for r in by_name if value(r) is not None]
    missing = [r for r in by_name if value(r) is None]
    present.sort(key=value, reverse=desc)
    return present + missing


# ─── chart series (013 "Charts") ────────────────────────────────────────────

FUNNEL_DAYS = 30
FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("found", "Found"),
    ("prefilter", "Passed prefilter"),
    ("ab", "Bullseye + Strong"),  # label follows the selected buckets
    ("shortlisted", "Shortlisted"),
    ("applied", "Applied"),
    ("responded", "Responded"),
    ("interviewed", "Interviewed"),
    ("offer", "Offer"),
)
FUNNEL_ZOOM = 4  # the last four stages also get their own panel
# Bucket-mix slots in fixed order: A, B, C+D+E folded, F. G is hidden by design.
MIX_SLOTS: tuple[tuple[str, str, tuple[Bucket, ...]], ...] = (
    ("a", "Bullseye", (Bucket.A,)),
    ("b", "Strong", (Bucket.B,)),
    ("other", "Other fits", (Bucket.C, Bucket.D, Bucket.E)),
    ("f", "Stale match", (Bucket.F,)),
)
STATUS_ORDER: tuple[str, ...] = (
    "interested",
    "preparing",
    "applied",
    "acknowledged",
    "screening",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
    "no_response",
    "closed",
)
MIN_POINTS = 3


def _funnel(
    conn: sqlite3.Connection,
    profile: Profile,
    now: datetime,
    apps: Sequence[AppFact],
    buckets: Sequence[str] = DEFAULT_GROUP,
) -> list[dict[str, Any]]:
    chosen = {Bucket(b) for b in buckets}
    since = _since(now, FUNNEL_DAYS)
    found, passed = conn.execute(
        "WITH g AS (SELECT job_group_id AS gid FROM job WHERE job_group_id IS NOT NULL "
        "GROUP BY job_group_id HAVING MIN(first_seen_at) >= ?) "
        "SELECT COUNT(*), COALESCE(SUM(j.stage IN ('prefiltered', 'scored', 'triaged')), 0) "
        "FROM g JOIN job_group jg ON jg.id = g.gid JOIN job j ON j.id = jg.canonical_job_id",
        (since,),
    ).fetchone()
    bucket_of = {f.group_id: f.bucket for f in group_facts(conn, profile, "")}
    ab = sum(1 for f in group_facts(conn, profile, since) if f.bucket in chosen)
    # Downstream stages only count groups in the selected buckets, so a narrow selection
    # never shows more applications than matches.
    applied = [
        a
        for a in apps
        if a.applied_on and a.applied_on >= since and bucket_of.get(a.group_id) in chosen
    ]
    liked = {
        r[0]
        for r in conn.execute(
            "SELECT job_group_id FROM label WHERE label = 'interesting' AND labeled_at >= ?",
            (since,),
        )
        if bucket_of.get(r[0]) in chosen
    }
    shortlisted = liked | {a.group_id for a in applied}

    def reached(app: AppFact, statuses: set[str]) -> bool:
        return bool(({st for _, st in app.events} | {app.status}) & statuses)

    counts = [
        found,
        passed,
        ab,
        len(shortlisted),
        len(applied),
        sum(a.responded for a in applied),
        sum(reached(a, {"interview", "offer"}) for a in applied),
        sum(reached(a, {"offer"}) for a in applied),
    ]
    out: list[dict[str, Any]] = []
    for i, ((key, label), n) in enumerate(zip(FUNNEL_STAGES, counts, strict=True)):
        if key == "ab":
            label = group_name(buckets)
        prev = counts[i - 1] if i else 0
        out.append(
            {"key": key, "label": label, "count": n, "pct_prev": (n / prev) if prev else None}
        )
    return out


def series(
    conn: sqlite3.Connection,
    profile: Profile,
    range_days: int,
    now: datetime,
    buckets: Sequence[str] = DEFAULT_GROUP,
) -> dict[str, Any]:
    """Data for the four dashboard charts. Each chart reports ``enough`` (>= 3 points)."""
    days = _days(now, range_days)
    weeks = max(4, math.ceil(range_days / 7))
    week_ends = [_today(now) - timedelta(days=7 * w) for w in range(weeks - 1, -1, -1)]
    first_day = min(days[0], (week_ends[0] - timedelta(days=6)).isoformat())
    facts = [f for f in group_facts(conn, profile, first_day) if f.bucket is not None]

    chosen = {Bucket(b) for b in buckets}
    # New matches (selected buckets) per day, plus the mean of the last 7 days as a reference line.
    daily = dict.fromkeys(days, 0)
    for f in facts:
        if f.bucket in chosen and f.first_seen[:10] in daily:
            daily[f.first_seen[:10]] += 1
    line = [{"day": d, "count": daily[d]} for d in days]
    last7 = [daily[d] for d in days[-7:]]
    mean7 = sum(last7) / len(last7)

    # Bucket mix per week (weeks end today and step back 7 days).
    mix_weeks = []
    for end in week_ends:
        start = (end - timedelta(days=6)).isoformat()
        stop = end.isoformat()
        wk: dict[str, Any] = {"week": stop}
        for slot, _, slot_buckets in MIX_SLOTS:
            wk[slot] = sum(
                1 for f in facts if f.bucket in slot_buckets and start <= f.first_seen[:10] <= stop
            )
        mix_weeks.append(wk)

    funnel = _funnel(conn, profile, now, application_facts(conn), buckets)

    by_status = dict(
        conn.execute("SELECT status, COUNT(*) FROM application GROUP BY status").fetchall()
    )
    status = [{"status": s, "count": by_status.get(s, 0)} for s in STATUS_ORDER]

    return {
        "range": range_days,
        "buckets": list(buckets),
        "bucket_label": group_name(buckets),
        "min_points": MIN_POINTS,
        "line": {
            "points": line,
            "mean7": round(mean7, 3),
            "enough": sum(1 for p in line if p["count"] > 0) >= MIN_POINTS,
        },
        "funnel": {
            "days": FUNNEL_DAYS,
            "stages": funnel,
            "zoom_from": len(funnel) - FUNNEL_ZOOM,
            "enough": sum(1 for s in funnel if s["count"] > 0) >= MIN_POINTS,
        },
        "mix": {
            "slots": [{"key": k, "label": label} for k, label, _ in MIX_SLOTS],
            "weeks": mix_weeks,
            "enough": sum(1 for w in mix_weeks if any(w[k] for k, _, _ in MIX_SLOTS)) >= MIN_POINTS,
        },
        "status": {
            "items": status,
            "enough": sum(1 for s in status if s["count"] > 0) >= MIN_POINTS,
        },
    }


# ─── outcomes Sankey ────────────────────────────────────────────────────────

MANUAL_SOURCE = EMAIL_MANUAL  # proposals.MANUAL_SOURCE: groups made accepting a mail proposal
_UNPREFILTERED = ("listed", "resolved", "normalized", "grouped")
# Stages before a posting is grouped: such a job has no job_group yet, so group_facts never sees it.
_UNGROUPED_STAGES = ("listed", "resolved", "normalized")
# application status -> outcome node. ``acknowledged`` is an automated receipt, so it is still
# awaiting; ``screening`` is a person engaging, so it shares the interview node.
_OUTCOME_NODE = {
    "applied": "awaiting",
    "acknowledged": "awaiting",
    "screening": "interview",
    "interview": "interview",
    "offer": "offer",
    "rejected": "rejected",
    "withdrawn": "closed",
    "closed": "closed",
    "no_response": "no_response",
}
# (id, label, column, kind, href). kind picks the color: flow, win, loss, bad.
# Application nodes link to /pipeline?node=<id>, which lists exactly the cards the node counts;
# bucket nodes to the inbox with already-triaged groups included; Untriaged to every bucket.
_SANKEY_NODES: tuple[tuple[str, str, int, str, str | None], ...] = (
    ("fetched", "Fetched", 0, "flow", None),
    ("elsewhere", "Applied elsewhere", 0, "flow", "/pipeline?node=elsewhere"),
    # Postings pasted on New packet (specs/017) that were scored on request.
    ("pasted", "Pasted", 0, "flow", None),
    ("prefiltered_out", "Prefiltered out", 1, "loss", None),
    ("awaiting_prefilter", "Awaiting prefilter", 1, "loss", None),
    ("passed", "Passed prefilter", 1, "flow", None),
    ("unscored", "Not yet scored", 2, "loss", None),
    *(
        (f"bucket_{b}", title.title(), 2, "flow", f"/inbox?bucket={b}&triaged=1")
        for b, (title, _hint) in BUCKET_TITLES.items()
    ),
    ("untriaged", "Untriaged", 3, "loss", "/inbox?bucket=" + ",".join(BUCKET_TITLES)),
    ("dismissed", "Dismissed", 3, "loss", None),
    ("shortlisted", "Shortlisted", 3, "flow", "/pipeline?node=shortlisted"),
    ("not_applied", "Not applied yet", 4, "loss", "/pipeline?node=not_applied"),
    ("applied", "Applied", 4, "flow", "/pipeline?node=applied"),
    ("awaiting", "Awaiting response", 5, "loss", "/pipeline?node=awaiting"),
    ("interview", "Screening / interview", 5, "win", "/pipeline?node=interview"),
    ("offer", "Offer", 5, "win", "/pipeline?node=offer"),
    ("rejected", "Rejected", 5, "bad", "/pipeline?node=rejected"),
    ("closed", "Withdrawn / closed", 5, "loss", "/pipeline?node=closed"),
    ("no_response", "No response", 5, "loss", "/pipeline?node=no_response"),
)
_SANKEY_ORDER = {n[0]: i for i, n in enumerate(_SANKEY_NODES)}
# Where groups enter the chart: nothing flows into these, so their value is their outflow.
_SOURCE_NODES = ("fetched", "elsewhere", "pasted")
# Nodes whose /pipeline list is the applications in that node (the page filters by these).
PIPELINE_NODES = frozenset(
    {
        "elsewhere",
        "shortlisted",
        "not_applied",
        "applied",
        "awaiting",
        "interview",
        "offer",
        "rejected",
        "closed",
        "no_response",
    }
)
# Nodes whose count also holds rejection emails that matched no application here.
EMAILED_NODES = frozenset({"elsewhere", "applied", "rejected"})


@dataclass
class RejectionClaims:
    """What the employer-rejection table says about job groups and applications.

    ``hidden``: groups for a posting the employer rejected (the inbox leaves them out too).
    ``apps``: application ids that count as rejected whatever their own status says: one per
    rejection, matched by group, else by employer and title. ``emailed``: rejections that
    matched no application of ours; the candidate applied elsewhere and was turned down.
    """

    hidden: set[int] = field(default_factory=set)
    apps: set[int] = field(default_factory=set)
    emailed: int = 0


def rejection_claims(conn: sqlite3.Connection, apps: Sequence[AppFact]) -> RejectionClaims:
    """Pair each rejection with at most one applied application; the rest are ``emailed``.

    A rejection counts once however many groups share its employer and title (a posting in
    several locations is several groups), and an application a rejection email already refers
    to is not counted a second time as "applied elsewhere".
    """
    index = rejections.RejectionIndex.load(conn)
    if not index:
        return RejectionClaims()
    claims = RejectionClaims(hidden=rejections.rejected_group_ids(conn, index))
    applied = [a for a in apps if a.applied_on]
    by_group = {a.group_id: a for a in applied}
    where = {
        r["app_id"]: (rejections.employer_norm(r["emp"]), r["title"])
        for r in conn.execute(
            "SELECT a.id AS app_id, COALESCE(j.employer, j.agency_raw, '') AS emp, j.title "
            "FROM application a JOIN job_group g ON g.id = a.job_group_id "
            "JOIN job j ON j.id = g.canonical_job_id"
        )
    }
    seen: set[object] = set()
    unmatched: list[rejections.Rejection] = []
    # Rejections pinned to a group go first, so an employer-wide match never takes their app.
    for r in sorted(index.rejections, key=lambda r: r.job_group_id is None):
        key = (
            r.job_group_id
            if r.job_group_id is not None
            else (r.employer_norm, rejections.title_norm(r.title))
        )
        if key in seen:
            continue
        seen.add(key)
        if r.job_group_id is not None:
            app = by_group.get(r.job_group_id)
            if app is not None:
                claims.apps.add(app.app_id)
                continue
        else:
            near = [
                a
                for a in applied
                if r.employer_norm
                and where.get(a.app_id, ("", None))[0] == r.employer_norm
                and (not r.title or rejections.same_title(r.title, where[a.app_id][1]))
            ]
            free = [a for a in near if a.app_id not in claims.apps]
            if free:
                # the one already marked rejected is the likely match, then any still open
                free.sort(key=lambda a: (a.status != "rejected", a.status in TERMINAL, a.app_id))
                claims.apps.add(free[0].app_id)
                continue
            if near:
                continue  # another email for an application a rejection already accounts for
        if not any(_same_rejection(r, e) for e in unmatched):
            unmatched.append(r)
            claims.emailed += 1
    return claims


def _same_rejection(a: rejections.Rejection, b: rejections.Rejection) -> bool:
    """Two rejection rows plausibly about one job: same employer, titles agree or one is blank."""
    return (
        bool(a.employer_norm)
        and a.employer_norm == b.employer_norm
        and (not a.title or not b.title or rejections.same_title(a.title, b.title))
    )


@dataclass
class _Walk:
    links: dict[tuple[str, str], int]
    members: dict[str, set[int]]  # node id -> application ids counted there


_NO_FACT = GroupFact(group_id=0, job_id=0, first_seen="", scored=False, bucket=None)


def _walk(conn: sqlite3.Connection, facts: dict[int, GroupFact]) -> _Walk:
    """Send every job group down exactly one path; remember which applications sit on each node.

    ``facts`` only decides the bucket of groups no one has touched; applications never need it,
    so ``pipeline_node_apps`` passes an empty dict.
    """
    apps_all = application_facts(conn)
    apps = {a.group_id: a for a in apps_all}
    claims = rejection_claims(conn, apps_all)
    labels = dict(conn.execute("SELECT job_group_id, label FROM label").fetchall())
    meta = {
        r["gid"]: r
        for r in conn.execute(
            "SELECT jg.id AS gid, j.source_key, j.stage, p.passed "
            "FROM job_group jg JOIN job j ON j.id = jg.canonical_job_id "
            "LEFT JOIN prefilter_result p ON p.job_id = j.id"
        )
    }
    scored_ids = {r[0] for r in conn.execute("SELECT DISTINCT job_group_id FROM fit_score")}
    links: dict[tuple[str, str], int] = defaultdict(int)
    members: dict[str, set[int]] = defaultdict(set)

    def put(app: AppFact | None, node: str) -> None:
        if app is not None:
            members[node].add(app.app_id)

    for gid, m in meta.items():
        fact = facts.get(gid, _NO_FACT)
        app = apps.get(gid)
        label = labels.get(gid)
        applied_here = app is not None and app.applied_on is not None
        if gid in claims.hidden and not applied_here:
            continue  # the rejected posting itself; claims.emailed counts the application
        shortlisted = app is not None or label in ("interesting", "applied")
        if m["source_key"] == MANUAL_SOURCE:
            if app is None:  # a manual group exists only to hold an application
                continue
            came_from = "elsewhere"
        elif m["source_key"] == PASTE_MANUAL:
            # A pasted posting skips fetch and prefilter (the user chose it). Scored, it flows
            # through the buckets like an ingested group; unscored, it is only in the pipeline.
            # Whether it is scored comes from fit_score, not ``facts``: pipeline_node_apps
            # walks with no facts and must list the same applications the chart counts.
            if gid not in scored_ids:
                continue
            scored = f"bucket_{fact.bucket.value}" if fact.bucket else "unscored"
            links[("pasted", scored)] += 1
        else:
            touched = fact.scored or shortlisted or label is not None
            if not touched and m["stage"] in _UNPREFILTERED:
                links[("fetched", "awaiting_prefilter")] += 1
                continue
            if not touched and m["passed"] == 0:
                links[("fetched", "prefiltered_out")] += 1
                continue
            scored = f"bucket_{fact.bucket.value}" if fact.bucket else "unscored"
            links[("fetched", "passed")] += 1
            links[("passed", scored)] += 1
        if m["source_key"] != MANUAL_SOURCE:
            if shortlisted:
                triage = "shortlisted"
            elif label == "not_interesting":
                triage = "dismissed"
            elif scored == "unscored":
                continue  # not scored, so not in the inbox either: it ends at "Not yet scored"
            else:
                triage = "untriaged"
            links[(scored, triage)] += 1
            if not shortlisted:
                continue
            came_from = "shortlisted"
        put(app, came_from)
        if not applied_here:
            links[(came_from, "not_applied")] += 1
            put(app, "not_applied")
            continue
        links[(came_from, "applied")] += 1
        put(app, "applied")
        outcome = "rejected" if app.app_id in claims.apps else _OUTCOME_NODE.get(app.status)
        links[("applied", outcome or "awaiting")] += 1
        put(app, outcome or "awaiting")

    # Postings listed but not grouped yet are in no job_group, so the walk above never saw them.
    waiting = conn.execute(
        "SELECT COUNT(*) FROM job WHERE job_group_id IS NULL AND stage IN (?, ?, ?)",
        _UNGROUPED_STAGES,
    ).fetchone()[0]
    if waiting:
        links[("fetched", "awaiting_prefilter")] += waiting
    if claims.emailed:
        links[("elsewhere", "applied")] += claims.emailed
        links[("applied", "rejected")] += claims.emailed
    return _Walk(links, members)


def node_label(node: str) -> str:
    return next((n[1] for n in _SANKEY_NODES if n[0] == node), node)


def pipeline_node_apps(conn: sqlite3.Connection, node: str) -> set[int]:
    """Application ids the Sankey counts on ``node`` (what /pipeline?node=<id> lists)."""
    return set(_walk(conn, {}).members.get(node, ()))


def emailed_count(conn: sqlite3.Connection) -> int:
    """Rejections with no application here: the part of a Sankey node /pipeline cannot list."""
    return rejection_claims(conn, application_facts(conn)).emailed


def sankey(conn: sqlite3.Connection, profile: Profile) -> dict[str, Any]:
    """Outcome flows over every job group (deduped, not postings), all time.

    Each group takes exactly one path, so flow is conserved: for every node that has outgoing
    links, in == out. A group the employer rejected you for (rejection emails) lands on the
    Rejected node whatever its application status says. A rejection that matches no
    application of ours was applied for elsewhere (the email proves it): it flows Applied
    elsewhere -> Applied -> Rejected, once, however many job groups share that title. Postings
    not grouped yet count as Fetched and Awaiting prefilter. Zero-value nodes and links are
    dropped from the payload.
    """
    links = _walk(conn, {f.group_id: f for f in group_facts(conn, profile, "")}).links
    value: dict[str, int] = defaultdict(int)
    for (a, b), n in links.items():
        value[b] += n
        if a in _SOURCE_NODES:
            value[a] += n
    nodes = [
        {"id": i, "label": label, "col": col, "kind": kind, "href": href, "value": value[i]}
        for i, label, col, kind, href in _SANKEY_NODES
        if value[i] > 0
    ]
    link_rows = [
        {"source": a, "target": b, "value": n}
        for (a, b), n in sorted(
            links.items(), key=lambda kv: tuple(_SANKEY_ORDER[k] for k in kv[0])
        )
    ]
    return {"nodes": nodes, "links": link_rows, "total": sum(value[n] for n in _SOURCE_NODES)}
