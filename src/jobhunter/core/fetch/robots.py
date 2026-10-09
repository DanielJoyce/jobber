"""robots.txt: fetched once per scheme+host per process, enforced on every hop (specs/008, 015).

Parsed by our own RFC 9309 implementation, not ``urllib.robotparser``: the stdlib parser's
internals changed between 3.13 patch releases (entries merged per agent, 5xx semantics), and we
need wildcards and longest-match anyway.

Interpretation:
- 200 -> parsed for product token ``jobhunter`` (case-insensitive). Groups naming the same agent
  are merged (RFC 9309 2.2.1); with no group for us the ``*`` groups apply; with neither,
  everything is allowed. An empty file, or one whose directives are all commented out, allows
  everything. Blank lines do not end a group.
- Rules: ``*`` and a trailing ``$`` in paths; pattern and URL path+query are percent-normalized;
  the longest matching pattern wins and ``allow`` wins ties; an empty ``disallow`` disallows
  nothing. ``/robots.txt`` itself is always allowed. ``crawl-delay`` (non-standard) is per group.
- 404/410 and other 4xx (except 401/403) -> no robots.txt -> allow all.
- 401/403 -> disallow all.
- Network error, 5xx after retries, redirect loop -> disallow all for this run, logged.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from jobhunter.core.fetch.errors import RobotsDisallowed

log = logging.getLogger(__name__)

ROBOTS_UA = "jobhunter"

Verdict = Literal["parsed", "allow_all", "deny_all"]

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset(b"0123456789abcdefABCDEF")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _normalize(text: str) -> str:
    """Percent-normalize: decode unreserved octets, uppercase other escapes, escape non-ASCII."""
    raw = text.encode("utf-8", errors="surrogateescape")
    out: list[str] = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x25 and i + 2 < len(raw) and raw[i + 1] in _HEX and raw[i + 2] in _HEX:
            val = int(raw[i + 1 : i + 3], 16)
            out.append(chr(val) if val in _UNRESERVED else f"%{val:02X}")
            i += 3
            continue
        if b <= 0x20 or b >= 0x7F:
            out.append(f"%{b:02X}")
        else:
            out.append(chr(b))
        i += 1
    return "".join(out)


@dataclass
class _Rule:
    allow: bool
    raw: str
    pattern: str  # normalized
    regex: re.Pattern[str]

    @classmethod
    def build(cls, allow: bool, raw: str) -> _Rule:
        pattern = _normalize(raw)
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = ".*".join(re.escape(part) for part in body.split("*"))
        return cls(allow, raw, pattern, re.compile(rx + ("$" if anchored else ""), re.DOTALL))


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    delay: float | None = None


@dataclass
class RobotsFile:
    groups: list[_Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)

    def groups_for(self, token: str) -> list[_Group]:
        """Groups naming ``token`` (merged by the caller), else the ``*`` groups, else none."""
        token = token.lower()
        named = [g for g in self.groups if token in g.agents]
        return named or [g for g in self.groups if "*" in g.agents]

    def rules_for(self, token: str) -> list[_Rule]:
        return [r for g in self.groups_for(token) for r in g.rules]

    def delay_for(self, token: str) -> float | None:
        return next((g.delay for g in self.groups_for(token) if g.delay is not None), None)

    def allows(self, token: str, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        target = _normalize(path + ("?" + parts.query if parts.query else ""))
        best: _Rule | None = None
        for rule in self.rules_for(token):
            if not rule.pattern or not rule.regex.match(target):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def parse_robots(text: str) -> RobotsFile:
    parsed = RobotsFile()
    group: _Group | None = None
    collecting_agents = False
    for line in text.lstrip("\ufeff").splitlines():
        line = line.split("#", 1)[0].strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if group is None or not collecting_agents:
                group = _Group()
                parsed.groups.append(group)
            group.agents.append(value.split("/", 1)[0].strip().lower())
            collecting_agents = True
        elif key in ("allow", "disallow"):
            collecting_agents = False
            if group is not None and value:
                group.rules.append(_Rule.build(key == "allow", value))
        elif key == "crawl-delay":
            collecting_agents = False
            if group is not None and group.delay is None:
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    group.delay = delay
        elif key == "sitemap":
            if value:
                parsed.sitemaps.append(value)
    return parsed


@dataclass
class RobotsRules:
    origin: str
    verdict: Verdict
    reason: str
    parsed: RobotsFile | None = field(default=None, repr=False)

    def crawl_delay(self) -> float | None:
        """Crawl-delay for our product token, falling back to ``*``. None when unspecified."""
        if self.verdict != "parsed" or self.parsed is None:
            return None
        return self.parsed.delay_for(ROBOTS_UA)

    def allows(self, url: str) -> bool:
        if self.verdict == "allow_all":
            return True
        if self.verdict == "deny_all" or self.parsed is None:
            return False
        return self.parsed.allows(ROBOTS_UA, url)

    def disallowed_paths_for_us(self) -> list[str]:
        """Non-empty Disallow patterns of the (merged) group that applies to us."""
        if self.verdict != "parsed" or self.parsed is None:
            return []
        return [r.raw for r in self.parsed.rules_for(ROBOTS_UA) if not r.allow]

    def sitemaps(self) -> list[str]:
        return list(self.parsed.sitemaps) if self.parsed is not None else []


def rules_from_response(origin: str, status: int, text: str) -> RobotsRules:
    if status in (401, 403):
        return RobotsRules(origin, "deny_all", f"robots.txt returned {status}")
    if 400 <= status < 500:
        return RobotsRules(origin, "allow_all", f"no robots.txt ({status})")
    if status != 200:
        return RobotsRules(origin, "deny_all", f"robots.txt returned {status}")
    if not text.strip():
        return RobotsRules(origin, "allow_all", "empty robots.txt")
    return RobotsRules(origin, "parsed", "robots.txt", parse_robots(text))


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

    def cached(self, url: str) -> RobotsRules | None:
        """Rules already fetched for ``url``'s origin. Never fetches (safe inside a request)."""
        with self._lock:
            return self._rules.get(origin_of(url))

    def reset(self) -> None:
        with self._lock:
            self._rules.clear()
            self._origin_locks.clear()
            self._warned.clear()


ROBOTS = RobotsPolicy()
