"""``jobhunter backfill``: one-time historical ingest and screen within a budget.

Specs: 006 "Cost" (about 50,000 historical jobs is about $100, run once) and 004 (watermarks).

Two halves. Ingest runs the list → prefilter stages over the last N days and ignores
watermarks; watermarks still only move forward. Screening then prices every eligible group
(prefilter pass, no fit_score for the current scorer, prompt and scoring versions) and submits
them in chunks. ``budget_usd`` is this command's own ceiling and replaces the daily cap for
this command only. Nothing is submitted until ``confirm`` returns True.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from jobhunter.config import Settings
from jobhunter.core import rejections
from jobhunter.core.models import SourceRow
from jobhunter.core.spend import SCORING_ONLY
from jobhunter.pipeline import runner
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import screen
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import FitScorer
from jobhunter.sources.adapters.nlx import DEFAULT_MAX_PAGES as NLX_MAX_PAGES
from jobhunter.sources.adapters.usajobs import MAX_DATE_POSTED_DAYS as USAJOBS_MAX_DAYS

INGEST_STAGES = tuple(s for s in runner.STAGES if s != "score")
DEFAULT_CHUNK_SIZE = 5000
DEFAULT_MAX_RESOLVE = 5000
DEFAULT_POLL_SECONDS = 60.0
DEFAULT_MAX_WAIT_SECONDS = 4 * 3600
# Used only when a scorer has no price of its own (no measured history, no price table).
FALLBACK_COST_PER_JOB_USD = screen.ESTIMATED_COST_PER_REQUEST_USD  # Haiku, batched
# A typical screen call (specs/006 "Cost"): 2,000 uncached input, 3,000 cached, 350 output.
TYPICAL_USAGE = SimpleNamespace(
    input_tokens=2000,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=3000,
    output_tokens=350,
)

# Same conditions as screen._ELIGIBLE, counted rather than loaded.
_COUNT_ELIGIBLE = """
SELECT COUNT(*)
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
JOIN prefilter_result p ON p.job_id = j.id AND p.passed = 1 AND p.filter_version = :filter_version
WHERE NOT EXISTS (
    SELECT 1 FROM fit_score f
    WHERE f.job_group_id = g.id AND f.tier = :tier AND f.model = :model
      AND f.prompt_version = :prompt_version AND f.scoring_version = :scoring_version)
  AND NOT EXISTS (
    SELECT 1 FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id
    WHERE i.job_group_id = g.id AND b.collected_at IS NULL AND b.tier = :tier
      AND b.model = :model AND b.prompt_version = :prompt_version
      AND b.scoring_version = :scoring_version)
  AND g.id NOT IN (SELECT value FROM json_each(:rejected))
"""

_RESOLVE_BACKLOG = """
SELECT COUNT(*) FROM job
WHERE stage = 'listed' AND needs_resolve = 1
  AND (description_raw IS NULL OR description_raw = '')
  AND (closes_at IS NULL OR closes_at > ?)
