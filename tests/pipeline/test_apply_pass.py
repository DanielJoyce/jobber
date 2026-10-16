"""Nightly apply-link pass (bug cf23a1c). httpx.MockTransport through FetchContext: no network."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.console import activity_log
from jobhunter.core import db
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state
from jobhunter.pipeline import apply_attempts, apply_pass, applylink, runner
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
NIGHT2 = NOW + timedelta(days=1)  # the next nightly run
PROFILE = Profile.model_validate({"hard": {"states_allowed": ["CO"]}})
PAGE = "<html><body><h1>Analyst</h1><p>Apply today.</p></body></html>"


def dims(skills: int, direction: str = "match") -> dict:
    return {
        "skills": {"score": skills, "why": "x"},
        "seniority": {"score": skills, "why": "x"},
        "domain": {"score": skills, "why": "x"},
        "raw_skills": skills,
        "recency_weighted_skills": skills,
        "stale_skills": [],
        "seniority_direction": direction,
    }


class Server:
    """Every page is an open posting and every robots.txt a 404, unless routed otherwise."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response] | httpx.Response] = {}
        self.delay = 0.0

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            self.times.append(time.monotonic())
        if self.delay:
            time.sleep(self.delay)
        target = self.routes.get(str(request.url.copy_with(query=None)))
        if target is not None:
            return target(request) if callable(target) else target
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=PAGE)

    _lock = threading.Lock()

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests if r.url.path != "/robots.txt"}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


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


def host(gid: int) -> str:
    return f"careers{gid}.example.org"


def add(
    conn,
    gid: int,
    *,
    skills: int | None = None,
    label: str | None = None,
    app: str | None = None,
    closes: datetime | None = None,
    direction: str = "match",
    url: str | None = None,
) -> int:
    ts = NOW.isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, salary_stated, "
        "location_scope, closes_at, first_seen_at, last_seen_at) "
        "VALUES (?, 'wa', ?, ?, 'Analyst', 0, 'single', ?, ?, ?)",
        (
            gid,
            str(gid),
            url or f"https://{host(gid)}/j/{gid}",
            closes and closes.isoformat(),
            ts,
            ts,
        ),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'manual', ?)",
        (gid, gid, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, gid))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, 'CO', 'Town', 1)",
        (gid,),
    )
    if skills is not None:
        score(conn, gid, skills, direction=direction)
    if label:
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, ?, ?)", (gid, label, ts)
        )
    if app:
        conn.execute(
            "INSERT INTO application (job_group_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (gid, app, ts, ts),
        )
    return gid


def score(conn, gid: int, skills: int, *, tier="screen", direction="match", at=NOW) -> None:
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, created_at) "
        "VALUES (?, ?, 'm', 'p', 's', 'strong', 0, ?, '[]', '[]', ?)",
        (gid, tier, json.dumps(dims(skills, direction)), at.isoformat()),
    )


def member(conn, gid: int, n: int, url: str) -> None:
    """Another posting in group ``gid`` (not canonical)."""
    ts = NOW.isoformat()
    conn.execute(
        "INSERT INTO job (source_key, external_id, job_group_id, url, title, first_seen_at, "
        "last_seen_at) VALUES ('wa', ?, ?, ?, 'Analyst', ?, ?)",
        (f"{gid}-m{n}", gid, url, ts, ts),
    )
    conn.execute("UPDATE job_group SET member_count = member_count + 1 WHERE id = ?", (gid,))


