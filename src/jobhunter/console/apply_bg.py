"""Apply-link network work off the request thread (bug e0d047b).

Resolving a group walks up to ``applylink.MAX_FETCH_STARTS`` start URLs, each hop rate-limited
per host and robots-checked, so it can take a minute on slow sites. A click on Apply, or the job
page's apply-button fragment, must never wait that long: the work runs in a background thread
with its own database connection, and the request waits only a short budget for it. When the
budget runs out the request answers with the best link already stored, and the thread finishes
and saves its result for the next look.

One job per group at a time (single flight): the page's fragment and a click on Apply for the
same group share one walk instead of two that queue behind each other on the per-host limiter.
At most ``MAX_IN_FLIGHT`` jobs run at once; past that a request answers with the stored link
and starts nothing. A walk that ends without an answer (an older login-wall row whose links
lead nowhere better) is not walked again for that group for ``WALK_BACKOFF_S``.

``log_click`` is never allowed to fail a redirect: a write that cannot get the database lock
quickly is retried in the background.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from jobhunter.console import detail
from jobhunter.pipeline import applylink

logger = logging.getLogger(__name__)

# How long a request waits for a background walk (resolve, re-resolve: many hosts) before
# answering without it.
WAIT_S = 2.0
# A click's re-verify is at most two requests to one host, but the first request to a host
# after a console start also fetches robots.txt and waits a full rate interval: give it longer.
REVERIFY_WAIT_S = 7.0
# Background jobs in flight at once (one per group).
MAX_IN_FLIGHT = 8
# A walk that ended without an answer is not repeated for the same group sooner than this.
WALK_BACKOFF_S = 24 * 3600.0
# The click's own write waits this long for the lock, then hands the write to a thread.
CLICK_BUSY_MS = 500
CLICK_RETRIES = 5
CLICK_RETRY_GAP_S = 2.0

# A walk returns True when it settled the group's link and False when it did not (backed off);
# None (a re-verify) records nothing.
Work = Callable[[sqlite3.Connection, applylink.CtxFactory], bool | None]
Outcome = Literal["done", "pending", "skipped"]
ConnFactory = Callable[[], sqlite3.Connection]
CtxFactoryFor = Callable[[sqlite3.Connection], applylink.CtxFactory]


class ApplyLinkWorker:
    """Single-flight background apply-link work, one thread per group in flight."""

    def __init__(
        self,
        conn_factory: ConnFactory,
        ctx_factory_for: CtxFactoryFor,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn_factory = conn_factory
        self._ctx_factory_for = ctx_factory_for
        self._clock = clock
        self._lock = threading.Lock()
        self._running: dict[int, threading.Event] = {}
        self._unsettled: dict[int, float] = {}  # group -> when its last walk found no answer

    def run(
        self, group_id: int, work: Work, *, wait_s: float | None = None, walk: bool = False
    ) -> Outcome:
        """Start ``work(conn, ctx_factory)`` for the group, or join the one already running,
        and wait up to ``wait_s``. ``done``: it finished and the caller reads the stored row;
        ``pending``: still running; ``skipped``: nothing was started (the cap is reached, a
        ``walk`` is backed off, or no thread could start) and the stored row is the answer."""
        with self._lock:
            done = self._running.get(group_id)
            if done is None:
                if walk and self._backed_off(group_id):
                    return "skipped"
                if len(self._running) >= MAX_IN_FLIGHT:
                    logger.info(
                        "apply link for group %s: %d jobs in flight", group_id, MAX_IN_FLIGHT
                    )
                    return "skipped"
                done = threading.Event()
                self._running[group_id] = done
                try:
                    threading.Thread(
                        target=self._body,
                        args=(group_id, work, done),
                        name=f"apply-link-{group_id}",
                        daemon=True,
                    ).start()
                except Exception:
                    del self._running[group_id]
                    done.set()
                    logger.warning(
                        "apply link for group %s: no thread started", group_id, exc_info=True
                    )
                    return "skipped"
        return "done" if done.wait(WAIT_S if wait_s is None else wait_s) else "pending"

    def _backed_off(self, group_id: int) -> bool:
        at = self._unsettled.get(group_id)
        return at is not None and self._clock() - at < WALK_BACKOFF_S

    def in_flight(self) -> int:
        with self._lock:
            return len(self._running)

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no work is in flight (tests, shutdown). True when idle."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                pending = list(self._running.values())
            if not pending:
                return True
            left = deadline - time.monotonic()
            if left <= 0 or not pending[0].wait(left):
                return False

    def _body(self, group_id: int, work: Work, done: threading.Event) -> None:
        try:
            conn = self._conn_factory()
            try:
                settled = work(conn, self._ctx_factory_for(conn))
            finally:
                conn.close()
            with self._lock:
                if settled is False:
                    self._unsettled[group_id] = self._clock()
                elif settled is True:
                    self._unsettled.pop(group_id, None)
        except Exception:
            logger.warning(
                "apply link for group %s: background work failed", group_id, exc_info=True
            )
        finally:
            with self._lock:
                self._running.pop(group_id, None)
            done.set()


def log_click_safely(
    conn: sqlite3.Connection,
    conn_factory: ConnFactory,
    group_id: int,
    final_url: str,
    outcome: str,
    now: datetime,
) -> None:
    """``detail.log_click`` that never raises: a short wait for the lock on the request's
    connection, then retries in a background thread (a nightly run may be writing)."""
    old = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        conn.execute(f"PRAGMA busy_timeout = {CLICK_BUSY_MS}")
        detail.log_click(conn, group_id, final_url, outcome, now)
        return
    except sqlite3.OperationalError as exc:  # locked or busy: a run is writing
        logger.warning("apply click for group %s: %s; retrying in the background", group_id, exc)
    except sqlite3.Error:
        logger.error("apply click for group %s was not recorded", group_id, exc_info=True)
        return
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA busy_timeout = {int(old)}")

    def retry() -> None:
        for attempt in range(CLICK_RETRIES):
            try:
                c = conn_factory()
                try:
                    detail.log_click(c, group_id, final_url, outcome, now)
                    return
                finally:
                    c.close()
            except Exception as exc:
                logger.warning(
                    "apply click for group %s: attempt %d failed: %s", group_id, attempt + 2, exc
                )
            time.sleep(CLICK_RETRY_GAP_S)
        logger.error("apply click for group %s was not recorded", group_id)

    try:
        threading.Thread(target=retry, name=f"apply-click-{group_id}", daemon=True).start()
    except Exception:
        logger.error("apply click for group %s was not recorded", group_id, exc_info=True)
