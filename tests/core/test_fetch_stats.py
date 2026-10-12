"""FetchContext.stats: the counts behind a source's list progress line (bug 85e102e)."""

from __future__ import annotations

import logging

import httpx
import pytest
from test_fetch import (  # noqa: F401
    HOST,
    _fresh_shared_state,
    clock,
    conn,
    make_ctx,
    server,
    settings,
)

from jobhunter.core.fetch import RobotsDisallowed


def test_stats_count_pages_cache_hits_and_robots_skips(make_ctx, server):  # noqa: F811
    server.route(
        f"{HOST}/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    server.route(f"{HOST}/a", httpx.Response(200, text="a"))
    server.route(f"{HOST}/b", httpx.Response(200, text="b"))
    ctx = make_ctx()
    assert ctx.stats == {"pages": 0, "cached": 0, "robots_skips": 0}
    ctx.get(f"{HOST}/a")
    ctx.get(f"{HOST}/b")
    ctx.get(f"{HOST}/a", ttl=3600)  # answered from the local cache
    with pytest.raises(RobotsDisallowed):
        ctx.get(f"{HOST}/private/x")
    # robots.txt itself is not a page of the source.
    assert ctx.stats == {"pages": 2, "cached": 1, "robots_skips": 1}


def test_retry_line_names_the_host_not_the_url(make_ctx, server, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    server.route(f"{HOST}/jobs?q=SECRETQUERY&title=TITLEINURL", httpx.Response(503, text="down"))
    server.route(f"{HOST}/robots.txt", httpx.Response(404))
    with pytest.raises(Exception):  # noqa: B017 - TransientFetchError after the retries
        make_ctx().get(f"{HOST}/jobs?q=SECRETQUERY&title=TITLEINURL")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("retrying")]
    assert lines
    assert all("SECRETQUERY" not in m and "TITLEINURL" not in m and "/jobs" not in m for m in lines)


def test_spacing_reports_rate_limit_or_crawl_delay(make_ctx, server):  # noqa: F811
    server.route(f"{HOST}/robots.txt", httpx.Response(200, text="User-agent: *\nCrawl-delay: 5\n"))
    server.route(f"{HOST}/a", httpx.Response(200, text="a"))
    ctx = make_ctx()
    assert ctx.spacing(f"{HOST}/a") == (1.0, "rate limit")  # robots not cached yet
    ctx.get(f"{HOST}/a")
    assert ctx.spacing(f"{HOST}/a") == (5.0, "robots Crawl-delay")


def test_limiter_logs_only_long_waits(caplog):
    import random

    from jobhunter.core.fetch.ratelimit import HostRateLimiter

    caplog.set_level(logging.INFO)
    lim = HostRateLimiter()
    now = [0.0]
    kw = {"clock": lambda: now[0], "sleep": lambda s: None, "rng": random.Random(1)}
    lim.acquire("h.example", 3.0, **kw)
    lim.acquire("h.example", 3.0, **kw)  # waits 3 s: quiet
    assert not caplog.records
    lim.acquire("h.example", 30.0, **kw)  # waits 3 s more, reserves 30 s
    lim.acquire("h.example", 30.0, **kw)  # waits 30 s: logged
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["waiting 36s for h.example (per-host request spacing)"]
