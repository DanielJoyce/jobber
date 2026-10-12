"""Nightly apply-link pass: resolve links for the jobs you are likely to open (bug cf23a1c).

The run's ``apply_links`` stage, after scoring. Apply links otherwise resolve on demand (the
job page, a click on Apply), which can keep you waiting on a slow site. This pass resolves
them ahead of time for the groups most likely to be opened:

- **shortlisted**: an application at ``interested`` or ``preparing``, or an ``interesting``
  label with no application yet; then
- **A/B fit**: untriaged groups whose latest score (deep preferred) lands in bucket A or B
  under the current profile, best first.

Only groups with no ``apply_link`` row are taken, so a link you set by hand, a link already
resolved and a row already stopped at a login wall (``blocked``) are never fetched here; the
pass never retries a blocked row. Postings past their closing date are skipped.

Bounds: at most ``apply_links.per_run`` groups, and no group starts after
``apply_links.max_minutes`` (the collect unit waits for the run lock only so long).

An outage never freezes a worse link: when a start failed transiently (429, 5xx, a transport
error, a host held by Retry-After, an unreachable robots.txt) and nothing settled the group,
no row is written (``resolve_group(defer_transient=True)``) and the group stays eligible.

Network only through ``FetchContext`` (``applylink.resolve_group``): robots.txt and the
process-wide per-host rate limits hold across threads. Nothing here spends credits. With a
database file, up to ``apply_links.workers`` groups resolve at once, each thread on its own
connection (as the console's ``apply_bg`` does), since ``resolve_group`` reads and writes as
it walks. Ctrl-C ends rate-limit waits at once and starts no new request (``cancel=``).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import fetch_context_for_url
from jobhunter.core.models import Bucket
from jobhunter.pipeline import applylink
from jobhunter.pipeline.listing import to_iso
from jobhunter.pipeline.locations import load_many_job_group_locations
from jobhunter.scoring.buckets import compute_row
from jobhunter.scoring.profile import Profile

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 25  # a progress line every N groups or PROGRESS_SECONDS
PROGRESS_SECONDS = 30.0
LIKELY_BUCKETS = (Bucket.A, Bucket.B)
USER_LINK = "user_link"
DEFERRED = "deferred"
LATE = "late"  # not started: the pass ran out of time


class PassCancelled(BaseException):
    """The run is stopping (Ctrl-C): fetch waits end and no new request starts."""


@dataclass
class Candidate:
    group_id: int
    why: str  # shortlist | A | B
    overall: float = 0.0


@dataclass
class PassResult:
    candidates: int = 0  # groups that qualified (no row yet), before the bound
    shortlist: int = 0
    ab: int = 0
    tried: int = 0  # groups started
    outcomes: Counter[str] = field(default_factory=Counter)
    no_url: int = 0
    errors: int = 0

    def counts(self) -> dict[str, int]:
        """For ``run.counts``. /log shows the first eight keys, so the outcome comes first."""
        o = self.outcomes
        return {
            "tried": self.tried,
            "live": o["live"],
            "unresolved": o["unresolved"],
            "deferred": o[DEFERRED],
            "errors": self.errors,
            "blocked": o["blocked"],
            "expired": o["expired"],
            "left": max(0, self.candidates - self.tried),
            "late": o[LATE],
            "user_link": o[USER_LINK],
            "candidates": self.candidates,
            "shortlist": self.shortlist,
            "ab": self.ab,
            "no_url": self.no_url,
        }


_OPEN = "(j.closes_at IS NULL OR j.closes_at > :now)"

_SHORTLIST_SQL = f"""
SELECT g.id
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
LEFT JOIN apply_link a ON a.job_group_id = g.id
LEFT JOIN label l ON l.job_group_id = g.id
LEFT JOIN application ap ON ap.job_group_id = g.id
WHERE a.job_group_id IS NULL AND {_OPEN}
  AND (ap.status IN ('interested', 'preparing')
       OR (ap.id IS NULL AND l.label = 'interesting'))
ORDER BY g.id DESC
"""

_SCORED_SQL = f"""
SELECT g.id AS group_id, j.id AS job_id, j.salary_min, j.salary_max, j.salary_period,
       j.salary_stated, j.location_scope, j.description_completeness,
       fs.dimensions, fs.blockers
FROM job_group g
JOIN job j ON j.id = g.canonical_job_id
JOIN fit_score fs ON fs.id = (
    SELECT f2.id FROM fit_score f2 WHERE f2.job_group_id = g.id
    ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1)
LEFT JOIN apply_link a ON a.job_group_id = g.id
LEFT JOIN label l ON l.job_group_id = g.id
WHERE a.job_group_id IS NULL AND l.job_group_id IS NULL AND {_OPEN}
  AND NOT EXISTS (SELECT 1 FROM application ap WHERE ap.job_group_id = g.id)
