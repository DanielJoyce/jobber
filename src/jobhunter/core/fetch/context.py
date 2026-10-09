"""FetchContext: the only network entry point (specs/003-sources-and-adapters.md#adapter-contract).

Every request, on every redirect hop, goes through the same ladder:

1. source policy (blocked/disabled → SourceBlocked, before any I/O)
2. robots.txt for the hop's origin (RobotsDisallowed)
3. ttl short-circuit / conditional headers from fetch_log (GET only)
4. per-host rate limit, global and per-source concurrency caps
5. send; 429/5xx/connection errors retried with backoff (TransientFetchError when exhausted)
6. body into the content cache, one fetch_log row
7. 401/403 → AccessDenied (no retry); other 4xx returned to the caller
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from jobhunter.config import Settings, resolve_path
from jobhunter.core.fetch import ratelimit
from jobhunter.core.fetch.cache import ContentCache
from jobhunter.core.fetch.errors import (
    AccessDenied,
    FetchError,
    SourceBlocked,
    TierError,
    TransientFetchError,
)
from jobhunter.core.fetch.response import CachedResponse
from jobhunter.core.fetch.robots import ROBOTS, RobotsRules, origin_of, rules_from_response
from jobhunter.core.models import Policy, RateLimit, SourceClass, SourceRow, Tier

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

MAX_REDIRECTS = 10
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 120.0
# A Retry-After longer than this is a "come back tomorrow"; give up rather than sit on it.
MAX_RETRY_AFTER_S = 600.0
ADHOC_SOURCE_KEY = "_adhoc"


def request_hash(method: str, url: str, body: bytes = b"") -> str:
    """sha256(method|url-without-query|sorted-query|body): the fetch_log cache key."""
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    h = hashlib.sha256()
    h.update(f"{method.upper()}|{base}|{query}|".encode())
    h.update(body)
    return h.hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - _now()).total_seconds())


def _host_key(url: httpx.URL) -> str:
    return url.netloc.decode("ascii").lower()


class FetchContext:
    """Per-source network access: cache, fetch_log, robots, rate limits, retries, sessions."""

    def __init__(
        self,
        source: SourceRow,
        settings: Settings,
        conn: sqlite3.Connection,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.source = source
        self.settings = settings
        self.conn = conn
        self.cache = ContentCache(resolve_path(settings.paths.cache_dir))
        self.tokens: dict[str, str] = {}
        self._transport = transport
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._rng = rng or random.Random()
        # Times from an injected clock are not comparable with the process-wide limiter's.
        self._limiter = ratelimit.shared_limiter() if clock is None else ratelimit.HostRateLimiter()
        self.rate_limit = source.rate_limit or RateLimit(
            rps=settings.fetch.default_rps, concurrency=1, jitter=True
        )
        self._source_sem = threading.BoundedSemaphore(self.rate_limit.concurrency)
        self._db_lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._log_key = self._registered_key(source.key)
        self._pw: Any = None
        self._browser: Any = None
        self._page: Page | None = None

    # ─── public API ────────────────────────────────────────────────────────

    def policy(self) -> Policy:
        return self.source.policy

    def session(self) -> httpx.Client:
        """The per-source client: cookie jar, identifying User-Agent, configured timeout.

        Redirects are never followed by httpx itself; FetchContext walks them hop by hop so each
        hop is robots-checked, rate-limited and logged.
        """
        if self._client is None:
            self._client = httpx.Client(
                transport=self._transport,
                headers={"User-Agent": self.settings.user_agent},
                timeout=self.settings.fetch.timeout_s,
                follow_redirects=False,
            )
        return self._client

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ttl: float | timedelta | None = None,
        follow_redirects: bool = True,
    ) -> CachedResponse:
        return self._request(
            "GET", url, params=params, headers=headers, ttl=ttl, follow_redirects=follow_redirects
        )

    def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> CachedResponse:
        return self._request(
            "POST", url, data=data, json=json, headers=headers, follow_redirects=follow_redirects
        )

    def page(self) -> Page:
        """Lazy Playwright page; only for ``tier: browser`` sources.

        Navigations are robots-checked and rate-limited through a route handler, so the browser
        obeys the same rules as get().
        """
        if self.source.tier != Tier.browser:
            raise TierError(f"source {self.source.key!r} is tier {self.source.tier}, not browser")
        self._check_policy()
        if self._page is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:  # pragma: no cover - optional extra
                raise TierError("playwright is not installed; uv sync --extra browser") from exc
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            context = self._browser.new_context(user_agent=self.settings.user_agent)
            context.route("**/*", self._browser_route)
            self._page = context.new_page()
        return self._page

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
        self._page = None

    def __enter__(self) -> FetchContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ─── request ladder ────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        ttl: float | timedelta | None = None,
        follow_redirects: bool = True,
        check_robots: bool = True,
        raise_on_denied: bool = True,
    ) -> CachedResponse:
        self._check_policy()
        client = self.session()
        request = client.build_request(
            method, url, params=params, headers=headers, data=data, json=json
        )
        ttl_s = ttl.total_seconds() if isinstance(ttl, timedelta) else ttl
        hops = 0
        while True:
            if check_robots:
                self._check_robots(str(request.url))
            result, response = self._fetch_one(request, ttl_s)
            if response is None:
                result.url = url
                return result
            if raise_on_denied and response.status_code in (401, 403):
                raise AccessDenied(str(request.url), response.status_code)
            if follow_redirects and response.next_request is not None:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise FetchError(f"more than {MAX_REDIRECTS} redirects", url=url)
                request = response.next_request
                continue
            result.url = url
            return result

    def _fetch_one(
        self, request: httpx.Request, ttl: float | None
    ) -> tuple[CachedResponse, httpx.Response | None]:
        """One hop. Returns (result, raw response); raw is None when served from cache."""
        url = str(request.url)
        rhash = request_hash(request.method, url, request.read())
        prior = self._latest_ok(rhash) if request.method == "GET" else None

        if prior is not None and ttl is not None:
            fetched = datetime.fromisoformat(prior["fetched_at"])
            if (_now() - fetched).total_seconds() < ttl:
                return self._from_prior(url, prior, {}, fetched), None

        if prior is not None:
            if prior["etag"]:
                request.headers["If-None-Match"] = prior["etag"]
            if prior["last_modified"]:
                request.headers["If-Modified-Since"] = prior["last_modified"]

        response = self._exchange(request, rhash)
        headers = {k.lower(): v for k, v in response.headers.items()}

        if response.status_code == 304 and prior is not None:
            fetched_at = _now()
            self._log(
                url,
                request.method,
                rhash,
                prior["content_hash"],
                304,
                headers.get("etag") or prior["etag"],
                headers.get("last-modified") or prior["last_modified"],
                fetched_at,
                prior["bytes"],
                from_cache=True,
            )
            return self._from_prior(url, prior, headers, fetched_at), None

        body = response.content
        digest = self.cache.put(body)
        fetched_at = _now()
        self._log(
            url,
            request.method,
            rhash,
            digest,
            response.status_code,
            headers.get("etag"),
            headers.get("last-modified"),
            fetched_at,
            len(body),
            from_cache=False,
        )
        result = CachedResponse(
            url=url,
            final_url=url,
            status=response.status_code,
            headers=headers,
            content_hash=digest,
            from_cache=False,
            fetched_at=fetched_at,
            _cache=self.cache,
            _content=body,
        )
        return result, response

    def _from_prior(
        self, url: str, prior: sqlite3.Row, headers: dict[str, str], fetched_at: datetime
    ) -> CachedResponse:
        merged = dict(headers)
        if prior["etag"]:
            merged.setdefault("etag", prior["etag"])
        if prior["last_modified"]:
            merged.setdefault("last-modified", prior["last_modified"])
        return CachedResponse(
            url=url,
            final_url=url,
            status=200,
            headers=merged,
            content_hash=prior["content_hash"],
            from_cache=True,
            fetched_at=fetched_at,
            _cache=self.cache,
        )

    def _exchange(self, request: httpx.Request, rhash: str) -> httpx.Response:
        """Send with rate limiting and the retry ladder. Logs every failed attempt."""
        client = self.session()
        url = str(request.url)
        attempts = self.settings.fetch.max_retries + 1
        interval = 1.0 / self.rate_limit.rps
        gsem = ratelimit.global_semaphore(self.settings.fetch.global_concurrency)
        detail = ""
        status: int | None = None
        for attempt in range(attempts):
            self._limiter.acquire(
                _host_key(request.url),
                interval,
                clock=self._clock,
                sleep=self._sleep,
                jitter=self.rate_limit.jitter,
                rng=self._rng,
                floor=self._crawl_delay(str(request.url)),
            )
            retry_after: float | None = None
            with gsem, self._source_sem:
                try:
                    response = client.send(request)
                except httpx.TransportError as exc:
                    status, detail = None, f"{type(exc).__name__}: {exc}"
                    self._log(url, request.method, rhash, None, None, None, None, _now(), None)
                else:
                    status = response.status_code
                    if status != 429 and status < 500:
                        return response
                    detail = f"HTTP {status}"
                    body = response.content
                    self._log(
                        url,
                        request.method,
                        rhash,
                        self.cache.put(body),
                        status,
                        None,
                        None,
                        _now(),
                        len(body),
                    )
                    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
            if attempt == attempts - 1:
                break
            if retry_after is not None and retry_after > MAX_RETRY_AFTER_S:
                detail += f" (Retry-After {retry_after:.0f}s exceeds {MAX_RETRY_AFTER_S:.0f}s)"
                break
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            log.info("retrying %s in %.1fs after %s", url, delay, detail)
            self._sleep(delay)
        raise TransientFetchError(url, f"{detail} after {attempt + 1} attempt(s)", status=status)

    def _crawl_delay(self, url: str) -> float:
        """Robots Crawl-delay for the URL's origin, if its rules are already cached; else 0.

        The per-host interval is max(1/rps, crawl_delay). Rules are only read from the cache so
        that fetching robots.txt itself never recurses.
        """
        rules = ROBOTS.cached(url)
        delay = rules.crawl_delay() if rules is not None else None
        return delay or 0.0

    def _backoff(self, attempt: int) -> float:
        base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt))
        return base * self._rng.uniform(0.75, 1.25)

    # ─── policy and robots ─────────────────────────────────────────────────

    def _check_policy(self) -> None:
        if self.source.policy in (Policy.blocked, Policy.disabled):
            raise SourceBlocked(self.source.key, self.source.policy.value)

    def _sanctioned_api_host(self, url: str) -> bool:
        """USAJOBS-style sanctioned API (specs/008): robots governs crawlers, the key is consent."""
        return (
            self.source.tier == Tier.api
            and self.source.config.get("sanctioned_api") is True
            and origin_of(url) == origin_of(self.source.entry)
        )

    def _check_robots(self, url: str) -> None:
        if self._sanctioned_api_host(url):
            ROBOTS.check(
                url,
                self._fetch_robots,
                enforce=False,
                why_not_enforced=f"source {self.source.key} is a sanctioned API",
            )
            return
        ROBOTS.check(url, self._fetch_robots, enforce=self.settings.fetch.respect_robots)

    def _fetch_robots(self, origin: str) -> RobotsRules:
        robots_url = f"{origin}/robots.txt"
        try:
            resp = self._request("GET", robots_url, check_robots=False, raise_on_denied=False)
        except (FetchError, httpx.HTTPError) as exc:
            log.warning("robots.txt fetch failed for %s (%s); disallowing this run", origin, exc)
            return RobotsRules(origin, "deny_all", f"robots.txt unreachable ({exc})")
        if 300 <= resp.status < 400:
            reason = f"robots.txt redirect not followed ({resp.status})"
            return RobotsRules(origin, "deny_all", reason)
        return rules_from_response(origin, resp.status, resp.text)

    def _browser_route(self, route: Any) -> None:  # pragma: no cover - needs playwright
        request = route.request
        if request.is_navigation_request():
            try:
                self._check_policy()
                self._check_robots(request.url)
            except FetchError as exc:
                log.warning("browser navigation refused: %s", exc)
                route.abort("blockedbyclient")
                return
            self._limiter.acquire(
                _host_key(httpx.URL(request.url)),
                1.0 / self.rate_limit.rps,
                clock=self._clock,
                sleep=self._sleep,
                jitter=self.rate_limit.jitter,
                rng=self._rng,
                floor=self._crawl_delay(request.url),
            )
        route.continue_()

    # ─── fetch_log ─────────────────────────────────────────────────────────

    def _registered_key(self, key: str) -> str | None:
        """fetch_log.source_key references source(key); ad-hoc/unregistered sources log NULL."""
        row = self.conn.execute("SELECT 1 FROM source WHERE key = ?", (key,)).fetchone()
        return key if row else None

    def _latest_ok(self, rhash: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT content_hash, etag, last_modified, fetched_at, bytes FROM fetch_log "
            "WHERE request_hash = ? AND content_hash IS NOT NULL AND http_status IN (200, 304) "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (rhash,),
        ).fetchone()
        if row is None or not self.cache.has(row["content_hash"]):
            return None
        return row

    def _log(
        self,
        url: str,
        method: str,
        rhash: str,
        digest: str | None,
        status: int | None,
        etag: str | None,
        last_modified: str | None,
        fetched_at: datetime,
        size: int | None,
        *,
        from_cache: bool = False,
    ) -> None:
        with self._db_lock:
            self.conn.execute(
                "INSERT INTO fetch_log (source_key, url, method, request_hash, content_hash, "
                "http_status, etag, last_modified, fetched_at, bytes, from_cache) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._log_key,
                    url,
                    method,
                    rhash,
                    digest,
                    status,
                    etag,
                    last_modified,
                    fetched_at.isoformat(),
                    size,
                    int(from_cache),
                ),
            )


def fetch_context_for_url(
    settings: Settings,
    conn: sqlite3.Connection,
    url: str = "https://adhoc.invalid/",
    **kwargs: Any,
) -> FetchContext:
    """A context for hosts not tied to a registry source (apply-link hops, specs/015).

    Default rate limit (jittered), robots-checked like any other; fetch_log.source_key is NULL.
    """
    source = SourceRow(
        key=ADHOC_SOURCE_KEY,
        class_=SourceClass.A,
        name="ad-hoc",
        family="adhoc",
        tier=Tier.http,
        entry=url,
    )
    return FetchContext(source, settings, conn, **kwargs)


def reset_shared_state() -> None:
    """Forget process-wide robots rules, host slots and the global semaphore (tests)."""
    ROBOTS.reset()
    ratelimit.reset()
