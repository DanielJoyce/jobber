"""How many times a group's apply link was resolved, and whether to try again (bug c369bcc).

A group's link is **settled** when the user set it or a capture supplied it (``chosen_by``), or
its ``apply_link`` row is ``expired`` or ``blocked``, or ``live`` on the page where the
application happens (an ATS, the employer's site, any other host that is not a job board or a
tracker, or a USAJOBS posting: see ``apply_rank.RESOLVED_BOARD_HOSTS``). Otherwise it is
**unsettled** and is tried again, by the nightly ``apply_links`` pass and by the console's
on-demand walk, until ``apply_links.max_attempts`` tries were made:

- no row yet, or a deferred try that stored none;
- an ``unresolved`` row;
- a ``live`` row that ends on a job board copy or a tracker (``HostTier.aggregator`` or
  ``HostTier.tracker``, such as the NLX mirrors on ``*.dejobs.org``): "a whole new domain" is
  not resolved. It stays the best known link for Apply meanwhile, and a retry replaces it only
  with a link no worse (``resolve_group(only_if_better=True)``, compared under the write lock).

Every try counts, whatever came of it (a deferred try, a failure, a group with no URL to start
from). Older login-wall rows (``needs_reresolve``) are walked again on the job page under the
same count, and by the nightly resolve stage outside it.

Tries are spaced: after the n-th try the next waits ``BACKOFF_H[n - 1]`` hours (the last entry
for any later try), counted from when the try started (``last_at`` never moves back). The
nightly pass makes at most one try per group per run. After the last try the group keeps
"Open posting" (or its board copy) and is not tried again until the user resets it on the job
page, which clears the count.

**Walk version.** Each try stores ``applylink.WALK_VERSION``. When the walk learns something
new (a commit bumps that constant), tries counted by an older walk no longer count: groups not
resolved yet get a full set of tries with the better walk, without a migration.

The count is kept after a link settles (it is history, shown on the job page), and a settled
link is never retried, so it needs no reset there.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobhunter.core.models import ApplyLink, ApplyStatus
from jobhunter.pipeline.apply_rank import HostClasses, HostTier, not_resolved_live
from jobhunter.pipeline.applylink import WALK_VERSION, is_login_link, needs_reresolve
from jobhunter.pipeline.listing import _txn, from_iso, to_iso

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
# Hours to wait after the 1st, 2nd, 3rd, 4th (and any later) try before the next one.
BACKOFF_H: tuple[float, ...] = (1.0, 4.0, 12.0, 20.0)

# SQL: an apply_link row (alias ``a``, LEFT JOINed) that may leave the group unsettled; a
# ``live`` row still needs ``unsettled`` (its final host) to tell.
MAYBE_UNSETTLED_SQL = (
    "(a.job_group_id IS NULL OR (a.chosen_by IS NULL AND a.status IN ('unresolved', 'live')))"
)
# SQL: the attempt row (alias ``t``, LEFT JOINed) leaves tries; ``due`` checks the back-off.
TRIES_LEFT_SQL = (
    "(t.attempts IS NULL OR t.attempts < :max_attempts OR t.walk_version < :walk_version)"
)


@dataclass
class Attempts:
    group_id: int
    count: int  # tries by the current walk (0 when the stored ones were by an older walk)
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


def not_resolved_kind(link: ApplyLink | None, classes: HostClasses) -> str | None:
    """``board`` or ``tracker`` for a live, automatically ranked link that ends on a job
    board copy or a tracking redirect; None otherwise."""
    if link is None or link.chosen_by is not None or link.status != ApplyStatus.live:
        return None
    if is_login_link(link):
        return None  # an older login-wall row: needs_reresolve's business (334d9eb)
    if not not_resolved_live(link.final_url, classes):
        return None
    return "tracker" if classes.tier(link.final_url) == HostTier.tracker else "board"


def board_copy(link: ApplyLink | None, classes: HostClasses) -> bool:
    """A live link that ends on a job board copy or a tracker: not resolved yet."""
    return not_resolved_kind(link, classes) is not None


def unsettled(
    conn: sqlite3.Connection, link: ApplyLink | None, classes: HostClasses | None = None
) -> bool:
    """Whether the group's link is worth another try (see the module docstring)."""
    if link is None:
        return True
    if link.chosen_by is not None or needs_reresolve(link):
        return False
    if link.status == ApplyStatus.unresolved:
        return True
    if link.status != ApplyStatus.live:
        return False
    return board_copy(link, classes or HostClasses.load(conn, link.job_group_id))


def get(conn: sqlite3.Connection, group_id: int) -> Attempts | None:
    row = conn.execute(
        "SELECT attempts, last_at, last_outcome, walk_version FROM apply_link_attempt "
        "WHERE job_group_id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        return None
    count = int(row[0]) if int(row[3]) >= WALK_VERSION else 0
    return Attempts(group_id, count, from_iso(row[1]), row[2])


def due(attempts: Attempts | None, *, now: datetime, max_attempts: int) -> bool:
    """Whether another try is allowed now: under the limit and past the back-off."""
    if attempts is None:
        return max_attempts > 0
    return attempts.count < max_attempts and _utc(now) >= attempts.next_at()


def may_retry(conn: sqlite3.Connection, group_id: int, *, now: datetime, max_attempts: int) -> bool:
    return due(get(conn, group_id), now=now, max_attempts=max_attempts)


def record(conn: sqlite3.Connection, group_id: int, *, now: datetime, outcome: str) -> None:
    """Count one try that started at ``now``. Tries by an older walk are dropped first, and
    ``last_at`` never moves back (a try that started earlier may finish later). Never raises:
    a count that cannot be written is logged and skipped."""
    try:
        with _txn(conn):
            conn.execute(
                "INSERT INTO apply_link_attempt "
                "(job_group_id, attempts, last_at, last_outcome, walk_version) "
                "VALUES (?, 1, ?, ?, ?) ON CONFLICT (job_group_id) DO UPDATE SET "
                "attempts = CASE WHEN walk_version < excluded.walk_version THEN 1 "
                "ELSE attempts + 1 END, "
                "last_at = MAX(last_at, excluded.last_at), "
                "last_outcome = excluded.last_outcome, walk_version = excluded.walk_version",
                (group_id, to_iso(now), outcome, WALK_VERSION),
            )
    except sqlite3.Error as exc:
        logger.warning("apply link for group %s: try not counted: %s", group_id, exc)


def reset(conn: sqlite3.Connection, group_id: int) -> None:
    """Forget the tries (Reset on the job page): the group gets the full count again."""
    with _txn(conn):
        conn.execute("DELETE FROM apply_link_attempt WHERE job_group_id = ?", (group_id,))


_KIND_TEXT = {
    "board": "not resolved yet (board copy)",
    "tracker": "not resolved yet (redirect not followed to the end)",
}


def summary(
    attempts: Attempts | None, *, now: datetime, max_attempts: int, kind: str | None = None
) -> str | None:
    """One muted line for the job page about an unsettled link: why it is not resolved yet
    (``kind`` from ``not_resolved_kind``), and how many tries were made."""
    count = attempts.count if attempts is not None else 0
    head = _KIND_TEXT.get(kind or "")
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
