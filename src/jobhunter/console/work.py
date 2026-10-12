"""The console's in-process work registry (specs/019 Addendum B, S12b).

A restart must not cut off paid or long work. Every place the console starts such work enters
this registry first and leaves it in a ``finally``: the re-score thread
(``RescoreManager.start``), each extension Score now thread, the in-request Score now on the job
and packet pages, and drafting (``generator.generate``).

``POST /config/restart`` calls :meth:`WorkRegistry.begin_restart` under the same lock: it sets
``restarting`` only when nothing is registered, and from then on :meth:`enter` raises
:class:`Restarting`, which every entry point answers with 503 ("console restarting"). That closes
the window between the check and the shutdown. After the server stops, the console command
calls :meth:`wait_idle` (a bounded join) before it re-execs or exits.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from starlette.responses import Response

RESTARTING_TEXT = "console restarting; try again in a moment"
# On a 503 for "console restarting": app.js lets htmx swap such a response so the message shows.
RESTARTING_HEADER = "X-Jobhunter-Restarting"


class Restarting(Exception):
    """Raised by :meth:`WorkRegistry.enter` once a restart was accepted."""

    def __init__(self) -> None:
        super().__init__(RESTARTING_TEXT)


class WorkRegistry:
    """A set of named entries under one lock, plus the ``restarting`` flag."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._entries: dict[int, str] = {}
        self._ids = itertools.count(1)
        self._restarting = False
        self._clock = clock

    @property
    def restarting(self) -> bool:
        return self._restarting

    def enter(self, label: str) -> int:
        """Register work named ``label``; the returned token goes to :meth:`leave`."""
        with self._cond:
            if self._restarting:
                raise Restarting()
            token = next(self._ids)
            self._entries[token] = label
            return token

    def leave(self, token: int) -> None:
        with self._cond:
            self._entries.pop(token, None)
            self._cond.notify_all()

    @contextmanager
    def running(self, label: str) -> Iterator[int]:
        """``with registry.running("draft"):`` enter, run, and always leave."""
        token = self.enter(label)
        try:
            yield token
        finally:
            self.leave(token)

    def labels(self) -> list[str]:
        """What is running now, in the order it started."""
        with self._cond:
            return [self._entries[k] for k in sorted(self._entries)]

    def begin_restart(self) -> list[str]:
        """Set ``restarting`` when nothing runs and return ``[]``; else return what runs and
        leave the flag unset."""
        with self._cond:
            if self._entries:
                return [self._entries[k] for k in sorted(self._entries)]
            self._restarting = True
            return []

    def cancel_restart(self) -> None:
        """The restart could not go ahead after all (the server is not ours to stop)."""
        with self._cond:
            self._restarting = False

    def wait_idle(self, timeout: float) -> list[str]:
        """Wait up to ``timeout`` seconds for every entry to leave; return what is left."""
        deadline = self._clock() + timeout
        with self._cond:
            while self._entries:
                left = deadline - self._clock()
                if left <= 0:
                    break
                self._cond.wait(min(left, 1.0))
            return [self._entries[k] for k in sorted(self._entries)]


def refused[R: Response](response: R) -> R:
    """Turn a rendered response into the 503 every entry point answers while restarting."""
    response.status_code = 503
    response.headers[RESTARTING_HEADER] = "1"
    return response
