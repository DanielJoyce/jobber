"""Run a re-score now (specs/014 "Re-score now"; consumes ``rescore_request`` rows).

A paid preference change makes groups eligible for scoring again under the new
``scoring_version``. This module plans and runs that work through the existing scoring entry
points (``screen.score_sync`` for synchronous scorers including Jev, ``screen.submit_batch``
for the Anthropic batch, which ``score --collect-pending`` finishes). Scorers and clients are
injected, so tests never build a real one.

A run is bounded by its plan: the plan lists the job groups it covers and the most it may
spend, both stored on the request when the user confirms it. The run sends each listed group at
most once (one that errors stays waiting for a later run; it is not re-sent and paid for again
in the same run), never sends a group outside the list, and stops before its own spend would go
past ``max_usd``. A request queued by a /prefs save has no plan until something runs it.

Single-flight: at most one request is ``running`` (fresh heartbeat) at a time, across
processes. A running row whose heartbeat is older than ``STALE_AFTER`` is treated as dead. A
background thread keeps the heartbeat fresh while the run is alive, however slow one chunk is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobhunter.config import Scoring
from jobhunter.core import db
from jobhunter.core.models import Bucket
from jobhunter.pipeline.listing import from_iso, to_iso
from jobhunter.pipeline.locations import load_job_group_locations
from jobhunter.scoring import buckets as bk
from jobhunter.scoring import decisions, prefilter, screen
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import FitScorer, ScorerError, scorer_from_string, split_scorer

logger = logging.getLogger(__name__)

SCOPES = ("recent", "all")
RECENT_DAYS = 14
OPEN_BUCKETS = frozenset({Bucket.A, Bucket.B, Bucket.C, Bucket.D, Bucket.E})
CHUNK = 25
STALE_AFTER = timedelta(minutes=15)
DECISIONS_FALLBACK_COST = 0.00013  # about $0.13 per 1000 jobs for jev:/decisions:
EVERYTHING = 10**9
# The confirmed spend ceiling is the estimate plus this much headroom, rounded up to a cent.
HEADROOM = 1.25
HEADROOM_MIN_USD = 0.01
HEARTBEAT_EVERY_S = 60.0
PREFILTER_BATCH = 200  # jobs per prefilter transaction, so the write lock is held briefly

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


class PlanChanged(RescoreRefused):
    """The plan now differs from the one the user confirmed; show it and ask again."""

    def __init__(self, plan: Plan) -> None:
        super().__init__(
            f"The plan changed since you confirmed it: it is now {plan.total} job groups, "
            f"estimated ${plan.estimated_usd:.2f}. Nothing was run; check the new numbers and "
            "press Re-score now again."
        )
        self.plan = plan


class LostClaim(RescoreError):
    """Another process marked this run interrupted; it must stop sending."""


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


def cost_per_job(
    conn: sqlite3.Connection, spec: str, scoring: Scoring | None = None
) -> tuple[float, str]:
    """Measured screen cost per job for this scorer, else a per-kind default.

    Measured from ``fit_score``, where each row carries that job's share of its request's cost.
    Not from ``llm_spend``: it counts one call per request, and packed and Jev requests carry
    several jobs each (about 8 for Jev), so cost per call is not cost per job.
    """
    row = conn.execute(
        "SELECT avg(cost_usd), count(*) FROM fit_score WHERE tier = 'screen' AND model = ? "
        "AND cost_usd > 0",
        (spec,),
    ).fetchone()
    if row[1]:
        return float(row[0]), "fit_score"
    if is_decisions(spec):
        return DECISIONS_FALLBACK_COST, "default"
    # Nothing measured: the old flat default, or the model's list price at typical tokens
    # when that is higher (Sonnet 5.5 is about $0.005 a job; an OpenRouter slug is priced from
    # the cached catalog in ``scoring``), so a plan is never underpriced. A model with no known
    # price is priced at what the spend cap reserves for it (bug df51dff).
    price = screen.list_price_per_job_usd(spec, scoring, batch=spec.startswith("anthropic:"))
    if price is None:
        price = screen.unpriced_reserve_usd(spec, scoring, batch=spec.startswith("anthropic:"))
    return max(screen.ESTIMATED_COST_PER_REQUEST_USD, price), "default"


def _packs(spec: str, scoring: Scoring | None) -> bool:
    """True when ``spec`` sends several jobs a request (its cap check is per wave, not per job)."""
    provider = spec.partition(":")[0]
    scoring = scoring or Scoring()
    if provider == "openrouter":
        return scoring.openrouter.jobs_per_request > 1
    if provider == "openai-compat":
        return scoring.openai_compat.jobs_per_request > 1
    return False


def reserve_per_job(conn: sqlite3.Connection, spec: str, scoring: Scoring | None = None) -> float:
    """What the spend cap holds per job while a run with ``spec`` is in flight. Read-only.

    The figure the run's own up-front cap check uses (020 M5b, git-bug a322711): the Anthropic
    batch's ``screen.reserve_per_request_usd`` (batch prices), or ``score_sync``'s
    ``screen.reserve_per_job_usd`` for a one-job-a-request scorer. Never below
    ``cost_per_job``. Decisions models (Jev) and packed requests check the cap per request or
    per wave on their own estimate, so for them this is ``cost_per_job``. Reads the cached
    OpenRouter catalog only; never fetches.
    """
    per_job, _ = cost_per_job(conn, spec, scoring)
    if is_decisions(spec) or _packs(spec, scoring):
        return per_job
    held = screen.reserve_per_job_usd(conn, spec, scoring, batch=is_anthropic(spec))
    return max(per_job, held)


def refresh_catalog(
    spec: str, scoring: Scoring, scorer_factory: ScorerFactory | None = None
) -> None:
    """Refresh the cached OpenRouter price catalog (at most daily) before planning a run.

    The plan prices an OpenRouter slug from that cache (``cost_per_job``), and nothing else
    writes it until a scoring run does. Called only where a run is about to be planned and
    paid for (the CLI, ``run_request``); console pages read the cache and never fetch.
    """
    if not spec.startswith("openrouter:"):
        return
    make = scorer_factory or (lambda s: scorer_from_string(s, scoring=scoring))
    try:
        scorer = make(spec)
    except ScorerError as exc:
        logger.warning("price catalog not refreshed: %s", exc)
        return
    refresh = getattr(scorer, "catalog", None)
    if callable(refresh):
        refresh()  # fetch failures are caught and logged inside; the cache stays as it was


# The same caps the daily run checks (screen.remaining_budget: tighter of daily and weekly).
weekly_remaining = screen.weekly_remaining
remaining_budget = screen.remaining_budget


def max_usd_for(estimated_usd: float) -> float:
    """The spend ceiling a confirmed plan carries: the estimate plus headroom, up to a cent."""
    return math.ceil((estimated_usd * HEADROOM + HEADROOM_MIN_USD) * 100 - 1e-9) / 100


# ─── Plan ───────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    scope: str
    scorer: str
    total: int  # groups the run will try (len(group_ids))
    waiting: int  # all groups eligible for this scorer under the current profile
    rescored: int  # of those, groups that already have a score (stale, not new)
    cost_per_job: float
    cost_source: str
    estimated_usd: float
    remaining_usd: float  # tighter of daily and weekly cap
    prefilter: dict[str, int] = field(default_factory=dict)
    refusal: str | None = None
    group_ids: list[int] = field(default_factory=list)  # exactly the groups the run may send
    max_usd: float = 0.0  # the run stops before its own spend would pass this
    scoring_version: str = ""
    # What the spend cap holds per job while the run is in flight (``reserve_per_job``): at
    # least ``cost_per_job``, higher when the cap reserves more than the estimate (Haiku 5.5's
    # $0.002 floor, or a model's table price above its measured mean).
    reserve_per_job: float = 0.0

    @property
    def reserved_usd(self) -> float:
        """What the cap holds for every planned job at once."""
        return self.total * max(self.reserve_per_job, self.cost_per_job)

    @property
    def fits_now(self) -> int:
        """Planned jobs the remaining cap covers at the reservation now (the run sends no more)."""
        per_job = max(self.reserve_per_job, self.cost_per_job)
        if per_job <= 0:
            return self.total
        return min(self.total, math.floor(max(self.remaining_usd, 0.0) / per_job + 1e-9))

    @property
    def reserve_differs(self) -> bool:
        """The cap holds more per job than the estimate (the page says so)."""
        return self.reserve_per_job > self.cost_per_job * (1 + 1e-9)

    @property
    def token(self) -> str:
        """Identifies this exact plan; the confirm form posts it back."""
        blob = json.dumps(
            [self.scope, self.scorer, self.scoring_version, self.group_ids, f"{self.max_usd:.2f}"]
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def batch(self) -> bool:
        return is_anthropic(self.scorer)


def eligible_rows(
    conn: sqlite3.Connection,
    profile: Profile,
    spec: str,
    limit: int,
    group_ids: Sequence[int] | None = None,
) -> list[Any]:
    if is_decisions(spec):
        return decisions.eligible(conn, profile, spec, limit, group_ids=group_ids)
    return screen.eligible_groups(conn, profile, scorer=spec, limit=limit, group_ids=group_ids)


def without_on_request(conn: sqlite3.Connection, group_ids: Sequence[int]) -> list[int]:
    """``group_ids`` minus groups scored only on request (``job_group.score_on_request``).

    A re-score plan carries its own id list and the run passes it as ``:only``, which
    ``screen._ELIGIBLE`` treats as the user naming those groups. A group flagged after the plan
    was built (a capture, a paste) must still never be paid for by a plan (specs/017).
    """
    if not group_ids:
        return []
    flagged = {
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM job_group WHERE score_on_request = 1 "
            "AND id IN (SELECT value FROM json_each(?))",
            (json.dumps([int(g) for g in group_ids]),),
        )
    }
    return [int(g) for g in group_ids if int(g) not in flagged]


def ensure_prefilter(conn: sqlite3.Connection, profile: Profile, now: datetime) -> dict[str, int]:
    """Prefilter jobs with no result for the current ``filter_version`` (free, local).

    A /prefs edit can change filter_version too; eligibility joins prefilter_result on it, so
    without this a re-score would find nothing to do. Works in batches of ``PREFILTER_BATCH``,
    one short write transaction each, so another writer never waits out its busy timeout
    behind a whole re-prefilter. When nothing needs evaluating it writes nothing.
    """
    counts = {"evaluated": 0, "passed": 0, "rejected": 0}
    while True:
        got = prefilter.run_prefilter(conn, profile, now=now, limit=PREFILTER_BATCH)
        for k in counts:
            counts[k] += got.get(k, 0)
        if got.get("evaluated", 0) < PREFILTER_BATCH:
            return counts


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
    ids = without_on_request(
        conn, [int(r["group_id"]) for r in eligible_rows(conn, profile, spec, EVERYTHING)]
    )
    if scope == "all":
        planned = ids
    else:
        # Exactly the recent open A-E groups that are waiting. The run is given these ids, so
        # it never also scores the Mismatch / Stale groups that sit between them.
        _, recent = open_scored(conn, profile, now)
        planned = [gid for gid in ids if gid in recent]
    scored_before = {
        int(r[0]) for r in conn.execute("SELECT DISTINCT job_group_id FROM fit_score").fetchall()
    }
    per_job, source = cost_per_job(conn, spec, scoring)
    est = len(planned) * per_job
    plan = Plan(
        scope=scope,
        scorer=spec,
        total=len(planned),
        waiting=len(ids),
        rescored=sum(1 for i in ids if i in scored_before),
        cost_per_job=per_job,
        cost_source=source,
        estimated_usd=est,
        remaining_usd=remaining_budget(conn, scoring, now),
        prefilter=pre,
        group_ids=planned,
        max_usd=max_usd_for(est),
        scoring_version=profile.scoring_version,
        reserve_per_job=reserve_per_job(conn, spec, scoring),
    )
    _check_cap(plan)
    return plan


def _check_cap(plan: Plan) -> None:
    if plan.total and plan.estimated_usd > plan.remaining_usd:
        plan.refusal = (
            f"estimated ${plan.estimated_usd:.2f} is over the remaining spend cap "
            f"(${max(plan.remaining_usd, 0):.2f}); raise scoring.daily_cap_usd / weekly_cap_usd "
            "in config.toml, or pick a smaller scope"
        )
    elif plan.total and plan.fits_now == 0:
        # The estimate fits, but the cap holds more per job than is left, so the run would
        # send nothing (020 M5b): say so now rather than after a confirm.
        plan.refusal = (
            f"the spend cap holds ${plan.reserve_per_job:.4f} a job while a run is in flight "
            f"and ${max(plan.remaining_usd, 0):.4f} is left, so no job fits; raise "
            "scoring.daily_cap_usd / weekly_cap_usd in config.toml, or wait for the cap to reset"
        )


def stored_plan(
    conn: sqlite3.Connection, row: sqlite3.Row, profile: Profile, scoring: Scoring, now: datetime
) -> Plan:
    """The plan confirmed for this request, as stored on it (``group_ids`` is set)."""
    ids = [int(g) for g in json.loads(row["group_ids"])]
    plan = Plan(
        scope=row["scope"],
        scorer=row["scorer"],
        total=len(ids),
        waiting=len(ids),
        rescored=0,
        cost_per_job=float(row["cost_per_job_usd"]),
        cost_source="confirmed plan",
        estimated_usd=float(row["estimated_usd"]),
        remaining_usd=remaining_budget(conn, scoring, now),
        prefilter=ensure_prefilter(conn, profile, now),
        group_ids=ids,
        max_usd=float(row["max_usd"] or 0.0),
        scoring_version=row["scoring_version"],
        reserve_per_job=reserve_per_job(conn, row["scorer"], scoring),
    )
    _check_cap(plan)
    return plan


# ─── Requests ───────────────────────────────────────────────────────────────


def create_request(conn: sqlite3.Connection, profile: Profile, plan: Plan, now: datetime) -> int:
    """A pending request carrying this confirmed plan (its groups and spend ceiling)."""
    cur = conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, scorer, group_ids, max_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            to_iso(now),
            plan.scope,
            profile.scoring_version,
            plan.total,
            plan.cost_per_job,
            plan.estimated_usd,
            plan.scorer,
            json.dumps(plan.group_ids),
            plan.max_usd,
        ),
    )
    return int(cur.lastrowid or 0)


def attach_plan(conn: sqlite3.Connection, request_id: int, plan: Plan) -> None:
    """Store a plan on a queued (pending) request before it runs."""
    cur = conn.execute(
        "UPDATE rescore_request SET scope = ?, scoring_version = ?, job_count = ?, "
        "cost_per_job_usd = ?, estimated_usd = ?, scorer = ?, group_ids = ?, max_usd = ? "
        "WHERE id = ? AND status = 'pending'",
        (
            plan.scope,
            plan.scoring_version,
            plan.total,
            plan.cost_per_job,
            plan.estimated_usd,
            plan.scorer,
            json.dumps(plan.group_ids),
            plan.max_usd,
            request_id,
        ),
    )
    if cur.rowcount == 0:
        raise RescoreRefused(f"re-score #{request_id} is not pending")


def get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rescore_request WHERE id = ?", (request_id,)).fetchone()


def pending_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute("SELECT id FROM rescore_request WHERE status = 'pending' ORDER BY id")
    ]


def running_request(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """The live running request. Rows with a stale heartbeat are marked failed first.

    Writes only when there is a stale row, so a page that asks on every load stays read-only.
    """
    stale = "WHERE status = 'running' AND coalesce(heartbeat_at, started_at, '') < ?"
    cutoff = to_iso(now - STALE_AFTER)
    if conn.execute(f"SELECT 1 FROM rescore_request {stale} LIMIT 1", (cutoff,)).fetchone():
        conn.execute(
            "UPDATE rescore_request SET status = 'failed', done_at = ?, "
            f"error = 'interrupted: no progress for 15 minutes' {stale}",
            (to_iso(now), cutoff),
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


def supersede_pending(conn: sqlite3.Connection, run: sqlite3.Row, now: datetime) -> list[int]:
    """Close queued requests a finished run already covers, so nothing runs them again.

    A queued request asked for the same work under the profile of its day; running it later
    plans against the current profile anyway and, with the nightly default scorer, could pay a
    second model to score the groups this run just scored. Covered means queued no later than
    this run started, with a scope this run's scope includes ('all' covers 'recent').
    """
    scopes = ("recent", "all") if run["scope"] == "all" else ("recent",)
    started = from_iso(run["started_at"] or run["requested_at"])
    marks = ", ".join("?" for _ in scopes)
    closed: list[int] = []
    for r in conn.execute(
        "SELECT id, requested_at FROM rescore_request WHERE status = 'pending' AND id != ? "
        f"AND scope IN ({marks})",
        (run["id"], *scopes),
    ).fetchall():
        try:
            asked = from_iso(r["requested_at"])
        except ValueError:
            continue
        if asked > started:
            continue
        cur = conn.execute(
            "UPDATE rescore_request SET status = 'canceled', done_at = ?, note = ? "
            "WHERE id = ? AND status = 'pending'",
            (to_iso(now), f"superseded by re-score #{run['id']}", r["id"]),
        )
        if cur.rowcount:
            closed.append(int(r["id"]))
    return closed


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
    attempted: int = 0  # groups sent to the scorer, each at most once
    gone: int = 0  # planned groups no longer waiting when their turn came (scored elsewhere)
    scored: int = 0
    errored: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        return "; ".join(self.notes) or None


def _save(conn: sqlite3.Connection, st: RunState, now: datetime, **extra: Any) -> bool:
    """Write progress, only while this run still holds the row (``status = 'running'``).

    Returns False when it does not: another process marked it interrupted. The run must then
    stop, and its final write must not turn that 'failed' back into 'done'.
    """
    sets = {
        "total": st.total,
        "scored": st.scored,
        "errored": st.errored,
        "cost_usd": st.cost_usd,
        "note": st.note,
        "heartbeat_at": to_iso(now),
        **extra,
    }
    cur = conn.execute(
        f"UPDATE rescore_request SET {', '.join(f'{k} = ?' for k in sets)} "
        "WHERE id = ? AND status = 'running'",
        (*sets.values(), st.request_id),
    )
    return cur.rowcount > 0


def _db_file(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return row[2] or ""
    return ""


class Heartbeat:
    """Bumps ``heartbeat_at`` every ``HEARTBEAT_EVERY_S`` on its own connection while a run is
    alive, so a chunk slower than ``STALE_AFTER`` (a local model) is not taken for dead.

    An in-memory database cannot be opened by a second connection; it gets no thread.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        request_id: int,
        now: Callable[[], datetime],
        table: str = "rescore_request",
    ):
        if table not in ("rescore_request", "deep_request"):
            raise ValueError(f"no heartbeat for {table!r}")
        self._table = table
        self._path = _db_file(conn)
        self._id = request_id
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Heartbeat:
        if self._path:
            self._thread = threading.Thread(
                target=self._run, name=f"rescore-heartbeat-{self._id}", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)

    def _run(self) -> None:
        hb = db.connect(self._path)
        try:
            while not self._stop.wait(HEARTBEAT_EVERY_S):
                try:
                    hb.execute(
                        f"UPDATE {self._table} SET heartbeat_at = ? "
                        "WHERE id = ? AND status = 'running'",
                        (to_iso(self._now()), self._id),
                    )
                except sqlite3.Error as exc:  # busy: the next beat tries again
                    logger.debug("re-score heartbeat: %s", exc)
        finally:
            hb.close()


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

    A request carrying a confirmed plan runs exactly that plan with the scorer it was priced
    for. One with no plan yet (queued by a /prefs save) is planned now and the plan is stored.
    Raises RescoreBusy / RescoreRefused before starting. Once started, failures are recorded
    on the row (``status = 'failed'``, ``error``) and the final state is returned. A run that
    ends 'done' closes the queued requests it covers (``supersede_pending``).
    """
    row = get_request(conn, request_id)
    if row is None or row["status"] != "pending":
        raise RescoreRefused(f"re-score #{request_id} is not pending")
    planned = row["group_ids"] is not None
    # One that follows the configured scorer, which an invalid models.toml may block, is
    # skipped and left pending rather than run on another provider (specs/020 Storage). A
    # request that carries its own scorer (confirmed, or kept on the old model) still runs.
    follows = not scorer and not row["scorer"]
    if follows and (problem := scoring.scorer_problems().get("screen_scorer")):
        raise RescoreRefused(f"{problem}; the re-score stays queued")
    if planned:
        spec = row["scorer"] or scoring.screen_scorer
        if scorer and scorer != spec:
            raise RescoreRefused(
                f"re-score #{request_id} was confirmed for {spec}, not {scorer}; plan it again"
            )
        plan = stored_plan(conn, row, profile, scoring, now())
    else:
        spec = scorer or row["scorer"] or scoring.screen_scorer
        refresh_catalog(spec, scoring, scorer_factory)
        plan = make_plan(conn, profile, scoring, row["scope"], spec, now())
    if plan.refusal:
        raise RescoreRefused(plan.refusal)
    if not planned:
        attach_plan(conn, request_id, plan)
    _claim(conn, request_id, spec, plan.total, now())
    st = RunState(request_id, total=plan.total)
    # The caller's own plan (the estimate it showed) already ran the prefilter, so its counts
    # are the ones worth reporting; this run's plan then finds nothing left to evaluate.
    pre = prefiltered if prefiltered and prefiltered.get("evaluated") else plan.prefilter
    if pre.get("evaluated"):
        st.notes.append(f"re-prefiltered {pre['evaluated']} jobs ({pre['passed']} passed)")
    _save(conn, st, now())
    with Heartbeat(conn, request_id, now):
        try:
            _execute(
                conn, st, profile, scoring, spec, plan, now, scorer_factory, client_factory, chunk
            )
            status, error = "done", None
        except Exception as exc:  # recorded on the row; the caller reads it
            logger.warning("re-score #%d failed: %s", request_id, exc)
            status, error = "failed", str(exc) or type(exc).__name__
    if not _save(conn, st, now(), status=status, error=error, done_at=to_iso(now())):
        # Marked interrupted meanwhile: keep that status, but record what this run did.
        conn.execute(
            "UPDATE rescore_request SET scored = ?, errored = ?, cost_usd = ?, note = ? "
            "WHERE id = ?",
            (st.scored, st.errored, st.cost_usd, st.note, request_id),
        )
    elif status == "done" and (done := get_request(conn, request_id)) is not None:
        supersede_pending(conn, done, now())
    return st


def _priced_on_flat_default(plan: Plan) -> bool:
    """True when ``plan`` was priced at the old flat default rather than measured history.

    A stored plan does not record where its price came from, so the flat
    ``screen.ESTIMATED_COST_PER_REQUEST_USD`` itself is the marker; a measured mean is
    never exactly that. A default priced since per-model pricing is already at least the
    table price, so treating it the same changes nothing.
    """
    if plan.cost_source == "fit_score":
        return False
    return math.isclose(plan.cost_per_job, screen.ESTIMATED_COST_PER_REQUEST_USD, rel_tol=1e-9)


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

    ids = without_on_request(conn, plan.group_ids)
    st.gone += len(plan.group_ids) - len(ids)
    if not ids:
        st.notes.append("nothing waiting to be scored")
        return
    if is_anthropic(spec):
        if client_factory is None:
            import anthropic

            client_factory = anthropic.Anthropic
        # The confirmed ceiling: the batch is billed only when collected, so cut it now to
        # what max_usd covers at the planned cost per job. The daily and weekly caps are
        # applied inside submit_batch as usual.
        per_job = plan.cost_per_job
        if _priced_on_flat_default(plan):
            # Confirmed before per-model pricing, at the flat $0.002: price it at the model's
            # table price when that is higher (Sonnet 5.5). A plan priced on measured history
            # keeps its measured price, so it sends every group the user confirmed.
            per_job = max(per_job, screen.table_price_per_request_usd(spec))
        fits = len(ids) if per_job <= 0 else math.floor(plan.max_usd / per_job + 1e-9)
        if fits <= 0:
            st.notes.append(f"nothing submitted: ${plan.max_usd:.2f} confirmed covers no job")
            return
        if fits < len(ids):
            st.notes.append(
                f"batch cut to {fits} of {len(ids)} groups to stay within the confirmed "
                f"${plan.max_usd:.2f}"
            )
        batch_id = screen.submit_batch(
            conn,
            client_factory(),
            profile,
            limit=min(len(ids), fits),
            now=now(),
            scorer=spec,
            remaining_usd=remaining,
            group_ids=ids,
            rejection_days=scoring.employer_rejection_days,
        )
        st.notes.append(
            f"submitted batch {batch_id}; `jobhunter score --collect-pending` collects it"
            if batch_id
            else "nothing submitted (spend cap or nothing eligible)"
        )
        sent = (
            conn.execute("SELECT request_count FROM score_batch WHERE id = ?", (batch_id,))
            if batch_id
            else None
        )
        got = sent.fetchone() if sent is not None else None
        st.attempted = int(got[0]) if got else (min(len(ids), fits) if batch_id else 0)
        return
    make = scorer_factory or (lambda s: scorer_from_string(s, scoring=scoring))
    scorer = make(spec)
    pos = 0
    stop: str | None = None
    while pos < len(ids):
        batch = ids[pos : pos + max(chunk, 1)]
        # The confirmed ceiling: price the next chunk at the higher of the planned and the
        # measured cost per job, and send only what fits under max_usd.
        per_job = max(plan.cost_per_job, st.cost_usd / st.attempted if st.attempted else 0.0)
        room = plan.max_usd - st.cost_usd
        fits = len(batch) if per_job <= 0 else int(room // per_job + 1e-9)
        if fits <= 0:
            stop = f"stopped at the confirmed spend of ${plan.max_usd:.2f}"
            break
        batch = batch[:fits]
        waiting = len(eligible_rows(conn, profile, spec, len(batch), group_ids=batch))
        pos += len(batch)
        st.gone += len(batch) - waiting
        if waiting == 0:
            continue
        res = screen.score_sync(
            conn,
            scorer,
            profile,
            limit=len(batch),
            now=now(),
            remaining_usd=remaining,
            group_ids=batch,
            rejection_days=scoring.employer_rejection_days,
        )
        st.attempted += res.submitted
        st.scored += res.written
        st.errored += res.errored + res.invalid
        st.cost_usd += res.cost_usd
        if not _save(conn, st, now()):
            raise LostClaim("stopped: another process marked this run interrupted")
        if getattr(res, "capped", False) or res.submitted < waiting:
            stop = "stopped at the spend cap"
            break
        if res.written == 0:
            stop = f"stopped: a chunk of {res.submitted} scored nothing"
            break
    if stop:
        st.notes.append(stop)
    not_sent = len(plan.group_ids) - st.attempted - st.gone
    if not_sent > 0:
        st.notes.append(f"{not_sent} of {len(plan.group_ids)} planned groups were not sent")
    if st.errored:
        st.notes.append(
            f"{st.errored} errored or came back unusable; each was sent once and stays waiting"
        )
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
    """Run every pending request in order. A refused one is reported and left pending; one
    an earlier run in the drain superseded is reported and skipped."""
    out: list[tuple[int, RunState | str]] = []
    for rid in pending_ids(conn):
        row = get_request(conn, rid)
        if row is None or row["status"] != "pending":
            out.append((rid, f"skipped: {row['note'] if row else 'gone'}"))
            continue
        try:
            out.append((rid, run_request(conn, rid, profile, scoring, now=now, **kwargs)))
        except RescoreError as exc:
            out.append((rid, f"skipped: {exc}"))
    return out