def linked(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT job_group_id FROM apply_link")}


def resolve(conn, settings, factory, *, limit=200, profile=PROFILE, now=NOW):
    return apply_pass.resolve_likely(
        conn, settings, profile, now=now, limit=limit, ctx_factory=factory
    )


def test_only_shortlisted_and_ab_groups_are_resolved(conn, settings, factory, server):
    a = add(conn, 1, skills=90)
    b = add(conn, 2, skills=70)
    low = add(conn, 3, skills=20)
    short = add(conn, 4, label="interesting", app="interested")
    prep = add(conn, 5, app="preparing")
    applied = add(conn, 6, skills=90, label="interesting", app="applied")
    dismissed = add(conn, 7, skills=90, label="not_interesting")
    unscored = add(conn, 8)
    closed = add(conn, 9, skills=90, closes=NOW - timedelta(days=1))

    picked = apply_pass.select_groups(conn, PROFILE, now=NOW)
    assert [(c.group_id, c.why) for c in picked] == [
        (prep, "shortlist"),
        (short, "shortlist"),
        (a, "A"),
        (b, "B"),
    ]
    result = resolve(conn, settings, factory)
    assert linked(conn) == {a, b, short, prep}
    assert not linked(conn) & {low, applied, dismissed, unscored, closed}
    assert server.hosts() == {host(g) for g in (a, b, short, prep)}
    counts = result.counts()
    assert (counts["candidates"], counts["shortlist"], counts["ab"], counts["tried"]) == (
        4,
        2,
        2,
        4,
    )


def test_without_a_profile_only_the_shortlist(conn, settings, factory):
    add(conn, 1, skills=90)
    short = add(conn, 2, label="interesting")
    resolve(conn, settings, factory, profile=None)
    assert linked(conn) == {short}


def test_bound_holds_and_the_rest_waits(conn, settings, factory, server):
    for gid in range(1, 8):
        add(conn, gid, skills=90)
    result = resolve(conn, settings, factory, limit=3)
    assert len(linked(conn)) == 3
    assert len(server.hosts()) == 3
    assert result.counts()["left"] == 4
    resolve(conn, settings, factory, limit=3)
    assert len(linked(conn)) == 6  # next run takes the next three; resolved ones are skipped


def test_user_set_link_untouched(conn, settings, factory, server):
    gid = add(conn, 1, skills=90, label="interesting", app="interested")
    applylink.set_user_link(conn, gid, "https://mine.example.com/apply", NOW - timedelta(days=1))
    other = add(conn, 2, skills=90)
    resolve(conn, settings, factory)
    link = applylink.get_apply_link(conn, gid)
    assert link.chosen_by == "user" and link.final_url == "https://mine.example.com/apply"
    assert host(gid) not in server.hosts() and "mine.example.com" not in server.hosts()
    assert linked(conn) == {gid, other}


def test_existing_rows_including_login_walls_are_not_fetched(conn, settings, factory, server):
    for gid in (add(conn, 1, skills=90), add(conn, 2, label="interesting", app="interested")):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, "
            "resolved_at, error) VALUES (?, ?, ?, '[]', 'blocked', ?, ?)",
            (gid, f"https://{host(gid)}/j/1", f"https://{host(gid)}/j/1", NOW.isoformat(),
             applylink.LOGIN_ERROR),
        )  # fmt: skip
    assert apply_pass.select_groups(conn, PROFILE, now=NOW) == []
    result = resolve(conn, settings, factory)
    assert server.requests == [] and result.tried == 0


def test_one_group_failing_does_not_stop_the_others(conn, settings, factory, monkeypatch):
    add(conn, 1, skills=90)
    add(conn, 2, skills=90)
    real = applylink.resolve_group

    def flaky(c, gid, **kw):
        if gid == 1:
            raise RuntimeError("boom")
        return real(c, gid, **kw)

    monkeypatch.setattr(applylink, "resolve_group", flaky)
    result = resolve(conn, settings, factory)
    assert result.errors == 1 and linked(conn) == {2}


def test_parallel_path_uses_own_connections(tmp_path, settings, server, monkeypatch):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for gid in range(1, 6):
        add(c, gid, skills=90)
    clock = FakeClock()

    def ctx(s, conn, url, **kw):
        assert conn is not c  # each worker thread has its own connection
        kw.update(transport=server.transport, clock=clock, sleep=clock.sleep)
        return fetch_context_for_url(s, conn, url, **kw)

    monkeypatch.setattr(apply_pass, "fetch_context_for_url", ctx)
    result = apply_pass.resolve_likely(c, settings, PROFILE, now=NOW, limit=4, workers=3)
    assert result.tried == 4 and result.errors == 0
    assert len(linked(c)) == 4
    c.close()


# ─── in the run ─────────────────────────────────────────────────────────────


def go(conn, settings, factory, **kw):
    return runner.run_pipeline(
        conn,
        settings,
        [],
        adapters={},
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: PROFILE,
        now=NOW,
        apply_ctx_factory=factory,
        **kw,
    )


def test_run_resolves_after_scoring_and_counts_it(conn, settings, factory):
    gid = add(conn, 1, skills=90)
    report = go(conn, settings, factory)
    assert report.stages[-2:] == ["score", "apply_links"]
    assert linked(conn) == {gid}
    counts = json.loads(conn.execute("SELECT counts FROM run").fetchone()[0])
    assert counts["apply_links"]["tried"] == 1
    # /log shows the stage's counts with the run.
    run = next(e for e in activity_log.events(conn, "run") if e.kind == "run")
    line = next(x for x in run.detail if x.startswith("apply_links:"))
    assert line.startswith("apply_links: tried 1, live ") and "errors 0" in line


