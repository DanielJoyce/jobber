"""FetchContext.stats: the counts behind a source's list progress line (bug 85e102e)."""

from __future__ import annotations

import httpx
import pytest
from test_fetch import HOST, clock, conn, make_ctx, server, settings  # noqa: F401

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
