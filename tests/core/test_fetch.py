"""FetchContext tests. Everything runs on httpx.MockTransport and a fake clock: no network."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import (
    AccessDenied,
    CachedResponse,
    ContentCache,
    FetchContext,
    HostRateLimiter,
    RobotsDisallowed,
    SourceBlocked,
    TierError,
    TransientFetchError,
    fetch_context_for_url,
    request_hash,
    reset_shared_state,
)
from jobhunter.core.models import Policy, SourceRow, Tier

HOST = "https://jobs.example.gov"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Server:
    """Route table over MockTransport. Unrouted robots.txt is 404; anything else unrouted is 500."""

    def __init__(self, clock: FakeClock) -> None:
        self.routes: dict[str, Handler | httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []
        self.clock = clock

    def route(self, url: str, handler: Handler | httpx.Response) -> None:
        self.routes[url] = handler

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock())
        url = str(request.url.copy_with(query=None))
        target = self.routes.get(url)
        if target is None:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(500, text=f"no route for {url}")
        return target(request) if callable(target) else target

    def hits(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source(key, class, name, family, tier, entry, policy) "
        "VALUES ('s1', 'A', 'Example', 'vos', 'http', ?, 'enabled')",
        (HOST + "/",),
    )
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.model_validate({"paths": {"cache_dir": str(tmp_path / "cache")}})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def server(clock) -> Server:
    return Server(clock)


def make_source(**overrides) -> SourceRow:
    data = {
        "key": "s1",
        "class": "A",
        "name": "Example",
        "family": "vos",
        "tier": "http",
        "entry": HOST + "/",
        "rate_limit": {"rps": 1.0},
    }
    data.update(overrides)
    return SourceRow.model_validate(data)


@pytest.fixture
def make_ctx(settings, conn, server, clock):
    made: list[FetchContext] = []

    def _make(source: SourceRow | None = None, settings_: Settings | None = None) -> FetchContext:
        ctx = FetchContext(
            source or make_source(),
            settings_ or settings,
            conn,
            transport=server.transport,
            clock=clock,
            sleep=clock.sleep,
        )
        made.append(ctx)
        return ctx

    yield _make
    for ctx in made:
        ctx.close()


def log_rows(conn, url: str | None = None):
    sql = "SELECT * FROM fetch_log"
    args: tuple = ()
    if url is not None:
        sql += " WHERE url = ?"
        args = (url,)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


# ─── content cache ─────────────────────────────────────────────────────────


def test_cache_dedupes_identical_bodies(tmp_path):
    cache = ContentCache(tmp_path)
    h1 = cache.put(b"<html>same</html>")
    h2 = cache.put(b"<html>same</html>")
    h3 = cache.put(b"<html>different</html>")
    assert h1 == h2 != h3
    assert cache.path_for(h1) == tmp_path / h1[:2] / h1[2:4] / f"{h1}.gz"
    assert len(list(tmp_path.rglob("*.gz"))) == 2
    assert cache.get(h1) == b"<html>same</html>"


def test_identical_responses_share_one_cache_file(make_ctx, server, settings, tmp_path):
    server.route(f"{HOST}/a", httpx.Response(200, text="same body"))
    server.route(f"{HOST}/b", httpx.Response(200, text="same body"))
    ctx = make_ctx()
    a, b = ctx.get(f"{HOST}/a"), ctx.get(f"{HOST}/b")
    assert a.content_hash == b.content_hash
    files = [p for p in (tmp_path / "cache").rglob("*.gz")]
    # robots.txt 404 (empty body) + the shared page body.
    assert len(files) == 2


# ─── fetch_log, conditional requests, ttl ──────────────────────────────────


def test_fetch_log_row_written(make_ctx, server, conn):
    server.route(
        f"{HOST}/jobs",
        httpx.Response(
            200,
            text="hello",
            headers={"ETag": '"v1"', "Last-Modified": "Wed, 01 Oct 2026 00:00:00 GMT"},
        ),
    )
    resp = make_ctx().get(f"{HOST}/jobs", params={"q": "analyst"})
    assert resp.status == 200 and resp.text == "hello" and not resp.from_cache
    (row,) = log_rows(conn, f"{HOST}/jobs?q=analyst")
    assert row["source_key"] == "s1"
    assert row["method"] == "GET"
    assert row["http_status"] == 200
    assert row["etag"] == '"v1"'
    assert row["last_modified"] == "Wed, 01 Oct 2026 00:00:00 GMT"
    assert row["content_hash"] == resp.content_hash
    assert row["bytes"] == 5
    assert row["from_cache"] == 0
    assert row["request_hash"] == request_hash("GET", f"{HOST}/jobs?q=analyst")
    # robots.txt went through the same log.
    assert len(log_rows(conn, f"{HOST}/robots.txt")) == 1


def test_request_hash_ignores_param_order_but_not_body():
    assert request_hash("GET", "https://h/x?b=2&a=1") == request_hash("GET", "https://h/x?a=1&b=2")
    assert request_hash("POST", "https://h/x", b"a=1") != request_hash("POST", "https://h/x", b"")
    assert request_hash("GET", "https://h/x") != request_hash("POST", "https://h/x")


def test_conditional_request_304_returns_cached_body(make_ctx, server, conn):
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(
            200,
            text="original body",
            headers={"ETag": '"v1"', "Last-Modified": "Wed, 01 Oct 2026 00:00:00 GMT"},
        )

    server.route(f"{HOST}/list", handler)
    ctx = make_ctx()
    first = ctx.get(f"{HOST}/list")
    second = ctx.get(f"{HOST}/list")
    assert "if-none-match" not in seen[0]
    assert seen[1]["if-none-match"] == '"v1"'
    assert seen[1]["if-modified-since"] == "Wed, 01 Oct 2026 00:00:00 GMT"
    assert second.from_cache and second.status == 200
    assert second.text == "original body"
    assert second.content_hash == first.content_hash
    rows = log_rows(conn, f"{HOST}/list")
    assert [(r["http_status"], r["from_cache"]) for r in rows] == [(200, 0), (304, 1)]
    assert rows[1]["content_hash"] == first.content_hash


def test_post_is_logged_and_never_conditional(make_ctx, server, conn):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="results", headers={"ETag": '"p"'})

    server.route(f"{HOST}/search", handler)
    ctx = make_ctx()
    ctx.post(f"{HOST}/search", data={"q": "x"})
    resp = ctx.post(f"{HOST}/search", data={"q": "x"})
    assert "if-none-match" not in seen[1].headers
    assert not resp.from_cache
    rows = log_rows(conn, f"{HOST}/search")
    assert [r["method"] for r in rows] == ["POST", "POST"]
    assert rows[0]["request_hash"] == request_hash("POST", f"{HOST}/search", b"q=x")


def test_ttl_short_circuits_network(make_ctx, server, conn):
    server.route(f"{HOST}/detail", httpx.Response(200, text="cached"))
    ctx = make_ctx()
    ctx.get(f"{HOST}/detail")
    resp = ctx.get(f"{HOST}/detail", ttl=timedelta(hours=1))
    assert server.hits("/detail") == 1
    assert resp.from_cache and resp.text == "cached"
    # A stale entry goes back to the network.
    conn.execute("UPDATE fetch_log SET fetched_at = '2000-01-01T00:00:00+00:00'")
    ctx.get(f"{HOST}/detail", ttl=3600)
    assert server.hits("/detail") == 2


# ─── robots ────────────────────────────────────────────────────────────────


def test_robots_allow_and_deny(make_ctx, server):
    server.route(
        f"{HOST}/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /private\n# Disallow: /\n"),
    )
    server.route(f"{HOST}/public", httpx.Response(200, text="ok"))
    ctx = make_ctx()
    assert ctx.get(f"{HOST}/public").text == "ok"
    with pytest.raises(RobotsDisallowed):
        ctx.get(f"{HOST}/private/page")
    assert server.hits("/private/page") == 0
    # Fetched once per host per process, even across contexts.
    make_ctx().get(f"{HOST}/public")
    assert server.hits("/robots.txt") == 1


def test_robots_matches_our_product_token(make_ctx, server):
    server.route(
        f"{HOST}/robots.txt",
        httpx.Response(200, text="User-agent: Googlebot\nAllow: /\n\nUser-agent: *\nDisallow: /\n"),
    )
    with pytest.raises(RobotsDisallowed):
        make_ctx().get(f"{HOST}/jobs")


def test_robots_commented_out_directives_allow(make_ctx, server):
    server.route(f"{HOST}/robots.txt", httpx.Response(200, text="#User-agent: *\n#Disallow: /\n"))
    server.route(f"{HOST}/jobs", httpx.Response(200, text="ok"))
    assert make_ctx().get(f"{HOST}/jobs").status == 200


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_allows(make_ctx, server, status):
    server.route(f"{HOST}/robots.txt", httpx.Response(status))
    server.route(f"{HOST}/jobs", httpx.Response(200, text="ok"))
    assert make_ctx().get(f"{HOST}/jobs").status == 200


@pytest.mark.parametrize("status", [401, 403])
def test_forbidden_robots_denies(make_ctx, server, status):
    server.route(f"{HOST}/robots.txt", httpx.Response(status))
    with pytest.raises(RobotsDisallowed):
        make_ctx().get(f"{HOST}/jobs")
    assert server.hits("/jobs") == 0


def test_robots_network_error_denies(make_ctx, server, caplog):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server.route(f"{HOST}/robots.txt", boom)
    with caplog.at_level(logging.WARNING), pytest.raises(RobotsDisallowed):
        make_ctx().get(f"{HOST}/jobs")
    assert server.hits("/jobs") == 0
    assert "robots.txt fetch failed" in caplog.text


def test_respect_robots_false_computes_but_does_not_enforce(make_ctx, server, settings, caplog):
    lax = settings.model_copy(
        update={"fetch": settings.fetch.model_copy(update={"respect_robots": False})}
    )
    server.route(f"{HOST}/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    server.route(f"{HOST}/jobs", httpx.Response(200, text="ok"))
    ctx = make_ctx(settings_=lax)
    with caplog.at_level(logging.WARNING):
        assert ctx.get(f"{HOST}/jobs").status == 200
        ctx.get(f"{HOST}/jobs")
    assert caplog.text.count("disallows jobhunter") == 1


def test_sanctioned_api_skips_robots_enforcement_on_its_own_host(make_ctx, server):
    api = "https://data.example.gov"
    server.route(f"{api}/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    server.route(f"{api}/api/search", httpx.Response(200, json={"items": []}))
    src = make_source(tier="api", entry=f"{api}/api/search", config={"sanctioned_api": True})
    assert make_ctx(src).get(f"{api}/api/search").json() == {"items": []}
    # Any other host is still robots-enforced.
    server.route(f"{HOST}/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    with pytest.raises(RobotsDisallowed):
        make_ctx(src).get(f"{HOST}/x")


# ─── source policy ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("policy", [Policy.blocked, Policy.disabled])
def test_blocked_source_raises_before_network(make_ctx, server, policy):
    ctx = make_ctx(make_source(policy=policy))
    with pytest.raises(SourceBlocked):
        ctx.get(f"{HOST}/jobs")
    with pytest.raises(SourceBlocked):
        ctx.post(f"{HOST}/jobs", data={"a": "1"})
    assert server.requests == []


# ─── retry ladder ──────────────────────────────────────────────────────────


def test_429_with_retry_after_then_success(make_ctx, server, clock):
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")]
    )
    server.route(f"{HOST}/jobs", lambda _req: next(responses))
    resp = make_ctx().get(f"{HOST}/jobs")
    assert resp.status == 200
    assert server.hits("/jobs") == 2
    assert 7 in clock.sleeps


def test_5xx_exhausts_retries(make_ctx, server, settings, clock, conn):
    server.route(f"{HOST}/jobs", httpx.Response(503, text="down"))
    with pytest.raises(TransientFetchError) as info:
        make_ctx().get(f"{HOST}/jobs")
    attempts = settings.fetch.max_retries + 1
    assert server.hits("/jobs") == attempts
    assert info.value.status == 503
    assert [r["http_status"] for r in log_rows(conn, f"{HOST}/jobs")] == [503] * attempts


def test_connection_errors_retry_then_give_up(make_ctx, server):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    server.route(f"{HOST}/jobs", boom)
    with pytest.raises(TransientFetchError):
        make_ctx().get(f"{HOST}/jobs")


def test_huge_retry_after_gives_up_immediately(make_ctx, server):
    server.route(f"{HOST}/jobs", httpx.Response(429, headers={"Retry-After": "86400"}))
    with pytest.raises(TransientFetchError):
        make_ctx().get(f"{HOST}/jobs")
    assert server.hits("/jobs") == 1


@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_without_retry(make_ctx, server, status):
    server.route(f"{HOST}/jobs", httpx.Response(status))
    with pytest.raises(AccessDenied) as info:
        make_ctx().get(f"{HOST}/jobs")
    assert info.value.status == status
    assert server.hits("/jobs") == 1


def test_other_4xx_is_returned(make_ctx, server):
    server.route(f"{HOST}/gone", httpx.Response(404, text="nope"))
    resp = make_ctx().get(f"{HOST}/gone")
    assert resp.status == 404 and resp.text == "nope"
    assert server.hits("/gone") == 1


# ─── rate limiting ─────────────────────────────────────────────────────────


def test_rate_limiter_spaces_requests_per_host(make_ctx, server):
    for p in ("a", "b", "c"):
        server.route(f"{HOST}/{p}", httpx.Response(200, text=p))
    server.route("https://other.example.org/x", httpx.Response(200, text="x"))
    ctx = make_ctx(make_source(rate_limit={"rps": 0.5}))
    for p in ("a", "b", "c"):
        ctx.get(f"{HOST}/{p}")
    # robots at t=0, then one request every 2s.
    assert server.times == [0.0, 2.0, 4.0, 6.0]
    # A different host is not held back by the first host's schedule.
    ctx.get("https://other.example.org/x")
    assert server.times[-2:] == [6.0, 8.0]  # its robots.txt immediately, page after interval


def test_rate_limiter_jitter_bounds():
    limiter = HostRateLimiter()
    t = [0.0]
    waits: list[float] = []

    def sleep(s: float) -> None:
        waits.append(s)
        t[0] += s

    for _ in range(50):
        limiter.acquire("h", 4.0, clock=lambda: t[0], sleep=sleep, jitter=True)
    assert all(3.0 <= w <= 5.0 for w in waits)
    assert len(set(waits)) > 1


def page_times(server, path: str) -> list[float]:
    return [t for r, t in zip(server.requests, server.times, strict=True) if r.url.path == path]


def test_crawl_delay_floors_the_interval(make_ctx, server):
    server.route(f"{HOST}/robots.txt", httpx.Response(200, text="User-agent: *\nCrawl-delay: 10\n"))
    server.route(f"{HOST}/a", httpx.Response(200, text="a"))
    server.route(f"{HOST}/b", httpx.Response(200, text="b"))
    # rps 1.0 asks for 1s; jitter would scatter that to 0.75-1.25s, but the floor holds at 10s.
    ctx = make_ctx(make_source(rate_limit={"rps": 1.0, "jitter": True}))
    ctx.get(f"{HOST}/a")
    ctx.get(f"{HOST}/b")
    a, b = page_times(server, "/a")[0], page_times(server, "/b")[0]
    assert b - a >= 10


def test_crawl_delay_prefers_our_product_token(make_ctx, server):
    robots = "User-agent: *\nCrawl-delay: 2\n\nUser-agent: jobhunter\nCrawl-delay: 30\n"
    server.route(f"{HOST}/robots.txt", httpx.Response(200, text=robots))
    server.route(f"{HOST}/a", httpx.Response(200, text="a"))
    server.route(f"{HOST}/b", httpx.Response(200, text="b"))
    ctx = make_ctx(make_source(rate_limit={"rps": 1.0}))
    ctx.get(f"{HOST}/a")
    ctx.get(f"{HOST}/b")
    a, b = page_times(server, "/a")[0], page_times(server, "/b")[0]
    assert b - a >= 30


def test_default_rate_limit_from_settings(make_ctx, server, settings):
    server.route(f"{HOST}/a", httpx.Response(200))
    ctx = make_ctx(make_source(rate_limit=None))
    assert ctx.rate_limit.rps == settings.fetch.default_rps
    assert ctx.rate_limit.jitter is True
    ctx.get(f"{HOST}/a")
    gap = server.times[1] - server.times[0]
    assert 0.75 * 5 <= gap <= 1.25 * 5


# ─── redirects and sessions ────────────────────────────────────────────────


def test_follow_redirects_false_exposes_location(make_ctx, server):
    server.route(f"{HOST}/go", httpx.Response(302, headers={"Location": "/dest?id=1"}))
    resp = make_ctx().get(f"{HOST}/go", follow_redirects=False)
    assert resp.status == 302 and resp.is_redirect
    assert resp.location == f"{HOST}/dest?id=1"
    assert server.hits("/dest") == 0


def test_redirects_followed_hop_by_hop_with_robots(make_ctx, server, conn):
    server.route(
        f"{HOST}/go", httpx.Response(302, headers={"Location": "https://ats.example.com/j"})
    )
    server.route("https://ats.example.com/j", httpx.Response(200, text="apply here"))
    resp = make_ctx().get(f"{HOST}/go")
    assert resp.url == f"{HOST}/go"
    assert resp.final_url == "https://ats.example.com/j"
    assert resp.text == "apply here"
    assert server.hits("/robots.txt") == 2  # one per host
    assert [r["http_status"] for r in log_rows(conn, f"{HOST}/go")] == [302]


def test_redirect_into_disallowed_host_stops(make_ctx, server):
    server.route(
        f"{HOST}/go", httpx.Response(302, headers={"Location": "https://ats.example.com/j"})
    )
    server.route(
        "https://ats.example.com/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
    )
    with pytest.raises(RobotsDisallowed) as info:
        make_ctx().get(f"{HOST}/go")
    assert info.value.url == "https://ats.example.com/j"
    assert server.hits("/j") == 0


def test_cookies_persist_in_session(make_ctx, server):
    server.route(
        f"{HOST}/start", httpx.Response(200, text="hi", headers={"Set-Cookie": "sid=abc; Path=/"})
    )
    server.route(
        f"{HOST}/echo", lambda req: httpx.Response(200, text=req.headers.get("cookie", ""))
    )
    ctx = make_ctx()
    ctx.get(f"{HOST}/start")
    assert ctx.get(f"{HOST}/echo").text == "sid=abc"
    assert ctx.session() is ctx.session()


def test_user_agent_and_tokens(make_ctx, server, settings):
    server.route(
        f"{HOST}/ua", lambda req: httpx.Response(200, text=req.headers.get("user-agent", ""))
    )
    ctx = make_ctx()
    assert ctx.get(f"{HOST}/ua").text == settings.user_agent
    ctx.tokens["__VIEWSTATE"] = "abc"
    assert ctx.tokens == {"__VIEWSTATE": "abc"}


def test_page_requires_browser_tier(make_ctx):
    with pytest.raises(TierError):
        make_ctx().page()


def test_adhoc_context_logs_null_source_and_checks_robots(settings, conn, server, clock):
    server.route(
        "https://ats.example.com/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
    )
    server.route("https://ats.example.com/j", httpx.Response(200, text="ok"))
    ctx = fetch_context_for_url(
        settings, conn, transport=server.transport, clock=clock, sleep=clock.sleep
    )
    try:
        assert ctx.source.tier == Tier.http
        assert ctx.get("https://ats.example.com/j").status == 200
        with pytest.raises(RobotsDisallowed):
            ctx.get("https://ats.example.com/private")
    finally:
        ctx.close()
    (row,) = log_rows(conn, "https://ats.example.com/j")
    assert row["source_key"] is None


# ─── CachedResponse ────────────────────────────────────────────────────────


def test_cached_response_decoding(tmp_path):
    cache = ContentCache(tmp_path)
    body = "café".encode("latin-1")
    h = cache.put(body)

    def mk(headers: dict[str, str]) -> CachedResponse:
        from datetime import UTC, datetime

        return CachedResponse(
            url="u",
            final_url="u",
            status=200,
            headers=headers,
            content_hash=h,
            from_cache=False,
            fetched_at=datetime.now(UTC),
            _cache=cache,
        )

    assert mk({"content-type": "text/html; charset=ISO-8859-1"}).text == "café"
    assert mk({}).text == "caf�"  # utf-8 with replacement
    assert mk({"content-type": "text/html; charset=bogus"}).encoding == "utf-8"
    jh = cache.put(b'{"a": 1}')
    resp = mk({})
    resp.content_hash = jh
    assert resp.json() == {"a": 1}


CP1252_PAGE = "<html><body>café</body></html>".encode("cp1252")


def test_windows_1252_charset_survives_ttl_and_304(make_ctx, server, conn):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"p1"':
            return httpx.Response(304, headers={"ETag": '"p1"'})
        return httpx.Response(
            200,
            content=CP1252_PAGE,
            headers={"Content-Type": "text/html; charset=windows-1252", "ETag": '"p1"'},
        )

    server.route(f"{HOST}/old", handler)
    ctx = make_ctx()
    first = ctx.get(f"{HOST}/old")
    assert first.text == "<html><body>café</body></html>"
    from_ttl = ctx.get(f"{HOST}/old", ttl=timedelta(hours=1))
    assert from_ttl.from_cache and from_ttl.text == first.text
    from_304 = ctx.get(f"{HOST}/old")
    assert from_304.from_cache and from_304.text == first.text
    assert server.hits("/old") == 2  # the ttl hit made no request
    # One row for the 200 and one for the 304; ttl hits write no row.
    rows = log_rows(conn, f"{HOST}/old")
    assert [r["http_status"] for r in rows] == [200, 304]
    assert [r["content_type"] for r in rows] == ["text/html; charset=windows-1252"] * 2


@pytest.mark.parametrize(
    "meta",
    [
        '<meta charset="windows-1252">',
        '<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">',
    ],
)
def test_meta_charset_sniffed_when_no_content_type_charset(make_ctx, server, meta):
    body = f"<html><head>{meta}</head><body>café</body></html>".encode("cp1252")
    server.route(
        f"{HOST}/sniff", httpx.Response(200, content=body, headers={"Content-Type": "text/html"})
    )
    resp = make_ctx().get(f"{HOST}/sniff")
    assert resp.encoding == "cp1252"
    assert "café" in resp.text


def test_no_charset_anywhere_falls_back_to_utf8_replacement(make_ctx, server):
    server.route(f"{HOST}/plain", httpx.Response(200, content=CP1252_PAGE))
    assert "caf�" in make_ctx().get(f"{HOST}/plain").text


@pytest.mark.parametrize("location", ["mailto:jobs@example.com", "javascript:void(0)", "http://["])
@pytest.mark.parametrize("follow", [False, True])
def test_non_http_location_is_returned_not_raised(make_ctx, server, location, follow):
    server.route(f"{HOST}/go", httpx.Response(302, headers={"Location": location}))
    resp = make_ctx().get(f"{HOST}/go", follow_redirects=follow)
    assert resp.status == 302
    assert resp.is_redirect
    assert resp.location == location
    assert server.hits("/go") == 1  # never followed
