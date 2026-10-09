"""robots.txt: fetched once per scheme+host per process, enforced on every hop (specs/008, 015).

Interpretation:
- 200 → parsed with ``urllib.robotparser`` for product token ``jobhunter``. An empty file, or one
  whose directives are all commented out, allows everything.
- 404/410 and other 4xx (except 401/403) → no robots.txt → allow all.
- 401/403 → disallow all (the long-standing robotparser convention).
- Network error, 5xx after retries, redirect loop → disallow all for this run, logged.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from jobhunter.core.fetch.errors import RobotsDisallowed

log = logging.getLogger(__name__)

ROBOTS_UA = "jobhunter"

Verdict = Literal["parsed", "allow_all", "deny_all"]


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


@dataclass
class RobotsRules:
    origin: str
    verdict: Verdict
    reason: str
    parser: RobotFileParser | None = field(default=None, repr=False)

    def allows(self, url: str) -> bool:
        if self.verdict == "allow_all":
            return True
        if self.verdict == "deny_all" or self.parser is None:
            return False
        return self.parser.can_fetch(ROBOTS_UA, url)


def rules_from_response(origin: str, status: int, text: str) -> RobotsRules:
    if status in (401, 403):
        return RobotsRules(origin, "deny_all", f"robots.txt returned {status}")
    if 400 <= status < 500:
        return RobotsRules(origin, "allow_all", f"no robots.txt ({status})")
    if status != 200:
        return RobotsRules(origin, "deny_all", f"robots.txt returned {status}")
    if not text.strip():
        return RobotsRules(origin, "allow_all", "empty robots.txt")
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return RobotsRules(origin, "parsed", "robots.txt", parser)


class RobotsPolicy:
    """Process-wide rules cache keyed by origin. One fetch per origin, even under threads."""

    def __init__(self) -> None:
        self._rules: dict[str, RobotsRules] = {}
        self._origin_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._warned: set[str] = set()

    def rules_for(self, url: str, fetch: Callable[[str], RobotsRules]) -> RobotsRules:
        origin = origin_of(url)
        with self._lock:
            cached = self._rules.get(origin)
            if cached is not None:
                return cached
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:
            cached = self._rules.get(origin)
            if cached is None:
                cached = fetch(origin)
                with self._lock:
                    self._rules[origin] = cached
        return cached

    def check(
        self,
        url: str,
        fetch: Callable[[str], RobotsRules],
        *,
        enforce: bool,
        why_not_enforced: str = "fetch.respect_robots is false",
    ) -> bool:
        """Return True if allowed. Raise RobotsDisallowed if disallowed and ``enforce``.

        When not enforcing, a disallowed URL is still computed, and a warning is logged once
        per origin.
        """
        rules = self.rules_for(url, fetch)
        if rules.allows(url):
            return True
        if enforce:
            raise RobotsDisallowed(url, reason=f"{rules.reason} disallows {ROBOTS_UA}")
        with self._lock:
            first = rules.origin not in self._warned
            self._warned.add(rules.origin)
        if first:
            log.warning(
                "robots.txt at %s disallows %s; proceeding because %s",
                rules.origin,
                ROBOTS_UA,
                why_not_enforced,
            )
        return False

    def reset(self) -> None:
        with self._lock:
            self._rules.clear()
            self._origin_locks.clear()
            self._warned.clear()


ROBOTS = RobotsPolicy()
