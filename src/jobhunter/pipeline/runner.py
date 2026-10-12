"""Pipeline runner: ``jobhunter run`` (specs/004-ingest-pipeline.md).

Stages run in a fixed order; each source is isolated so one broken source never aborts a
run. Everything that touches the world is injectable (registry, adapter map, fetch context
factory, profile loader) so tests need no network.

List and resolve fetch up to ``fetch.parallel_sources`` sources at once (bug 50320dc, see
``pipeline/fanout.py``). FetchContext keeps every per-host limit, Crawl-delay, Retry-After and
the global cap across threads; database use is serialized on the connection's lock, and the
list stage writes each source's stubs on this thread in plan order, so the rows match a serial
run. Adapters that use the database themselves run alone, after the others.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunter import logsetup
from jobhunter.config import Settings, resolve_path
from jobhunter.core.db import connection_lock
from jobhunter.core.fetch import (
    AccessDenied,
    FetchContext,
    RobotsDisallowed,
    SourceBlocked,
    TransientFetchError,
)
from jobhunter.core.models import JobStub, Policy, Query, SourceRow
from jobhunter.pipeline.dedupe import group_pending
from jobhunter.pipeline.dedupe_url import merge_by_apply_url
from jobhunter.pipeline.dedupe_xstate import merge_cross_state
from jobhunter.pipeline.fanout import Outcome, Stopper, run_ordered
from jobhunter.pipeline.listing import (
    compute_watermark,
    from_iso,
    record_run_watermark,
    to_iso,
    upsert_stubs,
)
from jobhunter.pipeline.normalize import normalize_pending
from jobhunter.scoring.prefilter import run_prefilter
from jobhunter.scoring.profile import Profile, ProfileError, load_profile_for
from jobhunter.sources.adapters.base import SourceAdapter
from jobhunter.sources.adapters.htmlconfig import HtmlConfigAdapter
from jobhunter.sources.adapters.mailalerts import MailAlertsAdapter
from jobhunter.sources.adapters.nlx import NlxAdapter
from jobhunter.sources.adapters.usajobs import UsajobsAdapter, queries_from_profile
from jobhunter.sources.adapters.wyoming import WyomingAdapter
from jobhunter.sources.registry import sync_sources_table

logger = logging.getLogger(__name__)

STAGES = ("list", "resolve", "normalize", "dedupe", "prefilter", "score", "apply_links")
NEEDS_PROFILE = ("prefilter", "score")
DEFAULT_MAX_RESOLVE = 1500
FULL_LOOKBACK_DAYS = 60
DEFAULT_SCORE_LIMIT = 2000
FLUSH_EVERY = 100
PROGRESS_EVERY = 100  # resolve: a progress line every N postings or PROGRESS_SECONDS
PROGRESS_SECONDS = 30.0

# Extension point: add a family -> adapter class here as adapters land (specs/003).
ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {
    "usajobs": UsajobsAdapter,
    "nlx": NlxAdapter,
    "wyo": WyomingAdapter,
    "htmlconfig": HtmlConfigAdapter,
    "mailalerts": MailAlertsAdapter,
}

CtxFactory = Callable[[SourceRow], AbstractContextManager[Any]]
ProfileLoader = Callable[[], Profile | None]


@dataclass
class PlanEntry:
    source: SourceRow
    has_adapter: bool
    queries: int
    watermark: datetime
    skip_reason: str | None = None


@dataclass
class SourceResult:
    key: str
    status: str = "ok"
    queries_run: int = 0
    stubs_found: int = 0
    resolved: int = 0
    error: str | None = None
    duration_ms: int = 0
    errors: int = 0
    max_posted: datetime | None = None

    @property
    def failed(self) -> bool:
        return self.status != "ok"


@dataclass
class RunReport:
    run_id: int | None = None
    plan: list[PlanEntry] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    results: dict[str, SourceResult] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    # The score stage's result as a fixed code for readiness.json (specs/019 S4): scored,
    # skipped_no_credential, cap_reached, failed:<exception type>, no_profile or not_run.
    score_outcome: str = "not_run"
    trigger: str = "manual"

    @property
    def non_ok(self) -> list[SourceResult]:
        return [r for r in self.results.values() if r.failed]

    @property
    def exit_code(self) -> int:
        return 1 if self.non_ok else 0


def parse_stages(values: Iterable[str] | None) -> list[str]:
    """Repeatable and/or comma-separated ``--stage`` values, in canonical order."""
    if not values:
        return list(STAGES)
    wanted: set[str] = set()
    for value in values:
        for part in value.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in STAGES:
                raise ValueError(f"unknown stage {name!r}; choose from {', '.join(STAGES)}")
            wanted.add(name)
    return [s for s in STAGES if s in wanted]


def parse_states(value: str | Iterable[str] | None) -> set[str] | None:
    if not value:
        return None
    parts = value.split(",") if isinstance(value, str) else list(value)
    states = {p.strip().upper() for p in parts if p.strip()}
    return states or None


def filter_sources(rows: Iterable[SourceRow], states: set[str] | None) -> list[SourceRow]:
    """Enabled rows only. National/federal rows (state None) pass with no filter or with US."""
    out = []
    for r in rows:
        if r.policy is not Policy.enabled:
            continue
        if states is not None:
            if r.state is None:
                if "US" not in states:
                    continue
            elif r.state.upper() not in states:
                continue
        out.append(r)
    return out


def default_profile_loader(settings: Settings) -> ProfileLoader:
    def load() -> Profile | None:
        profile_dir = resolve_path(settings.paths.profile_dir)
        if not profile_dir.is_dir():
            return None
        try:
            return load_profile_for(settings)
        except ProfileError as exc:
            logger.warning("profile unusable: %s", exc)
            return None

    return load


def queries_for(src: SourceRow, profile: Profile | None) -> list[Query] | None:
    """Queries for a source; None means they cannot be built (profile needed, none loaded)."""
    if src.queries != "from_profile":
        return list(src.queries)
    if profile is None:
        return None
    return queries_from_profile(profile)


def effective_since(
    conn: sqlite3.Connection,
    src: SourceRow,
    now: datetime,
    *,
    since: datetime | None,
    full: bool,
) -> datetime:
    if since is not None:
        return since if since.tzinfo else since.replace(tzinfo=UTC)
    if full:
        return now - timedelta(days=FULL_LOOKBACK_DAYS)
    return compute_watermark(conn, src.key, now)


def build_plan(
    conn: sqlite3.Connection,
    rows: Sequence[SourceRow],
    adapters: Mapping[str, Callable[[], SourceAdapter]],
    profile: Profile | None,
    now: datetime,
    *,
    since: datetime | None = None,
    full: bool = False,
) -> list[PlanEntry]:
    plan = []
    for src in rows:
        queries = queries_for(src, profile)
        reason = None
        if src.family not in adapters:
            reason = "no adapter yet"
        elif queries is None:
            reason = "no profile: from_profile queries unavailable"
        plan.append(
            PlanEntry(
                source=src,
                has_adapter=src.family in adapters,
                queries=len(queries or []),
                watermark=effective_since(conn, src, now, since=since, full=full),
                skip_reason=reason,
            )
        )
    return plan


def mark_unavailable(
    plan: Sequence[PlanEntry],
    adapters: Mapping[str, Callable[[], SourceAdapter]],
    settings: Settings,
) -> None:
    """Skip sources whose adapter says it cannot run (``unavailable(settings)``), e.g. the
    email adapter before ``jobhunter mail auth`` has stored a Gmail token."""
    for e in plan:
        if e.skip_reason is not None:
            continue
        check = getattr(adapters.get(e.source.family), "unavailable", None)
        if callable(check) and (why := check(settings)):
            e.skip_reason = str(why)


def format_plan(plan: Sequence[PlanEntry], stages: Sequence[str], profile_loaded: bool) -> str:
    lines = [f"Run plan: stages {', '.join(stages)}"]
    if not profile_loaded:
        lines.append("note: no profile loaded; prefilter and score stages will be skipped")
    if not plan:
        lines.append("no sources enabled")
        return "\n".join(lines)
    lines.append(f"{'KEY':<24}{'FAMILY':<11}{'ADAPTER':<9}{'QUERIES':<8}WATERMARK")
    for e in plan:
        adapter = "yes" if e.has_adapter else "no"
        line = (
            f"{e.source.key:<24}{e.source.family:<11}{adapter:<9}{e.queries:<8}"
            f"{e.watermark.isoformat()}"
        )
        if e.skip_reason:
            line += f"  (skipped: {e.skip_reason})"
        lines.append(line)
    return "\n".join(lines)


# ─── source status bookkeeping ──────────────────────────────────────────────


def _mark_source(conn: sqlite3.Connection, key: str, status: str, note: str | None) -> None:
    conn.execute("UPDATE source SET status = ?, status_note = ? WHERE key = ?", (status, note, key))
    conn.commit()


def _record_failure(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO source_state (source_key, last_run_at, consecutive_failures)
        VALUES (?, ?, 1)
        ON CONFLICT(source_key) DO UPDATE SET
          last_run_at = excluded.last_run_at,
          consecutive_failures = source_state.consecutive_failures + 1
        """,
        (key, to_iso(now)),
    )
    conn.commit()