def test_stage_flag_skips_the_pass(conn, settings, factory, server):
    add(conn, 1, skills=90)
    report = go(conn, settings, factory, stages=runner.parse_stages(["prefilter,score"]))
    assert "apply_links" not in report.counts and linked(conn) == set()
    assert server.requests == []
    assert runner.parse_stages(["apply_links"]) == ["apply_links"]


def test_per_run_zero_turns_it_off(conn, factory, server, tmp_path):
    add(conn, 1, skills=90)
    off = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "c")}, "apply_links": {"per_run": 0}}
    )
    report = go(conn, off, factory)
    assert report.counts["apply_links"] == {"off": 1} and server.requests == []


def test_pass_failure_never_fails_the_run(conn, settings, factory, monkeypatch):
    add(conn, 1, skills=90)

    def broken(*a, **kw):
        raise sqlite3_error()

    monkeypatch.setattr(apply_pass, "resolve_likely", broken)
    report = go(conn, settings, factory)
    assert report.exit_code == 0
    assert report.counts["apply_links"] == {"failed": 1}
    assert any(m.startswith("apply_links: failed") for m in report.messages)
    row = conn.execute("SELECT exit_status, ended_at FROM run").fetchone()
    assert row["exit_status"] == "ok" and row["ended_at"]


def sqlite3_error() -> Exception:
    import sqlite3

    return sqlite3.OperationalError("database is locked")


def test_backfill_ingest_never_runs_the_pass():
    from jobhunter.pipeline import backfill

    assert "apply_links" not in backfill.INGEST_STAGES


# ─── selection details (review of PR 186) ──────────────────────────────────


def test_closed_shortlisted_posting_is_skipped(conn):
    add(conn, 1, label="interesting", app="interested", closes=NOW - timedelta(hours=1))
    still_open = add(conn, 2, label="interesting", app="interested", closes=NOW + timedelta(days=3))
    assert [c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NOW)] == [still_open]


def test_application_without_label_is_shortlisted(conn):
    gid = add(conn, 1, app="interested")
    picked = apply_pass.select_groups(conn, None, now=NOW)
    assert [(c.group_id, c.why) for c in picked] == [(gid, "shortlist")]


def test_bucket_c_is_not_selected(conn):
    add(conn, 1, skills=90, direction="above")  # stretch up: C
    a = add(conn, 2, skills=90)
    assert [c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NOW)] == [a]


def test_order_shortlist_newest_then_a_then_b_best_first(conn):
    s1 = add(conn, 1, label="interesting")
    s2 = add(conn, 2, app="preparing")
    a99, a92 = add(conn, 10, skills=99), add(conn, 11, skills=92)
    b75, b70 = add(conn, 12, skills=75), add(conn, 13, skills=70)
    picked = apply_pass.select_groups(conn, PROFILE, now=NOW)
    assert [c.group_id for c in picked] == [s2, s1, a99, a92, b75, b70]
    assert [c.why for c in picked] == ["shortlist", "shortlist", "A", "A", "B", "B"]


def test_deep_score_wins_over_a_newer_screen(conn):
    up = add(conn, 1)
    score(conn, up, 90, tier="deep", at=NOW - timedelta(days=2))
    score(conn, up, 20, at=NOW)
    down = add(conn, 2)
    score(conn, down, 20, tier="deep", at=NOW - timedelta(days=2))
    score(conn, down, 90, at=NOW)
    assert [c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NOW)] == [up]


# ─── user links, outages, deadline ─────────────────────────────────────────


def test_link_set_by_hand_during_the_walk_is_kept_and_reported(conn, settings, factory, server):
    gid = add(conn, 1, skills=90)
    page = f"https://{host(gid)}/j/{gid}"

    def user_sets_it(request: httpx.Request) -> httpx.Response:
        applylink.set_user_link(conn, gid, "https://mine.example.com/apply", NOW)
        return httpx.Response(200, html=PAGE)

    server.routes[page] = user_sets_it
    result = resolve(conn, settings, factory)
    link = applylink.get_apply_link(conn, gid)
    assert link.chosen_by == "user" and link.final_url == "https://mine.example.com/apply"
    counts = result.counts()
    assert counts["user_link"] == 1 and counts["live"] == 0