"""


@dataclass(frozen=True)
class PerJobCost:
    usd: float
    source: str


@dataclass(frozen=True)
class Estimate:
    eligible: int
    per_job: PerJobCost
    spent_today_usd: float
    pending_jobs: int
    budget_usd: float

    @property
    def pending_usd(self) -> float:
        return self.pending_jobs * self.per_job.usd

    @property
    def committed_usd(self) -> float:
        """Spend already on the books today plus the estimate for uncollected batches."""
        return self.spent_today_usd + self.pending_usd

    @property
    def cost_usd(self) -> float:
        return self.eligible * self.per_job.usd

    @property
    def fundable(self) -> int:
        """Jobs the budget still covers, before any submission in this run."""
        return _affordable(self.budget_usd, self.committed_usd, self.per_job.usd, self.eligible)


@dataclass
class BackfillResult:
    status: str = "ok"  # ok | nothing | dry_run | declined | budget_exhausted | no_profile
    submitted: int = 0
    batch_ids: list[str] = field(default_factory=list)
    uncollected: list[str] = field(default_factory=list)
    committed_usd: float = 0.0

    @property
    def exit_code(self) -> int:
        return {"no_profile": 1, "declined": 2}.get(self.status, 0)


def _affordable(budget: float, committed: float, per_job: float, cap: int) -> int:
    if per_job <= 0:
        return cap
    return max(0, math.floor((budget - committed) / per_job + 1e-9))


# ─── Ingest ─────────────────────────────────────────────────────────────────


def window_notes(rows: list[SourceRow], states: set[str] | None, days: int) -> list[str]:
    """Adapter limits that make the window shorter than asked, printed rather than hidden."""
    notes: list[str] = []
    selected = runner.filter_sources(rows, states)
    families = {r.family for r in selected}
    if "usajobs" in families and days > USAJOBS_MAX_DAYS:
        notes.append(
            f"usajobs caps DatePosted at {USAJOBS_MAX_DAYS} days; postings older than that "
            f"are not reachable (asked for {days})"
        )
    for src in selected:
        if src.family == "nlx":
            pages = src.pagination.get("max_pages", NLX_MAX_PAGES)
            notes.append(
                f"nlx {src.key}: paging stops at max_pages={pages} per query; "
                "older postings beyond that page are not reached"
            )
    return notes


def ingest(
    conn: sqlite3.Connection,
    settings: Settings,
    rows: list[SourceRow],
    *,
    days: int,
    states: set[str] | None,
    profile_loader: runner.ProfileLoader,
    max_resolve: int,
    now: datetime,
    adapters: Mapping[str, Callable[[], Any]] | None = None,
    ctx_factory: runner.CtxFactory | None = None,
) -> runner.RunReport:
    """list → prefilter with ``since = now - days``. Watermarks are not consulted."""
    return runner.run_pipeline(
        conn,
        settings,
        rows,
        adapters=adapters,
        ctx_factory=ctx_factory,
        profile_loader=profile_loader,
        stages=list(INGEST_STAGES),
        states=states,
        since=now - timedelta(days=days),
        max_resolve=max_resolve,
        now=now,
    )


def resolve_backlog(conn: sqlite3.Connection, now: datetime) -> int:
    return conn.execute(_RESOLVE_BACKLOG, (to_iso(now),)).fetchone()[0]


# ─── Estimate ───────────────────────────────────────────────────────────────


def per_job_cost(conn: sqlite3.Connection, scorer: FitScorer) -> PerJobCost:
    """Measured history first, then the scorer's own price, then the $0.002 default."""
    count, avg = conn.execute(
        "SELECT COUNT(*), AVG(cost_usd) FROM fit_score "
        "WHERE model = ? AND tier = 'screen' AND cost_usd > 0",
        (scorer.name,),
    ).fetchone()
    if count:
        return PerJobCost(float(avg), f"measured over {count} screened jobs")
    try:
        price = float(scorer.cost(TYPICAL_USAGE, batch=scorer.supports_batching))
    except Exception:  # an unpriced or misconfigured scorer must not stop the estimate
        price = 0.0
    if price > 0:
        return PerJobCost(price, "scorer price at typical tokens")
    return PerJobCost(FALLBACK_COST_PER_JOB_USD, "default; scorer price unknown")


def estimate(
    conn: sqlite3.Connection,
    profile: Profile,
    scorer: FitScorer,
    *,
    budget_usd: float,
    now: datetime,
) -> Estimate:
    params = {
        "filter_version": profile.filter_version,
        "tier": screen.TIER,
        "model": scorer.name,
        "prompt_version": screen.PROMPT_VERSION,
        "scoring_version": profile.scoring_version,
        "rejected": rejections.rejected_json(conn),
    }
    eligible = conn.execute(_COUNT_ELIGIBLE, params).fetchone()[0]
    spent = conn.execute(
        f"SELECT coalesce(sum(cost_usd), 0) FROM llm_spend WHERE day = ? AND {SCORING_ONLY}",
        (to_iso(now)[:10],),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT coalesce(sum(request_count), 0) FROM score_batch WHERE collected_at IS NULL"
    ).fetchone()[0]
    return Estimate(
        eligible=eligible,
        per_job=per_job_cost(conn, scorer),
        spent_today_usd=float(spent),
        pending_jobs=int(pending),
        budget_usd=budget_usd,
    )


