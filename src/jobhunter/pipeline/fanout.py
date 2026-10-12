"""Bounded fan-out for the list and resolve stages (bug 50320dc).

Scheduling only: which work runs where and in what order results are handed back. What a source
fetches, and which postings it skips, is the caller's ``work`` function.

- At most ``workers`` items run at once, each on a pool thread.
- ``on_done`` runs on the calling thread, in item order, so whatever it writes lands in the
  same order as a serial run (deterministic row ids). An item finished early waits in memory
  for the ones before it.
- An item for which ``inline(item)`` is true is a barrier: every earlier item finishes and is
  handed back first, then it runs on the calling thread with no other work in flight, and only
  then are later items started. Adapters that read or write the database themselves run this
  way.
- ``workers <= 1`` runs everything inline, one at a time, exactly as before the fan-out.
- One item raising never stops the others: its exception is handed to ``on_done``.
- Ctrl-C (or any exception from ``on_done``) stops the fan-out: queued items are cancelled,
  running ones see :class:`FetchCancelled` at their next sleep or check, and the exception
  propagates once the pool threads have exited.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass


class FetchCancelled(BaseException):
    """The run is stopping (Ctrl-C). A BaseException so adapters' ``except Exception`` and the
    per-posting error handling never swallow it."""


class Stopper:
    """Shared stop flag. ``sleep`` is a FetchContext sleep that wakes up when the run stops."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def stop(self) -> None:
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise FetchCancelled

    def sleep(self, seconds: float) -> None:
        if self._event.wait(max(0.0, seconds)):
            raise FetchCancelled


@dataclass
class Outcome[R]:
    value: R | None = None
    error: Exception | None = None


def _call[T, R](work: Callable[[T], R], item: T) -> Outcome[R]:
    try:
        return Outcome(value=work(item))
    except Exception as exc:  # one item never stops the others
        return Outcome(error=exc)


def run_ordered[T, R](
    items: Iterable[T],
    work: Callable[[T], R],
    on_done: Callable[[T, Outcome[R]], None],
    *,
    workers: int,
    stopper: Stopper,
    inline: Callable[[T], bool] = lambda _item: False,
) -> None:
    """Run ``work`` over ``items`` with at most ``workers`` at once; see the module docstring."""
    items = list(items)
    if workers <= 1:
        for item in items:
            stopper.check()
            on_done(item, _call(work, item))
        return

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobhunter-fetch")
    futures: dict[int, Future[Outcome[R]]] = {}
    submitted = handed = 0
    try:
        while handed < len(items):
            while submitted < len(items) and not inline(items[submitted]):
                futures[submitted] = pool.submit(_call, work, items[submitted])
                submitted += 1
            while handed < submitted:
                outcome = futures.pop(handed).result()
                on_done(items[handed], outcome)
                handed += 1
            if submitted < len(items):  # a barrier: nothing else is in flight now
                stopper.check()
                item = items[submitted]
                submitted += 1
                on_done(item, _call(work, item))
                handed += 1
    except BaseException:
        stopper.stop()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