"""


def select_groups(
    conn: sqlite3.Connection, profile: Profile | None, *, now: datetime
) -> list[Candidate]:
    """Every group the pass would resolve, in order: shortlisted (newest first), then A, then
    B (best overall first). A/B needs the profile; without one only the shortlist counts."""
    params = {"now": to_iso(now)}
    out = [Candidate(r[0], "shortlist") for r in conn.execute(_SHORTLIST_SQL, params)]
    if profile is None:
        return out
    taken = {c.group_id for c in out}
    rows = [r for r in conn.execute(_SCORED_SQL, params) if r["group_id"] not in taken]
    locs = load_many_job_group_locations(conn, (r["job_id"] for r in rows))
    likely: list[Candidate] = []
    for r in rows:
        fit = compute_row(r, r, locs.get(r["job_id"], []), profile)
        if fit.bucket in LIKELY_BUCKETS:
            likely.append(Candidate(r["group_id"], fit.bucket.value, fit.overall))
    likely.sort(key=lambda c: (c.why, -c.overall, -c.group_id))
    return out + likely


def resolve_one(
    conn: sqlite3.Connection, group_id: int, *, now: datetime, ctx_factory: applylink.CtxFactory
) -> str:
    """Resolve one group; the outcome label: a stored status, ``user_link`` (a link set by
    hand, before or during the walk, left as it is) or ``deferred`` (nothing written).
    ``ValueError``: the group has no URL to start from."""
    try:
        link = applylink.resolve_group(
            conn, group_id, now=now, ctx_factory=ctx_factory, defer_transient=True
        )
    except applylink.Deferred as exc:
        logger.info("apply link: %s", exc)
        return DEFERRED
    if link.chosen_by != "user":
        stored = applylink.get_apply_link(conn, group_id)
        if stored is not None and stored.chosen_by == "user":
            link = stored  # set while the walk ran: _save kept it
    return USER_LINK if link.chosen_by == "user" else link.status.value


def resolve_likely(
    conn: sqlite3.Connection,
    settings: Settings,
    profile: Profile | None,
    *,
    now: datetime,
    limit: int,
    workers: int = 1,
    max_seconds: float | None = None,
    ctx_factory: applylink.CtxFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PassResult:
    """Resolve up to ``limit`` likely groups' apply links, starting none after
    ``max_seconds``. One group failing never stops the others. ``ctx_factory`` (tests) runs
    everything on ``conn``, one group at a time."""
    result = PassResult()
    picked = select_groups(conn, profile, now=now)
    result.candidates = len(picked)
    result.shortlist = sum(1 for c in picked if c.why == "shortlist")
    result.ab = result.candidates - result.shortlist
    todo = picked[: max(0, limit)]
    path = db.database_file(conn)
    parallel = ctx_factory is None and path is not None and workers > 1 and len(todo) > 1
    n_workers = min(workers, len(todo)) if parallel else 1
    logger.info(
        "apply links: %d groups qualify (%d shortlisted, %d A/B), resolving up to %d, %d at a time",
        result.candidates,
        result.shortlist,
        result.ab,
        len(todo),
        n_workers,
    )
    if not todo:
        return result

    deadline = None if max_seconds is None else clock() + max_seconds
    stop = threading.Event()
    progress = _Progress(len(todo))

    def check() -> None:
        if stop.is_set():
            raise PassCancelled

    def stop_sleep(seconds: float) -> None:
        if stop.wait(max(0.0, seconds)):
            raise PassCancelled

    def factory_on(c: sqlite3.Connection) -> applylink.CtxFactory:
        if ctx_factory is not None:
            return ctx_factory
        return lambda url: fetch_context_for_url(settings, c, url, sleep=stop_sleep, cancel=check)

    def work(cand: Candidate, c: sqlite3.Connection | None = None) -> str:
        check()
        if deadline is not None and clock() >= deadline:
            return LATE
        if c is not None:
            return resolve_one(c, cand.group_id, now=now, ctx_factory=factory_on(c))
        assert path is not None
        own = db.connect(path)
        try:
            return resolve_one(own, cand.group_id, now=now, ctx_factory=factory_on(own))
        finally:
            own.close()

    def record(cand: Candidate, outcome: str | BaseException) -> None:
        if outcome == LATE:
            result.outcomes[LATE] += 1
            return
        result.tried += 1
        if isinstance(outcome, ValueError):
            result.no_url += 1
            logger.info("apply link for group %s: %s", cand.group_id, outcome)
        elif isinstance(outcome, BaseException):
            result.errors += 1
            logger.warning("apply link for group %s failed: %s", cand.group_id, outcome)
        else:
            result.outcomes[outcome] += 1
        progress.tick(result)

    if not parallel:
        try:
            for cand in todo:
                try:
                    outcome: str | BaseException = work(cand, conn)
                except Exception as exc:  # one group never stops the pass
                    outcome = exc
                record(cand, outcome)
        except BaseException:
            stop.set()
            raise
        _log_late(result)
        return result

    pool = ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="jobhunter-applylink")
    futures: dict[Future[str], Candidate] = {}
    try:
        for cand in todo:
            futures[pool.submit(work, cand)] = cand
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                exc = fut.exception()
                record(futures[fut], exc if exc is not None else fut.result())
    except BaseException:
        stop.set()  # waits end and no new request starts in the workers
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    _log_late(result)
    return result


def _log_late(result: PassResult) -> None:
    if result.outcomes[LATE]:
        logger.info(
            "apply links: out of time (apply_links.max_minutes); %d groups wait for the next run",
            result.outcomes[LATE],
        )


class _Progress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.last = time.monotonic()

    def tick(self, r: PassResult) -> None:
        if r.tried == self.total:
            return
        if r.tried % PROGRESS_EVERY and time.monotonic() - self.last < PROGRESS_SECONDS:
            return
        self.last = time.monotonic()
        logger.info(
            "apply links: %d of %d groups tried, %d live, %d deferred, %d errors",
            r.tried,
            self.total,
            r.outcomes["live"],
            r.outcomes[DEFERRED],
            r.errors,
        )