def test_retry_after_outage_defers_instead_of_storing(conn, settings, factory, server):
    gids = [add(conn, gid, skills=90, url=f"https://nlx.example.org/j/{gid}") for gid in (1, 2)]
    held = httpx.Response(429, headers={"Retry-After": "3600"})
    for gid in gids:
        server.routes[f"https://nlx.example.org/j/{gid}"] = held
    result = resolve(conn, settings, factory)
    assert linked(conn) == set()  # nothing frozen
    assert result.counts()["deferred"] == 2 and result.counts()["unresolved"] == 0
    server.routes.clear()
    reset_shared_state()  # the next night: a new process
    resolve(conn, settings, factory, now=NIGHT2)
    assert {applylink.get_apply_link(conn, g).status.value for g in gids} == {"live"}


def test_unreadable_robots_on_the_redirector_defers(conn, settings, factory, server):
    gh = "https://boards.greenhouse.io/acme/jobs/567"
    gid = add(conn, 1, skills=90, url="https://redirect.example.net/r/1")
    member(conn, gid, 1, "https://copies.example.com/jobs/1")  # a plain copy of the posting
    server.routes["https://redirect.example.net/robots.txt"] = httpx.Response(503)
    server.routes["https://redirect.example.net/r/1"] = httpx.Response(
        302, headers={"Location": gh}
    )
    result = resolve(conn, settings, factory)
    assert result.counts()["deferred"] == 1 and linked(conn) == set()
    del server.routes["https://redirect.example.net/robots.txt"]
    reset_shared_state()
    resolve(conn, settings, factory, now=NIGHT2)
    link = applylink.get_apply_link(conn, gid)
    assert link.ats == "greenhouse" and link.status.value == "live"


def test_no_group_starts_after_the_deadline(conn, settings, factory, server):
    for gid in range(1, 6):
        add(conn, gid, skills=90)
    ticks = iter(range(0, 1000, 4))  # each look at the clock is 4 s later
    result = apply_pass.resolve_likely(
        conn,
        settings,
        PROFILE,
        now=NOW,
        limit=10,
        max_seconds=10,
        ctx_factory=factory,
        clock=lambda: next(ticks),
    )
    counts = result.counts()
    assert (counts["tried"], counts["late"], counts["left"]) == (2, 3, 3)
    assert len(linked(conn)) == 2


def test_run_passes_the_deadline(conn, factory, tmp_path, monkeypatch):
    seen = {}

    def spy(*a, **kw):
        seen.update(kw)
        return apply_pass.PassResult()

    monkeypatch.setattr(apply_pass, "resolve_likely", spy)
    s = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "c")}, "apply_links": {"max_minutes": 7}}
    )
    go(conn, s, factory)
    assert seen["max_seconds"] == 7 * 60 and seen["limit"] == 200


def test_no_url_is_logged(conn, settings, factory, caplog):
    gid = add(conn, 1, skills=90, url="mailto:jobs@example.com")
    with caplog.at_level("INFO", logger="jobhunter.pipeline.apply_pass"):
        result = resolve(conn, settings, factory)
    assert result.no_url == 1
    assert any(
        f"group {gid}" in r.getMessage() and "usable URL" in r.getMessage() for r in caplog.records
    )


# ─── threads: shared limiter, Ctrl-C ───────────────────────────────────────


@pytest.fixture
def file_db(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


def live_transport(monkeypatch, server: Server) -> None:
    """Workers' contexts get the mock transport and nothing else: the process-wide limiter,
    the real clock and the pass's own sleep and cancel hooks."""

    def ctx(s, conn, url, **kw):
        assert set(kw) == {"sleep", "cancel"}
        return fetch_context_for_url(s, conn, url, transport=server.transport, **kw)

    monkeypatch.setattr(apply_pass, "fetch_context_for_url", ctx)


def test_workers_share_the_per_host_limiter(file_db, tmp_path, server, monkeypatch):
    for gid in range(1, 4):
        add(file_db, gid, skills=90, url=f"https://one.example.org/j/{gid}")
    live_transport(monkeypatch, server)
    s = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "c")}, "fetch": {"default_rps": 5, "max_retries": 0}}
    )
    result = apply_pass.resolve_likely(file_db, s, PROFILE, now=NOW, limit=3, workers=3)
    assert result.tried == 3 and result.errors == 0
    assert len(server.times) == 4  # robots.txt once, three pages
    gaps = [b - a for a, b in pairwise(server.times)]
    assert min(gaps) >= 0.14  # 0.2 s apart, jitter at most -25%


