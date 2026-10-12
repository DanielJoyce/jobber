"""Per-host minimum-interval limiter and the process-wide concurrency cap (specs/008#defaults)."""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)
LONG_WAIT_S = 10.0  # a limiter wait longer than this is worth a log line

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class HostHeld(Exception):
    """The host asked for a pause longer than the caller will wait (``max_hold``)."""

    def __init__(self, host: str, remaining: float) -> None:
        super().__init__(f"host {host} asked for a {remaining:.0f}s pause (Retry-After)")
        self.host = host
        self.remaining = remaining


class HostRateLimiter:
    """Reserve-then-sleep slots per host: the next request may start ``interval`` after the last.

    Each caller supplies its own interval, so two contexts with different rate limits that hit
    the same host still space out against each other.
    """

    def __init__(self) -> None:
        self._next: dict[str, float] = {}
        # Host -> clock time before which nobody may hit it (429 / Retry-After). Checked again
        # after every wait, so a request already waiting when the hold lands does not fire
        # inside it.
        self._held: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(
        self,
        host: str,
        interval: float,
        *,
        clock: Clock,
        sleep: Sleep,
        jitter: bool = False,
        rng: random.Random | None = None,
        floor: float = 0.0,
        max_hold: float | None = None,
    ) -> float:
        """Block until ``host`` may be hit again. Returns the seconds waited.

        ``floor`` is a minimum interval that jitter never goes below (robots Crawl-delay).
        ``max_hold``: raise :class:`HostHeld` instead of waiting out a host hold longer than
        this, checked before the first wait and again after every wake-up.
        """
        if jitter:
            interval *= (rng or random.Random()).uniform(0.75, 1.25)
        interval = max(interval, floor)
        waited = 0.0
        while True:
            with self._lock:
                now = clock()
                held = self._held.get(host, now)
                if max_hold is not None and held - now > max_hold:
                    raise HostHeld(host, held - now)
                start = max(now, self._next.get(host, now), held)
                self._next[host] = start + interval
            wait = start - now
            if wait > LONG_WAIT_S:
                log.info("waiting %.0fs for %s (per-host request spacing)", wait, host)
            if wait > 0:
                sleep(wait)
                waited += wait
            with self._lock:
                # A hold placed while this caller slept: take a new slot after it.
                if self._held.get(host, float("-inf")) <= clock():
                    return waited

    def remaining_hold(self, host: str, *, clock: Clock) -> float:
        """Seconds left on ``host``'s hold (0 when none)."""
        with self._lock:
            return max(0.0, self._held.get(host, float("-inf")) - clock())

    def hold(self, host: str, seconds: float, *, clock: Clock) -> None:
        """Nobody may hit ``host`` for ``seconds`` from now (a 429 or Retry-After). Shared by
        every context on the host; never shortens a later slot already reserved."""
        with self._lock:
            until = clock() + max(0.0, seconds)
            if until > self._held.get(host, float("-inf")):
                self._held[host] = until

    def reset(self) -> None:
        with self._lock:
            self._next.clear()
            self._held.clear()


_SHARED_LIMITER = HostRateLimiter()
_GLOBAL_SEM: threading.BoundedSemaphore | None = None
_GLOBAL_SEM_LOCK = threading.Lock()


def shared_limiter() -> HostRateLimiter:
    return _SHARED_LIMITER


def global_semaphore(size: int) -> threading.BoundedSemaphore:
    """Process-wide cap on in-flight requests. Sized by the first caller; later sizes ignored."""
    global _GLOBAL_SEM
    with _GLOBAL_SEM_LOCK:
        if _GLOBAL_SEM is None:
            _GLOBAL_SEM = threading.BoundedSemaphore(max(1, size))
        return _GLOBAL_SEM


def reset() -> None:
    global _GLOBAL_SEM
    _SHARED_LIMITER.reset()
    with _GLOBAL_SEM_LOCK:
        _GLOBAL_SEM = None