def classify(exc: BaseException) -> str:
    """Map a failure to a source status (specs/004 failure table)."""
    if isinstance(exc, TransientFetchError):
        return "suspect"
    if isinstance(exc, RobotsDisallowed | SourceBlocked):
        return "blocked"
    return "broken"  # AccessDenied and anything unexpected: a human must look


# ─── stages ─────────────────────────────────────────────────────────────────


def _list_source(
    sink: Callable[[list[JobStub]], None],
    src: SourceRow,
    adapter: SourceAdapter,
    queries: list[Query],
    since: datetime,
    ctx: Any,
    res: SourceResult,
    stopper: Stopper | None = None,
) -> None:
    """Run the source's queries, handing stubs to ``sink`` in batches of FLUSH_EVERY (and the
    rest on the way out, also when a query raised)."""
    buf: list[JobStub] = []

    def flush() -> None:
        if buf:
            sink(list(buf))
            buf.clear()

    try:
        for query in queries:
            if stopper is not None:
                stopper.check()
            res.queries_run += 1
            for stub in adapter.search(src, query, since, ctx):
                res.stubs_found += 1
                if stub.posted_at and (res.max_posted is None or stub.posted_at > res.max_posted):
                    res.max_posted = stub.posted_at
                buf.append(stub)
                if len(buf) >= FLUSH_EVERY:
                    flush()
    finally:
        flush()