def test_ctrl_c_stops_within_about_one_request(file_db, tmp_path, server, monkeypatch):
    quick = add(file_db, 1, label="interesting")  # one page: done first
    for gid in (2, 3, 4):  # long redirect chains, a new host per hop
        hops = [f"https://h{gid}-{i}.example.org/r" for i in range(7)]
        add(file_db, gid, label="interesting", url=hops[0])
        for a, b in pairwise(hops):
            server.routes[a] = httpx.Response(302, headers={"Location": b})
    server.delay = 0.05
    live_transport(monkeypatch, server)
    s = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "c")}, "fetch": {"default_rps": 1000}}
    )
    at: list[int] = []

    def interrupt(self, r):
        at.append(len(server.requests))
        raise KeyboardInterrupt

    monkeypatch.setattr(apply_pass._Progress, "tick", interrupt)
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        apply_pass.resolve_likely(file_db, s, PROFILE, now=NOW, limit=10, workers=4)
    assert time.monotonic() - t0 < 5
    # Only requests already in flight finish: at most one per other worker.
    assert len(server.requests) - at[0] <= 3
    assert applylink.get_apply_link(file_db, quick) is not None


# ─── retries of links that are not settled (bug c369bcc) ────────────────────

BOARD = "https://acme.dejobs.org/denver-co/analyst/ABC123/job/"
GH = "https://boards.greenhouse.io/acme/jobs/567"
TRACKER = "https://www.aplitrak.com/?adid=synthetic"
USAJOBS = "https://www.usajobs.gov/job/123456789"


def put_link(conn, gid: int, status: str, final: str, *, chosen_by=None, error=None) -> None:
    at = (NOW - timedelta(days=2)).isoformat()
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at, "
        "verified_at, chosen_by, error) VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?)",
        (gid, f"https://{host(gid)}/j/{gid}", final, status, at, at, chosen_by, error),
    )


def tries(conn, gid: int) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT attempts, last_outcome FROM apply_link_attempt WHERE job_group_id = ?", (gid,)
    ).fetchone()
    return None if row is None else (row[0], row[1])


def test_unresolved_and_board_copy_rows_are_retried(conn, settings, factory, server):
    unresolved = add(conn, 1, skills=90)
    put_link(conn, unresolved, "unresolved", f"https://{host(1)}/j/1", error="HTTP 500")
    board = add(conn, 2, skills=90)
    put_link(conn, board, "live", BOARD)
    server.routes[f"https://{host(2)}/j/2"] = httpx.Response(302, headers={"Location": GH})
    tracker = add(conn, 8, skills=90)
    put_link(conn, tracker, "live", TRACKER)  # a redirect not followed to the end
    server.routes[f"https://{host(8)}/j/8"] = httpx.Response(302, headers={"Location": GH})
    short_board = add(conn, 12, label="interesting")
    put_link(conn, short_board, "live", BOARD)
    server.routes[f"https://{host(12)}/j/12"] = httpx.Response(302, headers={"Location": GH})
    settled = [add(conn, gid, skills=90) for gid in (3, 4, 5, 6, 7, 9, 10)]
    put_link(conn, 3, "live", GH)
    put_link(conn, 4, "live", f"https://{host(4)}/careers/4")  # not a board: settled
    put_link(conn, 5, "expired", GH)
    put_link(conn, 6, "blocked", f"https://{host(6)}/j/6", error=applylink.LOGIN_ERROR)
    put_link(conn, 7, "live", BOARD, chosen_by="user")
    put_link(conn, 9, "live", USAJOBS)  # the federal apply entry point: settled
    put_link(conn, 10, "unresolved", "https://apply.staffing.example.net/j/9", chosen_by="capture")
    # shortlisted groups go through the same settled filter
    settled += [add(conn, 11, label="interesting"), add(conn, 13, app="interested")]
    put_link(conn, 11, "live", GH)
    put_link(conn, 13, "live", f"https://{host(13)}/careers/13")
    picked = {c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NOW)}
    assert picked == {unresolved, board, tracker, short_board}
    result = resolve(conn, settings, factory)
    assert applylink.get_apply_link(conn, unresolved).status.value == "live"
    for gid in (board, tracker, short_board):
        link = applylink.get_apply_link(conn, gid)
        assert link.status.value == "live" and link.final_url.startswith(GH)
        assert tries(conn, gid) == (1, "live")
    assert tries(conn, unresolved) == (1, "live")
    assert result.counts()["live"] == 4
    assert server.hosts() == {host(g) for g in (1, 2, 8, 12)} | {"boards.greenhouse.io"}
    assert all(tries(conn, gid) is None for gid in settled)


