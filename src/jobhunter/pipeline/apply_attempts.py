"""How many times a group's apply link was resolved, and whether to try again (bug c369bcc).

A group's link is **settled** when it was set by the user, or its ``apply_link`` row is
``expired`` or ``blocked``, or ``live`` on the page where the application happens (an ATS, the
employer's site, or any other host that is not a job board or a tracker). Otherwise it is
**unsettled** and is tried again, by the nightly ``apply_links`` pass and by the console's
on-demand walk, until ``apply_links.max_attempts`` tries were made:

- no row yet, or a deferred try that stored none;
- an ``unresolved`` row;
- a ``live`` row that ends on a job board copy or a tracker (``HostTier.aggregator`` or
  ``HostTier.tracker``, such as the NLX mirrors on ``*.dejobs.org``): "a whole new domain" is
  not resolved. It stays the best known link for Apply meanwhile, and a retry replaces it only
  with a link no worse (``resolve_group(only_if_better=True)``).

Every try counts, whatever came of it (a deferred try, a failure, a group with no URL to start
from).

Tries are spaced: after the n-th try the next waits ``BACKOFF_H[n - 1]`` hours (the last
entry for any later try), counted from the try's start. The nightly pass makes at most one try
per group per run. After the last try the group keeps "Open posting" and is not tried again
until the user resets it on the job page, which clears the count.

The count is kept after a link settles (it is history, shown on the job page), and a settled
link is never retried, so it needs no reset there.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobhunter.core.models import ApplyLink, ApplyStatus
from jobhunter.pipeline.apply_rank import HostClasses, HostTier
from jobhunter.pipeline.applylink import is_login_link, needs_reresolve
from jobhunter.pipeline.listing import _txn, from_iso, to_iso

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
# Hours to wait after the 1st, 2nd, 3rd, 4th (and any later) try before the next one.
BACKOFF_H: tuple[float, ...] = (1.0, 4.0, 12.0, 20.0)

NOT_RESOLVED_TIERS = (HostTier.tracker, HostTier.aggregator)

# SQL: an apply_link row (alias ``a``, LEFT JOINed) that may leave the group unsettled; a
# ``live`` row still needs ``unsettled`` (its final host's tier) to tell.
MAYBE_UNSETTLED_SQL = (
    "(a.job_group_id IS NULL OR (a.chosen_by IS NOT 'user' AND a.status IN ('unresolved', 'live')))"
)


@dataclass
class Attempts:
    group_id: int
    count: int
    last_at: datetime
    last_outcome: str | None = None

    def next_at(self) -> datetime:
        """When the next try may start."""
        if self.count <= 0:
            return self.last_at
        gap = BACKOFF_H[min(self.count, len(BACKOFF_H)) - 1]
        return self.last_at + timedelta(hours=gap)


def _utc(now: datetime) -> datetime:
    return from_iso(to_iso(now))


def board_copy(link: ApplyLink | None, classes: HostClasses) -> bool:
    """A live link, not set by the user, that ends on a job board copy or a tracker."""
    if link is None or link.chosen_by == "user" or link.status != ApplyStatus.live:
        return False
    if is_login_link(link):
        return False  # an older login-wall row: needs_reresolve's business (334d9eb)
    return classes.tier(link.final_url) in NOT_RESOLVED_TIERS


def unsettled(
    conn: sqlite3.Connection, link: ApplyLink | None, classes: HostClasses | None = None
) -> bool:
    """Whether the group's link is worth another try (see the module docstring)."""
    if link is None:
        return True
    if link.chosen_by == "user" or needs_reresolve(link):
        return False
    if link.status == ApplyStatus.unresolved:
        return True
    if link.status != ApplyStatus.live:
        return False
    return board_copy(link, classes or HostClasses.load(conn, link.job_group_id))


def get(conn: sqlite3.Connection, group_id: int) -> Attempts | None:
    row = conn.execute(
        "SELECT attempts, last_at, last_outcome FROM apply_link_attempt WHERE job_group_id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        return None
    return Attempts(group_id, int(row[0]), from_iso(row[1]), row[2])


def due(attempts: Attempts | None, *, now: datetime, max_attempts: int) -> bool:
    """Whether another try is allowed now: under the limit and past the back-off."""
    if attempts is None:
        return max_attempts > 0
    return attempts.count < max_attempts and _utc(now) >= attempts.next_at()


def may_retry(conn: sqlite3.Connection, group_id: int, *, now: datetime, max_attempts: int) -> bool:
    return due(get(conn, group_id), now=now, max_attempts=max_attempts)


def record(conn: sqlite3.Connection, group_id: int, *, now: datetime, outcome: str) -> None:
    """Count one try. Never raises: a count that cannot be written is logged and skipped."""
    try:
        with _txn(conn):
            conn.execute(
                "INSERT INTO apply_link_attempt (job_group_id, attempts, last_at, last_outcome) "
                "VALUES (?, 1, ?, ?) ON CONFLICT (job_group_id) DO UPDATE SET "
                "attempts = attempts + 1, last_at = excluded.last_at, "
                "last_outcome = excluded.last_outcome",
                (group_id, to_iso(now), outcome),
            )
    except sqlite3.Error as exc:
        logger.warning("apply link for group %s: try not counted: %s", group_id, exc)


def reset(conn: sqlite3.Connection, group_id: int) -> None:
    """Forget the tries (Reset on the job page): the group gets the full count again."""
    with _txn(conn):
        conn.execute("DELETE FROM apply_link_attempt WHERE job_group_id = ?", (group_id,))


def summary(
    attempts: Attempts | None, *, now: datetime, max_attempts: int, board: bool = False
) -> str | None:
    """One muted line for the job page about an unsettled link: a board copy is not resolved
    yet, and how many tries were made."""
    count = attempts.count if attempts is not None else 0
    head = "not resolved yet (board copy)" if board else None
    if count <= 0:
        return head
    tries = "try" if count == 1 else "tries"
    if count >= max_attempts:
        tail = f"gave up after {count} {tries}"
    else:
        tail = f"tried {count} of {max_attempts} times"
        if attempts is not None and _utc(now) < attempts.next_at():
            tail += ", trying again later"
    if head is None:
        return f"apply link not found: {tail}"
    return f"{head}; {tail}"
