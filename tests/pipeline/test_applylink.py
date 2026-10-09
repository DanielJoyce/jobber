"""Apply-link resolver (specs/015). httpx.MockTransport through FetchContext: no network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state
from jobhunter.core.models import ApplyStatus
from jobhunter.pipeline.applylink import (
    get_apply_link,
    js_redirect_target,
    meta_refresh_target,
    normalize_url,
    resolve_group,
    resolve_pending,
    resolve_url,
    reverify,
    unknown_hosts,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
BOARD = "https://board.example.org"
TRACK = "https://track.example.net"
WD = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Albany-NY/Analyst_R-1234"
GH = "https://boards.greenhouse.io/acme/jobs/567"
USAS = "https://apply.usastaffing.gov/Application/Apply?AnnouncementNumber=X-1&JobId=1"
OPEN_PAGE = "<html><body><h1>Analyst</h1><p>Apply today.</p></body></html>"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Server:
    """Route table over MockTransport. Unrouted robots.txt is 404; anything else unrouted is 500."""

    def __init__(self) -> None:
        self.routes: dict[str, Handler | httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    def route(self, url: str, handler: Handler | httpx.Response) -> None:
        self.routes[url] = handler

    def redirect(self, src: str, dst: str, status: int = 302) -> None:
        self.route(src, httpx.Response(status, headers={"Location": dst}))

    def page(self, url: str, html: str = OPEN_PAGE, status: int = 200) -> None:
        self.route(url, httpx.Response(status, html=html))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        target = self.routes.get(url)
        if target is None:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(500, text=f"no route for {url}")
        return target(request) if callable(target) else target

    def hosts(self, *, robots: bool = False) -> list[str]:
        return [
            r.url.host for r in self.requests if robots or r.url.path != "/robots.txt"
        ]  # fmt: skip

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache")}, "fetch": {"max_retries": 0}}
    )


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def factory(settings, conn, server):
    clock = FakeClock()

    def make(url: str):
        return fetch_context_for_url(
            settings, conn, url, transport=server.transport, clock=clock, sleep=clock.sleep
        )

    return make


@pytest.fixture
def resolve(factory):
    def run(url: str, **kw):
        ctx = factory(url)
        try:
            return resolve_url(url, ctx, **kw)
        finally:
            ctx.close()

    return run


def add_group(conn, *jobs: tuple[str, str | None]) -> int:
    """jobs: (url, apply_url); the first is the canonical member."""
    gid = conn.execute(
        "INSERT INTO job_group (member_count, method, created_at) VALUES (?, 'manual', ?)",
        (len(jobs), NOW.isoformat()),
    ).lastrowid
    for i, (url, apply_url) in enumerate(jobs):
        jid = conn.execute(
            "INSERT INTO job (source_key, external_id, job_group_id, url, title, apply_url, "
            "first_seen_at, last_seen_at) VALUES ('wa', ?, ?, ?, 'Analyst', ?, ?, ?)",
            (f"{gid}-{i}", gid, url, apply_url, NOW.isoformat(), NOW.isoformat()),
        ).lastrowid
        if i == 0:
            conn.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (jid, gid))
    return gid


# ─── unwrap and hops ───────────────────────────────────────────────────────


def test_appcast_link_unwrapped_locally(resolve, server):
    start = str(httpx.URL("https://click.appcast.io/track/abc", params={"cs": "1", "url": WD}))
    server.page(WD)
    res = resolve(start)
    assert "click.appcast.io" not in server.hosts(robots=True)
    assert [(h.method, h.url, h.status) for h in res.chain] == [("unwrap", WD, 200)]
    assert res.status == ApplyStatus.live
    assert (res.ats, res.final_url, res.employer_host) == (
        "workday",
        WD + "/apply",
        "acme.wd5.myworkdayjobs.com",
    )


def test_3xx_chain_to_workday_canonicalized(resolve, server):
    server.redirect(f"{BOARD}/jobs/1/apply", f"{TRACK}/t/9")
    server.redirect(f"{TRACK}/t/9", WD, 301)
    server.page(WD)
    res = resolve(f"{BOARD}/jobs/1/apply")
    assert [(h.method, h.host, h.status) for h in res.chain] == [
        ("3xx", "track.example.net", 301),
        ("3xx", "acme.wd5.myworkdayjobs.com", 200),
    ]
    assert res.status == ApplyStatus.live and res.final_url == WD + "/apply"


def test_already_apply_url_not_doubled(resolve, server):
    server.page(WD + "/apply")
    res = resolve(WD + "/apply")
    assert res.chain == [] and res.final_url == WD + "/apply"


def test_greenhouse_gets_app_fragment(resolve, server):
    server.redirect(f"{BOARD}/jobs/2", GH)
    server.page(GH)
    res = resolve(f"{BOARD}/jobs/2")
    assert (res.status, res.ats, res.final_url) == (ApplyStatus.live, "greenhouse", GH + "#app")


def test_greenhouse_redirect_to_board_home_is_expired(resolve, server):
    server.redirect(GH, "https://boards.greenhouse.io/acme?error=true")
    res = resolve(GH)
    assert (res.status, res.ats) == (ApplyStatus.expired, "greenhouse")


def test_meta_refresh_hop(resolve, server):
    server.page(
        f"{BOARD}/jobs/3",
        '<html><head><meta http-equiv="Refresh" content="0; URL=\'/out?id=3\'"></head></html>',
    )
    server.redirect(f"{BOARD}/out", GH)
    server.page(GH)
    res = resolve(f"{BOARD}/jobs/3")
    assert [h.method for h in res.chain] == ["meta", "3xx"]
    assert res.chain[0].url == f"{BOARD}/out?id=3"
    assert res.final_url == GH + "#app"


def test_js_location_hop(resolve, server):
    server.page(
        f"{BOARD}/jobs/4",
        '<html><script>window.location.href = "https://careers.acme.example/j/4";</script></html>',
    )
    server.page("https://careers.acme.example/j/4")
    res = resolve(f"{BOARD}/jobs/4")
    assert [(h.method, h.url) for h in res.chain] == [("js", "https://careers.acme.example/j/4")]
    assert (res.status, res.ats, res.employer_host) == (
        ApplyStatus.live,
        "unknown",
        "careers.acme.example",
    )


def test_js_and_meta_parsers():
    base = f"{BOARD}/x"
    assert js_redirect_target('<script>location.replace("/y")</script>', base) == f"{BOARD}/y"
    assert js_redirect_target("<script>location = '/z';</script>", base) == f"{BOARD}/z"
    # not a literal: concatenation and variables are ignored
    assert js_redirect_target('<script>location.href = "/a" + id;</script>', base) is None
    assert js_redirect_target("<script>window.location = dest;</script>", base) is None
    assert js_redirect_target('<script src="x.js">location="/q"</script>', base) is None
    # a long-delay refresh is a page reload, not a redirect
    assert meta_refresh_target('<meta http-equiv="refresh" content="300;url=/k">', base) is None
    assert meta_refresh_target('<meta http-equiv="refresh" content="5">', base) is None


def test_browser_fallback_hook(resolve, server):
    server.page(f"{BOARD}/jobs/5", "<html><script>go()</script></html>")
    server.page("https://careers.acme.example/j/5")
    seen: list[str] = []

    def fallback(url: str) -> str | None:
        seen.append(url)
        return "https://careers.acme.example/j/5" if url.endswith("/jobs/5") else None

    res = resolve(f"{BOARD}/jobs/5", browser_fallback=fallback)
    assert [h.method for h in res.chain] == ["browser"]
    assert res.status == ApplyStatus.live


def test_loop_detected(resolve, server):
    server.redirect(f"{BOARD}/a", f"{BOARD}/b")
    server.redirect(f"{BOARD}/b", f"{BOARD}/a/")
    res = resolve(f"{BOARD}/a")
    assert res.status == ApplyStatus.unresolved
    assert "loop" in (res.error or "")
    assert len(res.chain) == 1


def test_nine_hops_stops_at_eight(resolve, server):
    for i in range(9):
        server.redirect(f"{BOARD}/h{i}", f"{BOARD}/h{i + 1}")
    server.page(f"{BOARD}/h9")
    res = resolve(f"{BOARD}/h0")
    assert res.status == ApplyStatus.unresolved
    assert len(res.chain) == 8 and "more than 8 hops" in (res.error or "")
    assert f"{BOARD}/h9" not in [str(r.url) for r in server.requests]


# ─── expiry ────────────────────────────────────────────────────────────────


def test_404_final_is_expired(resolve, server):
    server.redirect(f"{BOARD}/jobs/6", "https://careers.acme.example/j/6")
    server.page("https://careers.acme.example/j/6", "<p>Not found</p>", status=404)
    res = resolve(f"{BOARD}/jobs/6")
    assert res.status == ApplyStatus.expired
    assert res.final_url == "https://careers.acme.example/j/6"


def test_closed_phrase_is_expired(resolve, server):
    server.page(
        "https://careers.acme.example/j/7",
        "<html><body><div>Sorry, this employer is\n No Longer  Accepting Applications.</div>"
        "</body></html>",
    )
    res = resolve("https://careers.acme.example/j/7")
    assert res.status == ApplyStatus.expired


def test_closed_phrase_in_script_ignored(resolve, server):
    server.page(
        "https://careers.acme.example/j/8",
        "<html><script>var msg = 'this job has expired';</script><p>Open role</p></html>",
    )
    assert resolve("https://careers.acme.example/j/8").status == ApplyStatus.live


# ─── robots, denials, failures ─────────────────────────────────────────────


def test_robots_disallow_mid_chain_is_blocked(resolve, server):
    server.redirect(f"{BOARD}/jobs/9", f"{TRACK}/t/9")
    server.redirect(f"{TRACK}/t/9", "https://www.governmentjobs.com/careers/acme/jobs/99/x")
    server.route(
        "https://www.governmentjobs.com/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
    )
    res = resolve(f"{BOARD}/jobs/9")
    assert res.status == ApplyStatus.blocked
    assert res.final_url == f"{TRACK}/t/9"  # last allowed URL
    assert res.chain[-1].host == "www.governmentjobs.com" and res.chain[-1].status is None
    assert server.hosts().count("www.governmentjobs.com") == 0


def test_robots_disallow_at_start(resolve, server):
    server.route(f"{BOARD}/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    res = resolve(f"{BOARD}/jobs/10")
    assert (res.status, res.final_url, res.chain) == (ApplyStatus.blocked, f"{BOARD}/jobs/10", [])


def test_access_denied_is_unresolved(resolve, server):
    server.redirect(f"{BOARD}/jobs/11", "https://careers.acme.example/j/11")
    server.route("https://careers.acme.example/j/11", httpx.Response(403))
    res = resolve(f"{BOARD}/jobs/11")
    assert res.status == ApplyStatus.unresolved
    assert "403" in (res.error or "") and res.chain[-1].status == 403


def test_transient_failure_is_unresolved(resolve, server):
    server.route(f"{BOARD}/jobs/12", httpx.Response(503))
    res = resolve(f"{BOARD}/jobs/12")
    assert res.status == ApplyStatus.unresolved and "giving up" in (res.error or "")


def test_non_http_destination_is_unresolved(resolve, server):
    server.redirect(f"{BOARD}/jobs/13", "mailto:jobs@example.com")
    res = resolve(f"{BOARD}/jobs/13")
    assert res.status == ApplyStatus.unresolved and res.final_url == f"{BOARD}/jobs/13"


def test_normalize_url():
    assert normalize_url("HTTPS://Board.Example.org:443/a/?b=2&a=1#x") == normalize_url(
        "https://board.example.org/a?a=1&b=2"
    )


# ─── groups and apply_link rows ────────────────────────────────────────────


def test_usajobs_apply_uri_needs_no_requests(conn, factory, server):
    gid = add_group(conn, ("https://www.usajobs.gov/job/123456", USAS))
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert server.requests == []
    assert (link.status, link.ats, link.final_url, link.chain) == (
        ApplyStatus.live,
        "usastaffing",
        USAS,
        [],
    )
    row = conn.execute("SELECT * FROM apply_link WHERE job_group_id = ?", (gid,)).fetchone()
    assert (row["status"], row["start_url"], json.loads(row["chain"])) == ("live", USAS, [])
    assert row["employer_host"] == "apply.usastaffing.gov" and row["verified_at"] is None


def test_first_member_to_reach_live_wins(conn, factory, server):
    server.page(f"{BOARD}/gone", "<p>gone</p>", status=404)
    server.redirect(f"{BOARD}/jobs/20", GH)
    server.page(GH)
    gid = add_group(
        conn, (f"{BOARD}/jobs/20", None), ("https://other.example/j/20", f"{BOARD}/gone")
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.start_url == f"{BOARD}/jobs/20"  # apply_url tried first, but it was expired
    assert (link.status, link.ats, link.final_url) == (ApplyStatus.live, "greenhouse", GH + "#app")
    stored = get_apply_link(conn, gid)
    assert stored is not None and stored.chain[0].method == "3xx"
    assert stored.verified_at == NOW


def test_resolve_pending_skips_existing_rows(conn, factory, server):
    server.page("https://careers.acme.example/j/30")
    a = add_group(conn, ("https://careers.acme.example/j/30", None))
    b = add_group(conn, ("https://www.usajobs.gov/job/1", USAS))
    done = resolve_pending(conn, now=NOW, ctx_factory=factory)
    assert sorted(link.job_group_id for link in done) == [a, b]
    assert resolve_pending(conn, now=NOW, ctx_factory=factory) == []
    c = add_group(conn, ("https://www.usajobs.gov/job/2", USAS))
    picked = resolve_pending(conn, now=NOW, ctx_factory=factory, groups=lambda _c: [a, c])
    assert [link.job_group_id for link in picked] == [c]


def test_reverify_flips_live_to_expired(conn, factory, server):
    server.page(GH)
    gid = add_group(conn, (GH, None))
    assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).status == ApplyStatus.live
    server.page(GH, "<p>Not found</p>", status=404)
    n = len(server.requests)

    fresh = reverify(conn, gid, now=NOW + timedelta(hours=1), ctx_factory=factory)
    assert fresh is not None and fresh.status == ApplyStatus.live
    assert len(server.requests) == n  # verified within 24h: no request

    later = NOW + timedelta(hours=25)
    link = reverify(conn, gid, now=later, ctx_factory=factory)
    assert link is not None and link.status == ApplyStatus.expired and link.verified_at == later
    assert [str(r.url) for r in server.requests[n:]] == [GH]  # one GET
    assert get_apply_link(conn, gid).status == ApplyStatus.expired


def test_reverify_phrase_after_one_redirect(conn, factory, server):
    server.page("https://careers.acme.example/j/40")
    gid = add_group(conn, ("https://careers.acme.example/j/40", None))
    resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    server.redirect("https://careers.acme.example/j/40", "https://careers.acme.example/closed/40")
    server.page("https://careers.acme.example/closed/40", "<p>This posting has closed.</p>")
    link = reverify(conn, gid, now=NOW + timedelta(days=2), ctx_factory=factory)
    assert link is not None and link.status == ApplyStatus.expired


def test_reverify_non_http_location_is_an_error(conn, factory, server):
    server.page(GH)
    gid = add_group(conn, (GH, None))
    assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).status == ApplyStatus.live
    server.redirect(GH, "mailto:jobs@example.com")
    link = reverify(conn, gid, now=NOW + timedelta(days=2), ctx_factory=factory)
    assert link is not None and link.status == ApplyStatus.live
    assert "non-http destination" in (link.error or "")


def test_reverify_leaves_blocked_alone(conn, factory, server):
    server.route(f"{BOARD}/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    gid = add_group(conn, (f"{BOARD}/jobs/50", None))
    assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).status == ApplyStatus.blocked
    n = len(server.requests)
    link = reverify(conn, gid, now=NOW + timedelta(days=3), ctx_factory=factory)
    assert link is not None and link.status == ApplyStatus.blocked
    assert len(server.requests) == n


def test_unknown_hosts_counts(conn):
    rows = [
        ("a.example", "unknown"),
        ("a.example", "unknown"),
        ("b.example", "unknown"),
        ("c.example", "workday"),
        ("a.example", "unknown"),
    ]
    for host, ats in rows:
        gid = add_group(conn, (f"https://{host}/j", None))
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, chain, ats, employer_host, status, "
            "resolved_at) VALUES (?, ?, '[]', ?, ?, 'live', ?)",
            (gid, f"https://{host}/j", ats, host, NOW.isoformat()),
        )
    assert unknown_hosts(conn) == [("a.example", 3), ("b.example", 1)]
    assert unknown_hosts(conn, top=1) == [("a.example", 3)]