def test_a_retry_never_trades_a_board_copy_for_a_worse_link(conn, settings, factory, server):
    gid = add(conn, 1, skills=90)
    put_link(conn, gid, "live", BOARD)
    server.routes[f"https://{host(1)}/j/1"] = httpx.Response(404)
    result = resolve(conn, settings, factory)
    link = applylink.get_apply_link(conn, gid)
    assert link.final_url == BOARD and link.status.value == "live"  # still the best known
    assert result.counts()["kept"] == 1 and tries(conn, gid) == (1, "kept")


def test_a_board_copy_found_counts_as_not_resolved(conn, settings, factory, server):
    gid = add(conn, 1, skills=90)
    server.routes[f"https://{host(1)}/j/1"] = httpx.Response(302, headers={"Location": BOARD})
    result = resolve(conn, settings, factory)
    assert applylink.get_apply_link(conn, gid).final_url == BOARD
    assert result.counts()["board"] == 1 and tries(conn, gid) == (1, "board")
    assert [c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NIGHT2)] == [gid]


def test_tries_are_spaced_and_stop_at_max_attempts(conn, settings, factory, server):
    gid = add(conn, 1, skills=90)
    server.routes[f"https://{host(1)}/j/1"] = httpx.Response(429, headers={"Retry-After": "60"})
    at = NOW
    for n in range(1, 6):
        reset_shared_state()
        result = resolve(conn, settings, factory, now=at)
        assert result.counts()["deferred"] == 1 and tries(conn, gid) == (n, "deferred")
        # one try per run, and none again before the back-off is over
        assert apply_pass.select_groups(conn, PROFILE, now=at + timedelta(minutes=30)) == []
        at += timedelta(days=1)
    before = len(server.requests)
    reset_shared_state()
    result = resolve(conn, settings, factory, now=at + timedelta(days=30))
    assert result.tried == 0 and len(server.requests) == before  # gave up after five
    assert linked(conn) == set() and tries(conn, gid) == (5, "deferred")


def test_backoff_schedule():
    from jobhunter.pipeline import apply_attempts as aa

    def due(count: int, hours_ago: float, max_attempts: int = 5) -> bool:
        a = aa.Attempts(1, count, NOW - timedelta(hours=hours_ago))
        return aa.due(a, now=NOW, max_attempts=max_attempts)

    assert aa.due(None, now=NOW, max_attempts=5) and not aa.due(None, now=NOW, max_attempts=0)
    assert not due(1, 0.5) and due(1, 1)
    assert not due(2, 3) and due(2, 4)
    assert not due(3, 11) and due(3, 12)
    assert not due(4, 19) and due(4, 20) and due(4, 23)  # the next nightly run qualifies
    assert not due(5, 1000) and due(5, 1000, max_attempts=6)


def test_failures_and_missing_urls_count_as_tries(conn, settings, factory, monkeypatch):
    add(conn, 1, skills=90)
    add(conn, 2, skills=90)

    def boom(c, gid, **kw):
        if gid == 1:
            raise RuntimeError("boom")
        raise ValueError("no usable URL")

    monkeypatch.setattr(applylink, "resolve_group", boom)
    result = resolve(conn, settings, factory)
    assert result.errors == 1 and result.no_url == 1
    assert tries(conn, 1) == (1, "error") and tries(conn, 2) == (1, "no_url")


def test_max_attempts_setting_is_read(conn, tmp_path, factory, server):
    s = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "c")}, "apply_links": {"max_attempts": 0}}
    )
    assert Settings().apply_links.max_attempts == 5
    add(conn, 1, skills=90)
    result = apply_pass.resolve_likely(conn, s, PROFILE, now=NOW, limit=10, ctx_factory=factory)
    assert result.tried == 0 and server.requests == []


def test_late_groups_are_not_counted(conn, settings, factory):
    add(conn, 1, skills=90)
    result = apply_pass.resolve_likely(
        conn, settings, PROFILE, now=NOW, limit=10, max_seconds=0, ctx_factory=factory
    )
    assert result.counts()["late"] == 1 and tries(conn, 1) is None


