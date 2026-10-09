"""Per-host minimum-interval limiter and the process-wide concurrency cap (specs/008#defaults)."""

from __future__ import annotations

import random
import threading
from collections.abc import Callable

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class HostRateLimiter:
    """Reserve-then-sleep slots per host: the next request may start ``interval`` after the last.

    Each caller supplies its own interval, so two contexts with different rate limits that hit
    the same host still space out against each other.
    """

    def __init__(self) -> None:
        self._next: dict[str, float] = {}
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
    ) -> float:
        """Block until ``host`` may be hit again. Returns the seconds waited."""
        if jitter:
            interval *= (rng or random.Random()).uniform(0.75, 1.25)
        with self._lock:
            now = clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + interval
        wait = start - now
        if wait > 0:
            sleep(wait)
        return max(wait, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._next.clear()


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
