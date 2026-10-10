"""Run a re-score now (specs/014 "Re-score now"; consumes ``rescore_request`` rows).

A paid preference change makes groups eligible for scoring again under the new
``scoring_version``. This module plans and runs that work through the existing scoring entry
points (``screen.score_sync`` for synchronous scorers including Jev, ``screen.submit_batch``
for the Anthropic batch, which ``score --collect-pending`` finishes). It does not touch the
eligibility SQL. Scorers and clients are injected, so tests never build a real one.

Single-flight: at most one request is ``running`` (fresh heartbeat) at a time, across
processes. A running row whose heartbeat is older than ``STALE_AFTER`` is treated as dead.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobhunter.config import Scoring
from jobhunter.core import db
from jobhunter.core.models import Bucket
from jobhunter.pipeline.listing import to_iso
from jobhunter.pipeline.locations import load_job_group_locations
from jobhunter.scoring import buckets as bk
from jobhunter.scoring import decisions, prefilter, screen
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import FitScorer, scorer_from_string, split_scorer

logger = logging.getLogger(__name__)

SCOPES = ("recent", "all")
RECENT_DAYS = 14
OPEN_BUCKETS = frozenset({Bucket.A, Bucket.B, Bucket.C, Bucket.D, Bucket.E})
CHUNK = 25
STALE_AFTER = timedelta(minutes=15)
DECISIONS_FALLBACK_COST = 0.00013  # about $0.13 per 1000 jobs for jev:/decisions:
EVERYTHING = 10**9

ScorerFactory = Callable[[str], FitScorer]
ClientFactory = Callable[[], Any]


class RescoreError(Exception):
    """Base for refusals the caller should show the user."""


class RescoreBusy(RescoreError):
    def __init__(self, request_id: int) -> None:
        super().__init__(f"re-score #{request_id} is already running")
        self.request_id = request_id


class RescoreRefused(RescoreError):
    """The plan was not run (over budget, bad scope, request not pending)."""


def is_decisions(spec: str) -> bool:
    return split_scorer(spec)[0] in ("jev", "decisions")


def is_anthropic(spec: str) -> bool:
    return split_scorer(spec)[0] == "anthropic"


# ─── Open scored jobs (shared with the /prefs estimate) ─────────────────────

OPEN_SCORED_SQL = """
SELECT fs.*, j.id AS job_id, j.salary_min, j.salary_max, j.salary_period, j.salary_stated,
       j.location_scope, coalesce(j.posted_at, j.first_seen_at) AS seen_at
FROM fit_score fs
JOIN job_group g ON g.id = fs.job_group_id
JOIN job j ON j.id = g.canonical_job_id
WHERE (j.closes_at IS NULL OR j.closes_at >= ?)
  AND fs.id = (
    SELECT f2.id FROM fit_score f2 WHERE f2.job_group_id = fs.job_group_id
    ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1
  )