def test_a_capture_is_never_replaced_by_a_retry(conn, settings, factory, server):
    """Review blocker: a capture's Apply destination (chosen_by='capture', unresolved, as
    capture.offer_apply_link writes it) is settled and beats a board copy."""
    gid = add(conn, 1, skills=90)
    dest = "https://apply.staffing.example.net/jobs/9"
    put_link(conn, gid, "unresolved", dest, chosen_by="capture")
    conn.execute("UPDATE apply_link SET start_url = ?, final_url = NULL", (dest,))
    server.routes[f"https://{host(1)}/j/1"] = httpx.Response(302, headers={"Location": BOARD})
    assert apply_pass.select_groups(conn, PROFILE, now=NOW) == []
    # even a walk that runs anyway (a direct retry) keeps it
    link = applylink.resolve_group(conn, gid, now=NOW, ctx_factory=factory, only_if_better=True)
    stored = applylink.get_apply_link(conn, gid)
    assert link.chosen_by == stored.chosen_by == "capture" and stored.start_url == dest


def test_groups_go_in_score_order(conn, settings, factory):
    """The user (c369bcc): "we should process links in order of score". Shortlisted first by
    score (unscored after, newest first); then A and B in one list by score, not by bucket
    letter; never-tried before a retry only on equal score."""
    short_lo = add(conn, 1, skills=70, label="interesting")
    short_hi = add(conn, 2, skills=90, label="interesting")
    put_link(conn, short_hi, "live", BOARD)  # a retry, but the best shortlisted score
    short_old = add(conn, 3, label="interesting")  # no score
    short_new = add(conn, 4, label="interesting")
    retry_hi = add(conn, 5, skills=95)
    put_link(conn, retry_hi, "live", BOARD)
    b_hi = add(conn, 6, skills=93)
    conn.execute("UPDATE job SET description_completeness = 'partial' WHERE id = ?", (b_hi,))
    fresh_lo = add(conn, 7, skills=90)
    tie_retry = add(conn, 9, skills=85)
    put_link(conn, tie_retry, "live", BOARD)
    tie_fresh = add(conn, 8, skills=85)
    picked = apply_pass.select_groups(conn, PROFILE, now=NOW)
    assert [c.group_id for c in picked] == [
        short_hi,
        short_lo,
        short_new,
        short_old,
        retry_hi,
        b_hi,
        fresh_lo,
        tie_fresh,
        tie_retry,
    ]
    why = {c.group_id: c.why for c in picked}
    assert why[b_hi] == "B" and why[fresh_lo] == "A"  # the score outranks the letter
    overall = [c.overall for c in picked[4:]]
    assert overall == sorted(overall, reverse=True)
    result = apply_pass.resolve_likely(
        conn, settings, PROFILE, now=NOW, limit=5, ctx_factory=factory
    )
    assert result.counts()["left"] == 4
    assert tries(conn, retry_hi)[0] == 1 and tries(conn, fresh_lo) is None  # score first


def test_a_better_walk_restarts_the_count(conn, settings, factory, monkeypatch):
    gid = add(conn, 1, skills=90)
    put_link(conn, gid, "live", BOARD)
    conn.execute(
        "INSERT INTO apply_link_attempt (job_group_id, attempts, last_at, walk_version) "
        "VALUES (?, 5, ?, 1)",
        (gid, (NOW - timedelta(days=3)).isoformat()),
    )
    assert apply_pass.select_groups(conn, PROFILE, now=NOW) == []
    monkeypatch.setattr(applylink, "WALK_VERSION", 2)
    monkeypatch.setattr(apply_attempts, "WALK_VERSION", 2)
    monkeypatch.setattr(apply_pass, "WALK_VERSION", 2)
    assert [c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NOW)] == [gid]
    resolve(conn, settings, factory)
    row = conn.execute("SELECT attempts, walk_version FROM apply_link_attempt").fetchone()
    assert tuple(row) == (1, 2)


def test_tries_are_stamped_when_they_start(conn, settings, factory):
    add(conn, 1, skills=90)
    add(conn, 2, skills=85)
    ticks = iter([0.0, 600.0, 1800.0, 3600.0])  # start, group 1 at +10 min, group 2 at +30
    apply_pass.resolve_likely(
        conn, settings, PROFILE, now=NOW, limit=5, ctx_factory=factory, clock=lambda: next(ticks)
    )
    assert apply_attempts.get(conn, 1).last_at == NOW + timedelta(minutes=10)
    assert apply_attempts.get(conn, 2).last_at == NOW + timedelta(minutes=30)
    # a try that started earlier but is counted later never moves last_at back
    apply_attempts.record(conn, 2, now=NOW, outcome="live")
    assert apply_attempts.get(conn, 2).last_at == NOW + timedelta(minutes=30)