# ─── Submit and collect ─────────────────────────────────────────────────────


def _submit(
    conn: sqlite3.Connection,
    profile: Profile,
    scorer: FitScorer,
    client: Any,
    est: Estimate,
    *,
    chunk_size: int,
    now: datetime,
    out: Callable[[str], None],
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> tuple[list[str], int, float, str]:
    """Submit chunks until the budget or the eligible set runs out. Returns what was done."""
    per = est.per_job.usd
    committed = est.committed_usd
    batch_ids: list[str] = []
    submitted = 0
    stop = "all eligible groups submitted"
    while True:
        affordable = _affordable(est.budget_usd, committed, per, chunk_size)
        if affordable <= 0:
            stop = "budget reached; the rest waits for the next run"
            break
        limit = min(chunk_size, affordable)
        if scorer.supports_batching:
            batch_id = screen.submit_batch(
                conn,
                client,
                profile,
                limit=limit,
                now=now,
                scorer=scorer.name,
                rejection_days=rejection_days,
            )
            if batch_id is None:
                break
            n = conn.execute(
                "SELECT request_count FROM score_batch WHERE id = ?", (batch_id,)
            ).fetchone()[0]
            batch_ids.append(batch_id)
            out(f"  batch {batch_id}: {n} jobs submitted")
        else:
            res = screen.score_sync(
                conn, scorer, profile, limit=limit, now=now, rejection_days=rejection_days
            )
            n = res.submitted
            if n == 0:
                break
            out(
                f"  chunk: {n} jobs, {res.written} scored, {res.errored} errored, "
                f"{res.invalid} invalid"
            )
            submitted += n
            committed += n * per
            if res.written == 0:
                stop = "a chunk wrote no scores; stopped"
                break
            out(f"  running estimate ${committed:.4f} of ${est.budget_usd:.2f}")
            continue
        submitted += n
        committed += n * per
        out(f"  running estimate ${committed:.4f} of ${est.budget_usd:.2f}")
    return batch_ids, submitted, committed, stop


def _collect(
    conn: sqlite3.Connection,
    client: Any,
    profile: Profile,
    batch_ids: list[str],
    *,
    max_wait_s: float,
    poll_s: float,
    out: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> list[str]:
    """Poll until every batch ends or ``max_wait_s`` passes. Returns batches still open."""
    pending = list(batch_ids)
    deadline = clock() + max_wait_s
    while pending:
        for batch_id in list(pending):
            res = screen.collect_batch(conn, client, batch_id, profile, now=datetime.now(UTC))
            if res.status in ("ended", "already_collected"):
                pending.remove(batch_id)
                out(
                    f"  collected {batch_id}: {res.written} written, {res.invalid} invalid, "
                    f"{res.errored} errored, ${res.cost_usd:.4f}"
                )
        if not pending:
            break
        if clock() >= deadline:
            out(f"  stopped waiting after {max_wait_s:.0f}s; {len(pending)} batch(es) open")
            break
        out(f"  {len(pending)} batch(es) still processing; checking again in {poll_s:.0f}s")
        sleep(poll_s)
    return pending


# ─── Orchestration ──────────────────────────────────────────────────────────


def _echo_estimate(out: Callable[[str], None], est: Estimate, settings: Settings) -> None:
    out(
        f"Estimate: {est.eligible} eligible groups x ${est.per_job.usd:.5f}/job "
        f"({est.per_job.source}) = ${est.cost_usd:.2f}"
    )
    out(
        f"Budget: ${est.budget_usd:.2f} for this backfill; already committed today "
        f"${est.committed_usd:.2f} (spent ${est.spent_today_usd:.2f}, uncollected batches "
        f"${est.pending_usd:.2f}); covers {est.fundable} more jobs"
    )
    out(
        f"Daily cap bypassed: this command runs under its own budget, not "
        f"scoring.daily_cap_usd (${settings.scoring.daily_cap_usd:.2f})."
    )


def run_backfill(
    conn: sqlite3.Connection,
    settings: Settings,
    rows: list[SourceRow],
    *,
    scorer: FitScorer,
    client: Any = None,
    days: int,
    states: set[str] | None = None,
    budget_usd: float,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_resolve: int = DEFAULT_MAX_RESOLVE,
    dry_run: bool = False,
    confirm: Callable[[Estimate], bool] = lambda est: False,
    wait: bool = False,
    max_wait_s: float = DEFAULT_MAX_WAIT_SECONDS,
    poll_s: float = DEFAULT_POLL_SECONDS,
    profile_loader: runner.ProfileLoader | None = None,
    adapters: Mapping[str, Callable[[], Any]] | None = None,
    ctx_factory: runner.CtxFactory | None = None,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BackfillResult:
    """Ingest, estimate, confirm, then submit within ``budget_usd`` and optionally wait.

    ``confirm`` is the only gate between the estimate and any money being spent; it defaults
    to refusing. A batching scorer needs ``client``. A non-batching scorer is screened
    synchronously, chunk by chunk.
    """
    if budget_usd < 0:
        raise ValueError("budget_usd must not be negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if scorer.supports_batching and client is None:
        raise ValueError(f"scorer {scorer.name} batches; an Anthropic client is required")
    now = now or datetime.now(UTC)

    loader = profile_loader or runner.default_profile_loader(settings)
    profile = loader()
    if profile is None:
        out("backfill: no profile; create profile/preferences.yaml")
        return BackfillResult(status="no_profile")

    since = now - timedelta(days=days)
    out(
        f"Ingest: postings listed since {since:%Y-%m-%d} ({days} days); watermarks are "
        "ignored and only move forward."
    )
    for note in window_notes(rows, states, days):
        out(f"  note: {note}")
    report = ingest(
        conn,
        settings,
        rows,
        days=days,
        states=states,
        profile_loader=lambda: profile,
        max_resolve=max_resolve,
        now=now,
        adapters=adapters,
        ctx_factory=ctx_factory,
    )
    for stage in INGEST_STAGES:
        if stage in report.counts:
            out(f"  {stage}: {report.counts[stage]}")
    for msg in report.messages:
        out(f"  {msg}")
    if report.non_ok:
        out(runner.format_summary(report))
    backlog = resolve_backlog(conn, now)
    if backlog:
        out(f"  resolve backlog: {backlog} postings still lack descriptions; re-run to continue")

    est = estimate(conn, profile, scorer, budget_usd=budget_usd, now=now)
    _echo_estimate(out, est, settings)
    if est.eligible == 0:
        out("Nothing to screen: every eligible group already has a score or an open batch.")
        return BackfillResult(status="nothing")
    if dry_run:
        out("Dry run: ingest ran; nothing submitted.")
        return BackfillResult(status="dry_run")
    if not confirm(est):
        out("Not confirmed; nothing submitted. Pass --yes to submit without the prompt.")
        return BackfillResult(status="declined")
    if est.fundable == 0:
        out("Budget exhausted: committed spend already reaches the budget; nothing submitted.")
        return BackfillResult(status="budget_exhausted", committed_usd=est.committed_usd)

    batch_ids, submitted, committed, stop = _submit(
        conn,
        profile,
        scorer,
        client,
        est,
        chunk_size=chunk_size,
        now=now,
        out=out,
        rejection_days=settings.scoring.employer_rejection_days,
    )
    out(f"Submitted {submitted} jobs in {len(batch_ids)} batch(es): {stop}.")
    result = BackfillResult(
        status="ok",
        submitted=submitted,
        batch_ids=batch_ids,
        committed_usd=committed,
    )
    if not batch_ids:
        return result
    if wait:
        result.uncollected = _collect(
            conn,
            client,
            profile,
            batch_ids,
            max_wait_s=max_wait_s,
            poll_s=poll_s,
            out=out,
            sleep=sleep,
            clock=clock,
        )
    else:
        result.uncollected = list(batch_ids)
        out("Collect later with: jobhunter score --collect-pending")
    return result