"""


def open_scored(conn: sqlite3.Connection, profile: Profile, now: datetime) -> tuple[int, set[int]]:
    """(open scored groups, ids of those seen in ``RECENT_DAYS`` that sit in buckets A-E)."""
    cutoff = (now - timedelta(days=RECENT_DAYS)).isoformat()
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(OPEN_SCORED_SQL, (now.isoformat(),)).fetchall()
    recent: set[int] = set()
    for row in rows:
        if (row["seen_at"] or "") < cutoff:
            continue
        locs = load_job_group_locations(conn, row["job_id"])
        if bk.compute_row(row, row, locs, profile).bucket in OPEN_BUCKETS:
            recent.add(int(row["job_group_id"]))
    return len(rows), recent


# ─── Cost ───────────────────────────────────────────────────────────────────


def cost_per_job(conn: sqlite3.Connection, spec: str) -> tuple[float, str]:
    """Measured screen cost per job for this scorer, else a per-kind default."""
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0), coalesce(sum(calls), 0) FROM llm_spend "
        "WHERE tier = 'screen' AND model = ? AND calls > 0",
        (spec,),
    ).fetchone()
    if row[1] and row[0] > 0:
        return float(row[0]) / float(row[1]), "llm_spend"
    row = conn.execute(
        "SELECT avg(cost_usd), count(*) FROM fit_score WHERE tier = 'screen' AND model = ? "
        "AND cost_usd > 0",
        (spec,),
    ).fetchone()
    if row[1]:
        return float(row[0]), "fit_score"
    if is_decisions(spec):
        return DECISIONS_FALLBACK_COST, "default"
    return screen.ESTIMATED_COST_PER_REQUEST_USD, "default"


def weekly_remaining(conn: sqlite3.Connection, cap_usd: float, now: datetime) -> float:
    since = (now - timedelta(days=6)).date().isoformat()
    spent = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM llm_spend WHERE day >= ?", (since,)
    ).fetchone()[0]
    return cap_usd - spent


def remaining_budget(conn: sqlite3.Connection, scoring: Scoring, now: datetime) -> float:
    """The tighter of the daily and weekly spend caps."""
    return min(
        screen.remaining_daily_budget(conn, scoring.daily_cap_usd, now),
        weekly_remaining(conn, scoring.weekly_cap_usd, now),
    )


# ─── Plan ───────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    scope: str
    scorer: str
    total: int  # groups the run will try, newest first
    waiting: int  # all groups eligible for this scorer under the current profile
    rescored: int  # of those, groups that already have a score (stale, not new)
    cost_per_job: float
    cost_source: str
    estimated_usd: float
    remaining_usd: float  # tighter of daily and weekly cap
    prefilter: dict[str, int] = field(default_factory=dict)
    refusal: str | None = None


def eligible_rows(conn: sqlite3.Connection, profile: Profile, spec: str, limit: int) -> list[Any]:
    if is_decisions(spec):
        return decisions.eligible(conn, profile, spec, limit)
    return screen.eligible_groups(conn, profile, scorer=spec, limit=limit)


def ensure_prefilter(conn: sqlite3.Connection, profile: Profile, now: datetime) -> dict[str, int]:
    """Prefilter jobs with no result for the current ``filter_version`` (free, local).

    A /prefs edit can change filter_version too; eligibility joins prefilter_result on it, so
    without this a re-score would find nothing to do.
    """
    return prefilter.run_prefilter(conn, profile, now=now)


def make_plan(
    conn: sqlite3.Connection,
    profile: Profile,
    scoring: Scoring,
    scope: str,
    spec: str,
    now: datetime,
) -> Plan:
    if scope not in SCOPES:
        raise RescoreRefused(f"unknown scope {scope!r}")
    pre = ensure_prefilter(conn, profile, now)
    ids = [int(r["group_id"]) for r in eligible_rows(conn, profile, spec, EVERYTHING)]
    if scope == "all":
        total = len(ids)
    else:
        _, recent = open_scored(conn, profile, now)
        hits = [i for i, gid in enumerate(ids) if gid in recent]
        # The entry points take a count and work newest first, so cover the last recent group.
        total = hits[-1] + 1 if hits else 0
    scored_before = {
        int(r[0]) for r in conn.execute("SELECT DISTINCT job_group_id FROM fit_score").fetchall()
    }
    per_job, source = cost_per_job(conn, spec)
    est = total * per_job
    remaining = remaining_budget(conn, scoring, now)
    plan = Plan(
        scope=scope,
        scorer=spec,
        total=total,
        waiting=len(ids),
        rescored=sum(1 for i in ids if i in scored_before),
        cost_per_job=per_job,
        cost_source=source,
        estimated_usd=est,
        remaining_usd=remaining,
        prefilter=pre,
    )
    if total and est > remaining:
        plan.refusal = (
            f"estimated ${est:.2f} is over the remaining spend cap (${max(remaining, 0):.2f}); "
            "raise scoring.daily_cap_usd / weekly_cap_usd in config.toml, or pick a smaller scope"
        )
    return plan


# ─── Requests ───────────────────────────────────────────────────────────────


def create_request(conn: sqlite3.Connection, profile: Profile, plan: Plan, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, scorer) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            to_iso(now),
            plan.scope,
            profile.scoring_version,
            plan.total,
            plan.cost_per_job,
            plan.estimated_usd,
            plan.scorer,
        ),
    )
    return int(cur.lastrowid or 0)


def get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rescore_request WHERE id = ?", (request_id,)).fetchone()


def pending_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute("SELECT id FROM rescore_request WHERE status = 'pending' ORDER BY id")
    ]


def running_request(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """The live running request. Rows with a stale heartbeat are marked failed first."""
    conn.execute(
        "UPDATE rescore_request SET status = 'failed', done_at = ?, "
        "error = 'interrupted: no progress for 15 minutes' "
        "WHERE status = 'running' AND coalesce(heartbeat_at, started_at, '') < ?",
        (to_iso(now), to_iso(now - STALE_AFTER)),
    )
    return conn.execute(
        "SELECT * FROM rescore_request WHERE status = 'running' ORDER BY id LIMIT 1"
    ).fetchone()


def cancel_request(conn: sqlite3.Connection, request_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE rescore_request SET status = 'canceled', done_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (to_iso(now), request_id),
    )
    return cur.rowcount > 0


def _claim(conn: sqlite3.Connection, request_id: int, spec: str, total: int, now: datetime) -> None:
    with db.transaction(conn):
        live = running_request(conn, now)
        if live is not None:
            raise RescoreBusy(int(live["id"]))
        cur = conn.execute(
            "UPDATE rescore_request SET status = 'running', scorer = ?, started_at = ?, "
            "heartbeat_at = ?, total = ?, scored = 0, errored = 0, cost_usd = 0, error = NULL, "
            "note = NULL, done_at = NULL WHERE id = ? AND status = 'pending'",
            (spec, to_iso(now), to_iso(now), total, request_id),
        )
        if cur.rowcount == 0:
            raise RescoreRefused(f"re-score #{request_id} is not pending")


@dataclass
class RunState:
    request_id: int
    total: int = 0
    attempted: int = 0
    scored: int = 0
    errored: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        return "; ".join(self.notes) or None


def _save(conn: sqlite3.Connection, st: RunState, now: datetime, **extra: Any) -> None:
    sets = {
        "total": st.total,
        "scored": st.scored,
        "errored": st.errored,
        "cost_usd": st.cost_usd,
        "note": st.note,
        "heartbeat_at": to_iso(now),
        **extra,
    }
    conn.execute(
        f"UPDATE rescore_request SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), st.request_id),
    )


def run_request(
    conn: sqlite3.Connection,
    request_id: int,
    profile: Profile,
    scoring: Scoring,
    *,
    scorer: str | None = None,
    now: Callable[[], datetime],
    scorer_factory: ScorerFactory | None = None,
    client_factory: ClientFactory | None = None,
    chunk: int = CHUNK,
    prefiltered: dict[str, int] | None = None,
) -> RunState:
    """Run one pending request to completion.

    Raises RescoreBusy / RescoreRefused before starting. Once started, failures are recorded
    on the row (``status = 'failed'``, ``error``) and the final state is returned.
    """
    row = get_request(conn, request_id)
    if row is None or row["status"] != "pending":
        raise RescoreRefused(f"re-score #{request_id} is not pending")
    spec = scorer or row["scorer"] or scoring.screen_scorer
    plan = make_plan(conn, profile, scoring, row["scope"], spec, now())
    if plan.refusal:
        raise RescoreRefused(plan.refusal)
    _claim(conn, request_id, spec, plan.total, now())
    st = RunState(request_id, total=plan.total)
    # The caller's own plan (the estimate it showed) already ran the prefilter, so its counts
    # are the ones worth reporting; this run's plan then finds nothing left to evaluate.
    pre = prefiltered if prefiltered and prefiltered.get("evaluated") else plan.prefilter
    if pre.get("evaluated"):
        st.notes.append(f"re-prefiltered {pre['evaluated']} jobs ({pre['passed']} passed)")
    _save(conn, st, now())
    try:
        _execute(conn, st, profile, scoring, spec, plan, now, scorer_factory, client_factory, chunk)
        status, error = "done", None
    except Exception as exc:  # recorded on the row; the caller reads it
        logger.warning("re-score #%d failed: %s", request_id, exc)
        status, error = "failed", str(exc) or type(exc).__name__
    _save(conn, st, now(), status=status, error=error, done_at=to_iso(now()))
    return st


def _execute(
    conn: sqlite3.Connection,
    st: RunState,
    profile: Profile,
    scoring: Scoring,
    spec: str,
    plan: Plan,
    now: Callable[[], datetime],
    scorer_factory: ScorerFactory | None,
    client_factory: ClientFactory | None,
    chunk: int,
) -> None:
    def remaining() -> float:
        return remaining_budget(conn, scoring, now())

    if plan.total == 0:
        st.notes.append("nothing waiting to be scored")
        return
    if is_anthropic(spec):
        if client_factory is None:
            import anthropic

            client_factory = anthropic.Anthropic
        batch_id = screen.submit_batch(
            conn,
            client_factory(),
            profile,
            limit=plan.total,
            now=now(),
            scorer=spec,
            remaining_usd=remaining,
        )
        st.notes.append(
            f"submitted batch {batch_id}; `jobhunter score --collect-pending` collects it"
            if batch_id
            else "nothing submitted (spend cap or nothing eligible)"
        )
        st.attempted = plan.total if batch_id else 0
        return
    make = scorer_factory or (lambda s: scorer_from_string(s, scoring=scoring))
    scorer = make(spec)
    while st.attempted < plan.total:
        res = screen.score_sync(
            conn,
            scorer,
            profile,
            limit=min(chunk, plan.total - st.attempted),
            now=now(),
            remaining_usd=remaining,
        )
        st.attempted += res.submitted
        st.scored += res.written
        st.errored += res.errored + res.invalid
        st.cost_usd += res.cost_usd
        _save(conn, st, now())
        if getattr(res, "capped", False) or res.submitted == 0:
            st.notes.append("stopped at the spend cap")
            break
        if res.written == 0:
            st.notes.append("stopped: a chunk scored nothing")
            break
    if st.errored and st.scored == 0:
        raise RescoreError(f"no job could be scored ({st.errored} errored)")


def drain_pending(
    conn: sqlite3.Connection,
    profile: Profile,
    scoring: Scoring,
    *,
    now: Callable[[], datetime],
    **kwargs: Any,
) -> list[tuple[int, RunState | str]]:
    """Run every pending request in order. A refused one is reported and left pending."""
    out: list[tuple[int, RunState | str]] = []
    for rid in pending_ids(conn):
        try:
            out.append((rid, run_request(conn, rid, profile, scoring, now=now, **kwargs)))
        except RescoreError as exc:
            out.append((rid, f"skipped: {exc}"))
    return out