def test_a_group_tried_elsewhere_after_selection_is_skipped(conn, settings, factory, server):
    gid = add(conn, 1, skills=90)
    real = apply_pass.select_groups

    def select_then_console_tries(*a, **kw):
        picked = real(*a, **kw)
        apply_attempts.record(conn, gid, now=NOW, outcome="unresolved")  # the job page
        return picked

    apply_pass.select_groups = select_then_console_tries
    try:
        result = resolve(conn, settings, factory)
    finally:
        apply_pass.select_groups = real
    assert result.counts()["skipped"] == 1 and result.tried == 0 and server.requests == []
    assert tries(conn, gid) == (1, "unresolved")


def test_a_better_link_stored_during_the_walk_is_kept(conn, settings, server):
    """Review: only_if_better compares with the row as it is at write time, not as it was
    read before the walk."""
    gid = add(conn, 1, skills=90)
    put_link(conn, gid, "live", BOARD)
    server.routes[f"https://{host(1)}/j/1"] = httpx.Response(302, headers={"Location": BOARD})
    clock = FakeClock()

    def factory(url):
        # another writer (a capture, the console) stores the ATS link mid-walk
        conn.execute(
            "UPDATE apply_link SET final_url = ?, status = 'live', resolved_at = ? "
            "WHERE job_group_id = ?",
            (GH, NOW.isoformat(), gid),
        )
        return fetch_context_for_url(
            settings, conn, url, transport=server.transport, clock=clock, sleep=clock.sleep
        )

    applylink.resolve_group(conn, gid, now=NIGHT2, ctx_factory=factory, only_if_better=True)
    assert applylink.get_apply_link(conn, gid).final_url == GH


def test_a_live_employer_link_is_kept_although_another_start_failed(
    conn, settings, factory, server
):
    """Review: a deferred try used to throw away a live link on the employer's site."""
    gid = add(conn, 1, skills=90, url="https://careers.acmewidgets.com/jobs/1")
    conn.execute("UPDATE job SET employer = 'Acmewidgets' WHERE id = ?", (gid,))
    member(conn, gid, 1, "https://redirect.example.net/r/1")
    server.routes["https://redirect.example.net/r/1"] = httpx.Response(503)
    result = resolve(conn, settings, factory)
    link = applylink.get_apply_link(conn, gid)
    assert link.status.value == "live" and "acmewidgets" in link.final_url
    assert result.counts()["deferred"] == 0 and tries(conn, gid) == (1, "live")


@pytest.mark.parametrize(
    ("answer", "replaced"),
    [(httpx.Response(403), False), (httpx.Response(404), False), (None, True)],
)
def test_retry_keeps_an_employer_row_over_a_board_result(
    conn, settings, factory, server, answer, replaced
):
    """link_rank: an unresolved row on the employer's site outranks an unresolved or dead
    board result (host tier); an equal result replaces the row (ties go to the newer)."""
    gid = add(conn, 1, skills=90, url="https://www.usnlx.com/uber/job/1")
    conn.execute("UPDATE job SET employer = 'Uber' WHERE id = ?", (gid,))
    loop = "https://jobs.uber.com/en/jobs/303432"
    put_link(conn, gid, "unresolved", loop, error="redirect loop")
    if answer is None:  # the same loop again: a tie
        server.routes["https://www.usnlx.com/uber/job/1"] = httpx.Response(
            302, headers={"Location": loop}
        )
        server.routes[loop] = httpx.Response(302, headers={"Location": loop})
    else:
        server.routes["https://www.usnlx.com/uber/job/1"] = answer
    result = resolve(conn, settings, factory)
    link = applylink.get_apply_link(conn, gid)
    assert link.final_url == loop and link.status.value == "unresolved"
    assert (link.resolved_at >= NOW) is replaced  # the stored row is two days old
    assert result.counts()["kept"] == (0 if replaced else 1)


def test_log_line_shows_board_and_kept():
    r = apply_pass.PassResult(tried=5, candidates=5)
    r.outcomes.update({"board": 3, "kept": 2})
    first8 = list(r.counts())[:8]
    assert {"tried", "live", "board", "kept", "deferred", "errors"} <= set(first8)