def _volume_floor_breach(conn: sqlite3.Connection, src: SourceRow, now: datetime) -> bool:
    floor = src.expect.get("min_jobs_per_week") or 0
    if not floor:
        return False
    n = conn.execute(
        "SELECT COUNT(*) FROM job WHERE source_key = ? AND first_seen_at >= ?",
        (src.key, to_iso(now - timedelta(days=7))),
    ).fetchone()[0]
    return n < floor


def _resolve_candidates(
    conn: sqlite3.Connection, key: str, now: datetime, limit: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM job
        WHERE source_key = ? AND stage = 'listed' AND needs_resolve = 1
          AND (description_raw IS NULL OR description_raw = ''
               OR description_completeness = 'partial')
          AND (closes_at IS NULL OR closes_at > ?)
        ORDER BY posted_at DESC, id
        LIMIT ?
        """,
        (key, to_iso(now), limit),
    ).fetchall()


def _stub_from_row(row: sqlite3.Row) -> JobStub:
    return JobStub(
        source_key=row["source_key"],
        external_id=row["external_id"],
        title=row["title"],
        url=row["url"],
        posted_at=from_iso(row["posted_at"]) if row["posted_at"] else None,
        closes_at=from_iso(row["closes_at"]) if row["closes_at"] else None,
        location_raw=row["location_raw"],
        salary_raw=row["salary_raw"],
        agency_raw=row["agency_raw"],
        apply_url=row["apply_url"],
        description_raw=row["description_raw"] or None,
        description_completeness=row["description_completeness"],
    )


def _resolve_source(
    conn: sqlite3.Connection,
    src: SourceRow,
    adapter: SourceAdapter,
    ctx: Any,
    res: SourceResult,
    candidates: Sequence[sqlite3.Row],
    *,
    db_lock: AbstractContextManager[Any] | None = None,
    stopper: Stopper | None = None,
) -> int:
    """Resolve ``candidates`` (from :func:`_resolve_candidates`). May run on a fan-out thread:
    every statement holds ``db_lock``; the fetch itself never does."""
    lock = db_lock if db_lock is not None else nullcontext()
    done = 0
    total = len(candidates)
    if total:
        spacing = getattr(ctx, "spacing", None)
        every, why = spacing(src.entry) if callable(spacing) else (None, "")
        logger.info(
            "resolve %s: %d candidates%s",
            src.key,
            total,
            f", one request every {every:.1f}s ({why})" if every else "",
        )
    last_log = time.monotonic()
    last_done = 0
    for i, row in enumerate(candidates):
        if done != last_done and (
            done % PROGRESS_EVERY == 0 or time.monotonic() - last_log >= PROGRESS_SECONDS
        ):
            last_log, last_done = time.monotonic(), done
            logger.info("resolve %s: %d of %d postings tried, %d resolved", src.key, i, total, done)
        if stopper is not None:
            stopper.check()
        with lock:
            conn.execute(
                "UPDATE job SET resolve_attempts = resolve_attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
        try:
            detail = adapter.resolve(_stub_from_row(row), ctx)
        except (TransientFetchError, AccessDenied, RobotsDisallowed, SourceBlocked):
            raise
        except Exception as exc:  # one bad posting never fails a source
            logger.warning("resolve failed for %s/%s: %s", src.key, row["external_id"], exc)
            res.errors += 1
            continue
        with lock:
            _store_detail(conn, row["id"], detail)
        done += 1
        res.resolved += 1
    return done


def _store_detail(conn: sqlite3.Connection, job_id: int, detail: Any) -> None:
    conn.execute(
        """
            UPDATE job SET description_raw = ?, needs_resolve = 0, stage = 'resolved',
              description_completeness = ?, employment_type = ?,
              apply_url = COALESCE(?, apply_url)
            WHERE id = ?
            """,
        (
            detail.description_raw,
            detail.description_completeness.value,
            detail.employment_type.value,
            detail.apply_url,
            job_id,
        ),
    )
    conn.commit()


def _score_stage(
    conn: sqlite3.Connection,
    settings: Settings,
    profile: Profile | None,
    now: datetime,
    report: RunReport,
) -> None:
    if profile is None:
        report.score_outcome = "no_profile"
        return  # already reported up front
    report.score_outcome = "scored"
    try:
        from jobhunter.scoring import screen  # lazy: built in a separate change
    except ImportError:
        report.messages.append("score: screen stage not installed; skipped")
        report.score_outcome = "failed:ImportError"
        return
    from jobhunter.scoring import rescore
    from jobhunter.scoring.scorers import (
        MISSING,
        credential_for,
        privacy_notice,
        scorer_from_string,
    )

    # new_only: the daily run scores new jobs (and pasted descriptions) only. Re-scoring jobs
    # already scored under another scoring_version, model or prompt costs money, so it runs only
    # as a re-score the user confirmed on /prefs (specs/006, specs/014). Groups a pending or
    # running re-score plan lists are left to that plan.
    spec = settings.scoring.screen_scorer
    if problem := settings.scoring.scorer_problems().get("screen_scorer"):
        report.messages.append(f"score: skipped ({problem}); survivors stay queued")
        logger.warning("score skipped: %s", problem)
        report.score_outcome = "failed:InvalidScorerSpec"
        return
    cred = credential_for(spec, settings.scoring)
    if cred.status == MISSING:  # 'unknown' (e.g. local:) and 'present' try as today
        names = ", ".join(cred.names)
        hint = f" (set {names})" if names else ""
        report.messages.append(
            f"score: skipped: no credential for {spec}{hint}; survivors stay queued"
        )
        report.score_outcome = "skipped_no_credential"
        logger.info("score skipped: no credential for %s", logsetup.spec_label(spec))
        return
    if notice := privacy_notice(spec, settings.scoring):
        report.messages.append(notice)
    # A re-score that died without finishing must not hold its groups back forever: this marks
    # a running request with a stale heartbeat failed, so only live plans exclude groups.
    rescore.running_request(conn, now)
    conn.commit()

    def remaining() -> float:
        return screen.remaining_budget(conn, settings.scoring, now)

    if remaining() <= 0:
        report.messages.append(
            "score: daily or weekly spend cap reached (scoring.daily_cap_usd / weekly_cap_usd); "
            "new jobs stay queued"
        )
        report.counts["score"] = "0"
        report.score_outcome = "cap_reached"
        logger.info("score stopped: daily or weekly spend cap reached; new jobs stay queued")
        return
    logger.info(
        "stage score: scorer %s, %s, $%.2f left under the caps",
        logsetup.spec_label(spec),
        "batch" if spec.startswith("anthropic:") else "synchronous",
        remaining(),
    )
    if not spec.startswith("anthropic:"):
        try:
            sync = screen.score_sync(
                conn,
                scorer_from_string(spec, scoring=settings.scoring),
                profile,
                limit=DEFAULT_SCORE_LIMIT,
                now=now,
                remaining_usd=remaining,
                rejection_days=settings.scoring.employer_rejection_days,
                new_only=True,
            )
        except Exception as exc:  # scoring trouble must not fail the ingest exit code
            logger.warning("score failed: %s", exc)
            report.messages.append(f"score: failed ({exc}); survivors stay queued")
            report.score_outcome = f"failed:{type(exc).__name__}"
            return
        report.counts["score"] = str(sync.written)
        return
    try:
        import anthropic

        client = anthropic.Anthropic()
    except Exception as exc:
        report.messages.append(f"score: no Anthropic client ({exc}); skipped")
        report.score_outcome = f"failed:{type(exc).__name__}"
        return
    try:
        result = screen.submit_batch(
            conn,
            client,
            profile,
            limit=DEFAULT_SCORE_LIMIT,
            now=now,
            scorer=settings.scoring.screen_scorer,
            remaining_usd=remaining,
            rejection_days=settings.scoring.employer_rejection_days,
            new_only=True,
        )
    except Exception as exc:  # scoring trouble must not fail the ingest exit code
        logger.warning("score submit failed: %s", exc)
        report.messages.append(f"score: submit failed ({exc}); survivors stay queued")
        report.score_outcome = f"failed:{type(exc).__name__}"
        return
    report.counts["score"] = str(result)


def _apply_links_stage(
    conn: sqlite3.Connection,
    settings: Settings,
    profile: Profile | None,
    now: datetime,
    report: RunReport,
    apply_ctx_factory: Callable[[str], Any] | None,
) -> None:
    """Resolve apply links for shortlisted and A/B groups (bug cf23a1c). After scoring, so it
    never holds scoring up; requests only through FetchContext, nothing paid. Any failure is
    logged and noted in the counts; it never fails the run."""
    limit = settings.apply_links.per_run
    if limit <= 0:
        report.counts["apply_links"] = {"off": 1}
        logger.info("stage apply_links: off (apply_links.per_run = 0)")
        return
    from jobhunter.pipeline import apply_pass

    try:
        result = apply_pass.resolve_likely(
            conn,
            settings,
            profile,
            now=now,
            limit=limit,
            workers=settings.apply_links.workers,
            ctx_factory=apply_ctx_factory,
        )
    except Exception as exc:  # never fail the run over apply links
        logger.warning("apply-link pass failed: %s", exc, exc_info=True)
        report.messages.append(
            f"apply_links: failed ({type(exc).__name__}); links resolve on demand"
        )
        report.counts["apply_links"] = {"failed": 1}
        return
    report.counts["apply_links"] = result.counts()


# ─── orchestration ──────────────────────────────────────────────────────────


@dataclass
class _Listed:
    """One source's list-stage result from a fan-out thread."""

    stubs: list[JobStub] = field(default_factory=list)
    error: Exception | None = None
    seconds: float = 0.0
    stats: dict[str, int] = field(default_factory=dict)


def fetch_workers(settings: Settings) -> int:
    """Sources fetched at once: ``fetch.parallel_sources``, never above the global cap."""
    return max(1, min(settings.fetch.parallel_sources, settings.fetch.global_concurrency))


def parallel_safe(adapter: Any) -> bool:
    """Opt-in: only an adapter that declares ``parallel_safe = True`` (it never touches
    ``ctx.conn``) runs on a fan-out thread; any other runs alone on the run's own thread."""
    return getattr(adapter, "parallel_safe", False) is True


def run_pipeline(
    conn: sqlite3.Connection,
    settings: Settings,
    rows: Sequence[SourceRow],
    *,
    adapters: Mapping[str, Callable[[], SourceAdapter]] | None = None,
    ctx_factory: CtxFactory | None = None,
    profile_loader: ProfileLoader | None = None,
    stages: Sequence[str] | None = None,
    states: set[str] | None = None,
    since: datetime | None = None,
    full: bool = False,
    max_resolve: int = DEFAULT_MAX_RESOLVE,
    now: datetime | None = None,
    apply_ctx_factory: Callable[[str], Any] | None = None,
    trigger: str = "manual",
) -> RunReport:
    adapters = ADAPTERS if adapters is None else adapters
    stages = list(stages) if stages is not None else list(STAGES)
    now = now or datetime.now(UTC)
    stopper = Stopper()
    if ctx_factory is None:

        def ctx_factory(src: SourceRow) -> AbstractContextManager[Any]:
            # Ctrl-C: waits (rate limit, Crawl-delay, Retry-After, backoff) end at once and no
            # new request attempt starts.
            return FetchContext(src, settings, conn, sleep=stopper.sleep, cancel=stopper.check)

    profile = (profile_loader or default_profile_loader(settings))()
    report = RunReport(stages=stages, trigger="timer" if trigger == "timer" else "manual")

    sync_sources_table(conn, rows)
    selected = filter_sources(rows, states)
    report.plan = build_plan(conn, selected, adapters, profile, now, since=since, full=full)
    mark_unavailable(report.plan, adapters, settings)

    if profile is None:
        for s in NEEDS_PROFILE:
            if s in stages:
                report.messages.append(
                    f"{s}: skipped (no profile; create profile/preferences.yaml)"
                )
    cur = conn.execute(
        'INSERT INTO run (started_at, stages, "trigger") VALUES (?, ?, ?)',
        (to_iso(now), ",".join(stages), report.trigger),
    )
    report.run_id = cur.lastrowid
    conn.commit()
    run_t0 = time.monotonic()
    skipped = sum(1 for e in report.plan if e.skip_reason)
    logger.info(
        "run %s start: trigger=%s stages=%s sources=%d planned, %d skipped, profile=%s",
        report.run_id,
        report.trigger,
        ",".join(stages),
        len(report.plan) - skipped,
        skipped,
        "loaded" if profile is not None else "missing",
    )

    runnable = [e for e in report.plan if e.skip_reason is None]
    results = {e.source.key: SourceResult(e.source.key) for e in runnable}
    entries = {e.source.key: e for e in runnable}
    adapter_cache = {k: adapters[e.source.family]() for k, e in entries.items()}

    def fail(key: str, exc: BaseException) -> None:
        res = results[key]
        res.status = classify(exc)
        res.error = f"{type(exc).__name__}: {exc}"
        logger.warning("source %s -> %s: %s", key, res.status, res.error)
        _mark_source(conn, key, res.status, res.error)
        _record_failure(conn, key, now)

    def stage_done(name: str, t0: float, detail: str = "") -> None:
        logger.info("stage %s done in %.1fs%s", name, time.monotonic() - t0, detail)

    shared_lock = connection_lock(conn)
    # Only a connection from db.connect carries the lock that lets threads share it.
    workers = fetch_workers(settings) if shared_lock is not None else 1
    db_lock: AbstractContextManager[Any] = shared_lock or threading.RLock()

    def inline(entry: PlanEntry) -> bool:
        return not parallel_safe(adapter_cache[entry.source.key])

    # Sources that run alone go after the others, so they never hold up the pool (and the mail
    # adapter dedupes against this run's listings). Same order in a serial run.
    ordered = [e for e in entries.values() if not inline(e)]
    ordered += [e for e in entries.values() if inline(e)]

    if "list" in stages:
        total = 0
        stage_t0 = time.monotonic()
        logger.info(
            "stage list: %d sources, %d at a time", len(entries), min(workers, len(entries))
        )

        def list_work(entry: PlanEntry) -> _Listed:
            key, src = entry.source.key, entry.source
            res = results[key]
            # Fanned-out sources keep their stubs; this thread writes them in plan order.
            # Sources that run alone (and a serial run) write as they go.
            out = _Listed()
            if workers > 1 and not inline(entry):
                sink: Callable[[list[JobStub]], None] = out.stubs.extend
            else:

                def sink(batch: list[JobStub]) -> None:
                    with db_lock:
                        upsert_stubs(conn, batch, now)

            t0 = time.monotonic()
            try:
                with ctx_factory(src) as ctx:
                    try:
                        _list_source(
                            sink,
                            src,
                            adapter_cache[key],
                            queries_for(src, profile) or [],
                            entry.watermark,
                            ctx,
                            res,
                            stopper,
                        )
                    finally:
                        out.stats = dict(getattr(ctx, "stats", None) or {})
            except Exception as exc:
                out.error = exc
            out.seconds = time.monotonic() - t0
            # Logged as it happens, from the worker; the status line follows once the stubs
            # are written, which is in plan order and may wait for a slower earlier source.
            logger.info(
                "fetched %s: %d stubs from %d queries in %.1fs%s",
                key,
                res.stubs_found,
                res.queries_run,
                out.seconds,
                f" ({type(out.error).__name__})" if out.error else "",
            )
            return out

        def list_done(entry: PlanEntry, outcome: Outcome[_Listed]) -> None:
            key, src = entry.source.key, entry.source
            res = results[key]
            listed = outcome.value or _Listed(error=outcome.error)
            with db_lock:
                try:
                    # Each batch is its own transaction: a failure keeps the batches before it.
                    for i in range(0, len(listed.stubs), FLUSH_EVERY):
                        upsert_stubs(conn, listed.stubs[i : i + FLUSH_EVERY], now)
                    if listed.error is not None:
                        fail(key, listed.error)
                    else:
                        record_run_watermark(conn, key, res.max_posted, now)
                        if _volume_floor_breach(conn, src, now):
                            res.status = "suspect"
                            res.error = "below expect.min_jobs_per_week"
                            _mark_source(conn, key, "suspect", res.error)
                        else:
                            _mark_source(conn, key, "ok", None)
                except Exception as exc:  # e.g. "database is locked": this source only
                    try:
                        fail(key, exc)
                    except Exception as again:
                        res.status = "broken"
                        res.error = f"{type(exc).__name__}: {exc}"
                        logger.warning("source %s: could not record failure: %s", key, again)
            res.duration_ms += int(listed.seconds * 1000)
            stats = listed.stats
            logger.info(
                "list %s: status=%s queries=%d pages=%d (cached %d) stubs=%d robots_skips=%d "
                "errors=%d in %.1fs",
                key,
                res.status,
                res.queries_run,
                stats.get("pages", 0),
                stats.get("cached", 0),
                res.stubs_found,
                stats.get("robots_skips", 0),
                1 if res.failed and res.status != "suspect" else 0,
                listed.seconds,
            )

        run_ordered(ordered, list_work, list_done, workers=workers, stopper=stopper, inline=inline)
        total = sum(results[k].stubs_found for k in entries)
        report.counts["list"] = {"stubs": total}
        stage_done("list", stage_t0, f": {total} stubs from {len(entries)} sources")
        for e in report.plan:
            if e.skip_reason:
                report.messages.append(f"list: {e.source.key} skipped ({e.skip_reason})")

    if "resolve" in stages:
        # The budget is shared out in plan order before any fetch, so it does not depend on
        # which source finishes first: each source gets up to what is left of max_resolve.
        budget, total = max_resolve, 0
        stage_t0 = time.monotonic()
        logger.info("stage resolve: budget %d", max_resolve)
        todo: list[tuple[PlanEntry, list[sqlite3.Row]]] = []
        for key, entry in entries.items():
            if budget <= 0:
                break
            if results[key].failed and results[key].status != "suspect":
                continue
            candidates = _resolve_candidates(conn, key, now, budget)
            if candidates:
                todo.append((entry, candidates))
                budget -= len(candidates)
        todo = [t for t in todo if not inline(t[0])] + [t for t in todo if inline(t[0])]
        logger.info(
            "stage resolve: %d postings from %d sources, %d at a time",
            sum(len(c) for _, c in todo),
            len(todo),
            min(workers, len(todo)),
        )

        def resolve_work(item: tuple[PlanEntry, list[sqlite3.Row]]) -> int:
            entry, candidates = item
            key = entry.source.key
            t0 = time.monotonic()
            n = 0
            try:
                with ctx_factory(entry.source) as ctx:
                    n = _resolve_source(
                        conn,
                        entry.source,
                        adapter_cache[key],
                        ctx,
                        results[key],
                        candidates,
                        db_lock=db_lock,
                        stopper=stopper,
                    )
            finally:
                seconds = time.monotonic() - t0
                results[key].duration_ms += int(seconds * 1000)
                logger.info(
                    "resolve %s: %d resolved, %d errors in %.1fs",
                    key,
                    results[key].resolved,
                    results[key].errors,
                    seconds,
                )
            return n

        def resolve_done(item: tuple[PlanEntry, list[sqlite3.Row]], outcome: Outcome[int]) -> None:
            if outcome.error is None:
                return
            key = item[0].source.key
            with db_lock:
                try:
                    fail(key, outcome.error)
                except Exception as again:
                    results[key].status = "broken"
                    results[key].error = f"{type(outcome.error).__name__}: {outcome.error}"
                    logger.warning("source %s: could not record failure: %s", key, again)

        run_ordered(
            todo,
            resolve_work,
            resolve_done,
            workers=workers,
            stopper=stopper,
            inline=lambda item: inline(item[0]),
        )
        total = sum(results[e.source.key].resolved for e, _ in todo)
        report.counts["resolve"] = {"resolved": total}
        # Apply links stored before login walls were recognized (bug 334d9eb): resolve those
        # groups again. Requests only through FetchContext; nothing paid.
        from jobhunter.pipeline import applylink

        try:
            redone = applylink.reresolve_login_walls(
                conn,
                now=now,
                ctx_factory=apply_ctx_factory or applylink.default_ctx_factory(settings, conn),
            )
        except Exception as exc:  # never fail the run over apply links
            logger.warning("apply-link login-wall pass failed: %s", exc)
            redone = []
        report.counts["resolve"]["apply_link_login_walls"] = len(redone)
        stage_done("resolve", stage_t0, f": {total} resolved, {len(redone)} login-wall re-resolved")

    if "normalize" in stages:
        stage_t0 = time.monotonic()
        tzs = {r.key: str(r.config.get("timezone", "UTC")) for r in rows}
        report.counts["normalize"] = {"rows": normalize_pending(conn, source_timezones=tzs)}
        stage_done("normalize", stage_t0, f": {report.counts['normalize']['rows']} rows")
    if "dedupe" in stages:
        stage_t0 = time.monotonic()
        d = group_pending(conn, now=now)
        report.counts["dedupe"] = {k: v for k, v in vars(d).items() if isinstance(v, int)}
        m = merge_by_apply_url(conn, now=now)
        report.counts["dedupe_url"] = {k: v for k, v in vars(m).items() if isinstance(v, int)}
        x = merge_cross_state(conn, now=now)
        report.counts["dedupe_xstate"] = {k: v for k, v in vars(x).items() if isinstance(v, int)}
        # A merge can abandon a packet: its employer-named copies go now (019561b (3)).
        from jobhunter.apply.export import sync_abandoned

        sync_abandoned(conn, resolve_path(settings.paths.data_dir))
        stage_done(
            "dedupe",
            stage_t0,
            ": "
            + ", ".join(
                f"{k} {v}"
                for k in ("dedupe", "dedupe_url", "dedupe_xstate")
                for v in [sum(report.counts[k].values())]
            ),
        )
    if "prefilter" in stages and profile is not None:
        stage_t0 = time.monotonic()
        report.counts["prefilter"] = run_prefilter(conn, profile, now=now)
        stage_done("prefilter", stage_t0, f": {report.counts['prefilter']}")
    if "score" in stages:
        stage_t0 = time.monotonic()
        _score_stage(conn, settings, profile, now, report)
        stage_done(
            "score", stage_t0, f": outcome={report.score_outcome} jobs={report.counts.get('score')}"
        )
    if "apply_links" in stages:
        stage_t0 = time.monotonic()
        _apply_links_stage(conn, settings, profile, now, report, apply_ctx_factory)
        stage_done("apply_links", stage_t0, f": {report.counts.get('apply_links')}")

    for key, res in results.items():
        conn.execute(
            "INSERT INTO run_source (run_id, source_key, queries_run, stubs_found, resolved, "
            "status, error, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.run_id,
                key,
                res.queries_run,
                res.stubs_found,
                res.resolved,
                res.status,
                res.error,
                res.duration_ms,
            ),
        )
    report.results = results
    status = "ok" if not report.non_ok else "degraded"
    conn.execute(
        "UPDATE run SET ended_at = ?, exit_status = ?, counts = ? WHERE id = ?",
        (to_iso(datetime.now(UTC)), status, json.dumps(report.counts), report.run_id),
    )
    conn.commit()
    logger.info(
        "run %s end: status=%s, %d sources (%d not ok) in %.1fs",
        report.run_id,
        status,
        len(results),
        len(report.non_ok),
        time.monotonic() - run_t0,
    )
    return report


def dry_run_plan(
    conn: sqlite3.Connection,
    rows: Sequence[SourceRow],
    *,
    adapters: Mapping[str, Callable[[], SourceAdapter]] | None = None,
    profile: Profile | None,
    stages: Sequence[str],
    states: set[str] | None = None,
    since: datetime | None = None,
    full: bool = False,
    now: datetime | None = None,
) -> str:
    """Printable plan. Reads watermarks only; no network, no writes."""
    plan = build_plan(
        conn,
        filter_sources(rows, states),
        ADAPTERS if adapters is None else adapters,
        profile,
        now or datetime.now(UTC),
        since=since,
        full=full,
    )
    return format_plan(plan, stages, profile is not None)


def format_summary(report: RunReport) -> str:
    """Only what needs attention; silence means healthy."""
    lines = list(report.messages)
    if report.non_ok:
        lines.append("Sources needing attention:")
        for r in report.non_ok:
            lines.append(f"  {r.key}: {r.status}" + (f" ({r.error})" if r.error else ""))
    return "\n".join(lines)
