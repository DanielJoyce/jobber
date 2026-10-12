"""Nightly apply-link pass: resolve links for the jobs you are likely to open (bug cf23a1c).

The run's ``apply_links`` stage, after scoring. Apply links otherwise resolve on demand (the
job page, a click on Apply), which can keep you waiting on a slow site. This pass resolves
them ahead of time for the groups most likely to be opened:

- **shortlisted**: an application at ``interested`` or ``preparing``, or an ``interesting``
  label with no application yet; then
- **A/B fit**: untriaged groups whose latest score (deep preferred) lands in bucket A or B
  under the current profile, best first.

Only groups with no ``apply_link`` row are taken, so a link you set by hand, a link already
resolved, and a login-wall row (re-tried at most once a night by the resolve stage's
login-wall pass, the same back-off as the console's) are never fetched here. Postings past
their closing date are skipped. At most ``apply_links.per_run`` groups per run.

Network only through ``FetchContext`` (``applylink.resolve_group``): robots.txt and per-host
rate limits hold across threads. Nothing here spends credits. With a database file, up to
``apply_links.workers`` groups resolve at once, each thread on its own connection (as the
console's ``apply_bg`` does), since ``resolve_group`` reads and writes as it walks.
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
from typing import Any

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
SHORTLIST_STATUSES = ("interested", "preparing")
LIKELY_BUCKETS = (Bucket.A, Bucket.B)


class PassCancelled(BaseException):
    """The run is stopping (Ctrl-C): fetch waits end and no new group starts."""


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
    tried: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    no_url: int = 0
    errors: int = 0

    def counts(self) -> dict[str, int]:
        """For ``run.counts``. /log shows the first eight keys, so the outcome comes first."""
        return {
            "tried": self.tried,
            "live": self.statuses["live"],
            "unresolved": self.statuses["unresolved"],
            "blocked": self.statuses["blocked"],
            "expired": self.statuses["expired"],
            "errors": self.errors,
            "left": max(0, self.candidates - self.tried),
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


ResolveOne = Callable[[int], applylink.ApplyLink]


def _resolver_for(
    conn: sqlite3.Connection, ctx_factory: applylink.CtxFactory, now: datetime
) -> ResolveOne:
    return lambda gid: applylink.resolve_group(conn, gid, now=now, ctx_factory=ctx_factory)


def resolve_likely(
    conn: sqlite3.Connection,
    settings: Settings,
    profile: Profile | None,
    *,
    now: datetime,
    limit: int,
    workers: int = 1,
    ctx_factory: applylink.CtxFactory | None = None,
) -> PassResult:
    """Resolve up to ``limit`` likely groups' apply links. One group failing never stops the
    others. ``ctx_factory`` (tests) runs everything on ``conn``, one group at a time."""
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
        "apply links: %d groups qualify (%d shortlisted, %d A/B), resolving %d, %d at a time",
        result.candidates,
        result.shortlist,
        result.ab,
        len(todo),
        n_workers,
    )
    if not todo:
        return result

    progress = _Progress(len(todo))

    def record(c: Candidate, outcome: applylink.ApplyLink | BaseException) -> None:
        result.tried += 1
        if isinstance(outcome, ValueError):
            result.no_url += 1  # no member with a usable URL: nothing to fetch
        elif isinstance(outcome, BaseException):
            result.errors += 1
            logger.warning("apply link for group %s failed: %s", c.group_id, outcome)
        else:
            result.statuses[outcome.status.value] += 1
        progress.tick(result)

    if not parallel:
        resolve = _resolver_for(
            conn, ctx_factory or applylink.default_ctx_factory(settings, conn), now
        )
        for c in todo:
            try:
                outcome: Any = resolve(c.group_id)
            except Exception as exc:  # one group never stops the pass
                outcome = exc
            record(c, outcome)
        return result

    assert path is not None
    stop = threading.Event()

    def stop_sleep(seconds: float) -> None:
        if stop.wait(max(0.0, seconds)):
            raise PassCancelled

    def work(c: Candidate) -> applylink.ApplyLink:
        if stop.is_set():
            raise PassCancelled
        own = db.connect(path)
        try:

            def factory(url: str) -> Any:
                return fetch_context_for_url(settings, own, url, sleep=stop_sleep)

            return _resolver_for(own, factory, now)(c.group_id)
        finally:
            own.close()

    pool = ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="jobhunter-applylink")
    futures: dict[Future[applylink.ApplyLink], Candidate] = {}
    try:
        for c in todo:
            futures[pool.submit(work, c)] = c
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                exc = fut.exception()
                record(futures[fut], exc if exc is not None else fut.result())
    except BaseException:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return result


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
            "apply links: %d of %d groups tried, %d live, %d errors",
            r.tried,
            self.total,
            r.statuses["live"],
            r.errors,
        )
