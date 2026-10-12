"""Parallel list and resolve (bug 50320dc). MockTransport and virtual clocks; no network.

The clock is per thread: each fan-out thread has its own virtual timeline, and sleeping moves
only that thread's time. In a parallel run every source starts at virtual time 0 (the clock is
reset when its context is made), as if each had its own worker, whichever pool thread actually
picks it up. A request is stamped with its thread's time, so the largest stamp is the run's
virtual makespan, while the shared per-host limiter still spaces requests to one host across
threads (a thread that must wait behind another's slot jumps forward to it).
"""

from __future__ import annotations

import inspect
import logging
import signal
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import ClassVar

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import (
    FetchContext,
    HostRateLimiter,
    TransientFetchError,
    reset_shared_state,
)
from jobhunter.core.models import JobDetail, JobStub, Query, SourceRow
from jobhunter.pipeline import runner

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
INTERVAL = 5.0  # rps 0.2, the spec 008 default
PAGES = 4


class ThreadClock:
    def __init__(self) -> None:
        self._local = threading.local()

    def now(self) -> float:
        return getattr(self._local, "t", 0.0)

    def sleep(self, seconds: float) -> None:
        self._local.t = self.now() + seconds

    def reset(self) -> None:
        self._local.t = 0.0


class Web:
    """MockTransport handler: every page is a tiny 200, robots.txt as configured per host."""

    def __init__(self, clock: ThreadClock) -> None:
        self.clock = clock
        self.robots: dict[str, str] = {}
        self.status: dict[str, int] = {}
        self.hits: list[tuple[str, str, float, str]] = []  # host, path, virtual t, thread
        self._lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        with self._lock:
            self.hits.append((host, path, self.clock.now(), threading.current_thread().name))
        if path == "/robots.txt":
            body = self.robots.get(host)
            return httpx.Response(200, text=body) if body else httpx.Response(404)
        return httpx.Response(self.status.get(host, 200), text=f"<p>{host}{path}</p>")

    def page_times(self, host: str) -> list[float]:
        return sorted(t for h, p, t, _ in self.hits if h == host and p != "/robots.txt")

    @property
    def makespan(self) -> float:
        return max(t for _, _, t, _ in self.hits)


class PagedAdapter:
    """Fetches PAGES search pages per source and one detail page per resolved stub."""

    family = "paged"
    parallel_safe = True
    barrier: ClassVar[threading.Barrier | None] = None

    def search(self, src, query, since, ctx):
        if PagedAdapter.barrier is not None:
            PagedAdapter.barrier.wait()  # every source in flight at once, or this breaks
        for n in range(PAGES):
            ctx.get(f"{src.entry}search?page={n}")
            yield JobStub(
                source_key=src.key,
                external_id=f"{src.key}-{n}",
                title=f"Systems Engineer {n}",
                url=f"{src.entry}job/{n}",
                posted_at=NOW - timedelta(days=n + 1),
                location_raw="Denver, CO",
                needs_resolve=True,
            )

    def resolve(self, stub, ctx):
        ctx.get(stub.url)
        return JobDetail(**(stub.model_dump() | {"description_raw": "<p>Resolved text.</p>"}))


def source(key: str, host: str, *, rps: float = 0.2, family: str = "paged") -> SourceRow:
    return SourceRow.model_validate(
        {
            "key": key,
            "state": "CO",
            "class": "B",
            "name": key,
            "family": family,
            "tier": "http",
            "entry": f"https://{host}/",
            "queries": [Query(title="engineer")],
            "rate_limit": {"rps": rps, "jitter": False},
        }
    )


@pytest.fixture(autouse=True)
def _fresh():
    reset_shared_state()
    PagedAdapter.barrier = None
    yield
    reset_shared_state()
    PagedAdapter.barrier = None


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def settings(tmp_path, workers: int) -> Settings:
    return Settings.model_validate(
        {
            "paths": {"cache_dir": str(tmp_path / "cache")},
            "fetch": {"parallel_sources": workers},
        }
    )


def run(conn, tmp_path, rows, *, workers, stages=("list",), robots=None, status=None):
    clock = ThreadClock()
    web = Web(clock)
    web.robots.update(robots or {})
    web.status.update(status or {})
    limiter = HostRateLimiter()  # one per run, shared by every context, like the real one
    s = settings(tmp_path, workers)

    def ctx_factory(src):
        if workers > 1:
            clock.reset()  # this source starts at 0 on whichever pool thread runs it
        return FetchContext(
            src,
            s,
            conn,
            transport=httpx.MockTransport(web.handle),
            clock=clock.now,
            sleep=clock.sleep,
            limiter=limiter,
        )

    report = runner.run_pipeline(
        conn,
        s,
        rows,
        adapters={"paged": PagedAdapter},
        ctx_factory=ctx_factory,
        profile_loader=lambda: None,
        stages=list(stages),
        now=NOW,
    )
    return report, web


def test_sources_on_different_hosts_take_max_not_sum(conn, tmp_path):
    n = 5
    rows = [source(f"s{i}", f"h{i}.example.gov") for i in range(n)]
    PagedAdapter.barrier = threading.Barrier(n, timeout=10)
    report, web = run(conn, tmp_path, rows, workers=n)
    assert report.exit_code == 0
    one_source = PAGES * INTERVAL  # robots at 0, then a page every 5 s
    assert web.makespan == pytest.approx(one_source)
    assert len({thread for *_, thread in web.hits}) == n


def test_serial_run_takes_the_sum(conn, tmp_path):
    n = 3
    rows = [source(f"s{i}", f"h{i}.example.gov") for i in range(n)]
    _, web = run(conn, tmp_path, rows, workers=1)
    assert web.makespan == pytest.approx(n * PAGES * INTERVAL)


def test_same_host_spacing_holds_across_sources(conn, tmp_path):
    rows = [source(f"s{i}", "shared.example.gov") for i in range(3)]
    PagedAdapter.barrier = threading.Barrier(3, timeout=10)  # all three in flight at once
    _, web = run(conn, tmp_path, rows, workers=3)
    times = sorted(t for h, _, t, _ in web.hits if h == "shared.example.gov")
    assert len(times) == 1 + 3 * PAGES  # robots.txt once for the host, then every page
    gaps = [b - a for a, b in pairwise(times)]
    assert min(gaps) >= INTERVAL - 1e-9
    assert len({thread for *_, thread in web.hits}) == 3  # really ran on three threads


def test_crawl_delay_is_respected_under_concurrency(conn, tmp_path):
    rows = [source(f"s{i}", "slow.example.gov", rps=1.0) for i in range(2)]
    rows.append(source("fast", "fast.example.gov", rps=1.0))
    robots = {"slow.example.gov": "User-agent: *\nCrawl-delay: 10\n"}
    report, web = run(conn, tmp_path, rows, workers=3, robots=robots)
    assert report.exit_code == 0
    slow = web.page_times("slow.example.gov")
    assert len(slow) == 2 * PAGES
    assert min(b - a for a, b in pairwise(slow)) >= 10.0 - 1e-9
    fast = web.page_times("fast.example.gov")
    assert max(fast) <= PAGES * 1.0  # the other host is not held to slow's delay


def test_one_failing_source_does_not_affect_the_others(conn, tmp_path):
    rows = [source(f"s{i}", f"h{i}.example.gov") for i in range(4)]
    report, _ = run(
        conn,
        tmp_path,
        rows,
        workers=4,
        stages=("list", "resolve"),
        status={"h1.example.gov": 403},
    )
    assert report.results["s1"].status == "broken"
    for key in ("s0", "s2", "s3"):
        assert report.results[key].status == "ok"
        assert report.results[key].stubs_found == PAGES
        assert report.results[key].resolved == PAGES
    counts = dict(conn.execute("SELECT source_key, COUNT(*) FROM job GROUP BY source_key"))
    assert counts == {"s0": PAGES, "s2": PAGES, "s3": PAGES}


def test_resolve_runs_hosts_in_parallel(conn, tmp_path):
    n = 4
    rows = [source(f"s{i}", f"h{i}.example.gov") for i in range(n)]
    run(conn, tmp_path, rows, workers=n)  # list
    reset_shared_state()
    report, web = run(conn, tmp_path, rows, workers=n, stages=("resolve",))
    assert report.counts["resolve"]["resolved"] == n * PAGES
    # robots at 0 then one detail page every 5 s per host, all hosts side by side
    assert web.makespan == pytest.approx(PAGES * INTERVAL)


# ─── same rows as a serial run ──────────────────────────────────────────────


class ShuffledAdapter:
    """No network. Earlier sources finish later, so completion order is the reverse of plan
    order; one source fails part way."""

    family = "shuffled"
    parallel_safe = True
    delay: ClassVar[dict[str, float]] = {"a": 0.15, "b": 0.10, "c": 0.05, "d": 0.0}

    def search(self, src, query, since, ctx):
        time.sleep(self.delay[src.key])
        for n in range(1, 6):
            yield JobStub(
                source_key=src.key,
                external_id=f"{src.key}-{n}",
                title=f"Engineer {src.key} {n}",
                url=f"https://example.com/{src.key}/{n}",
                posted_at=NOW - timedelta(days=n),
                needs_resolve=True,
            )
            if src.key == "c" and n == 3:
                raise RuntimeError("layout changed")

    def resolve(self, stub, ctx):
        time.sleep(self.delay[stub.source_key] / 5)
        if stub.external_id == "b-2":
            raise ValueError("one bad posting")
        return JobDetail(**(stub.model_dump() | {"description_raw": f"<p>{stub.title}</p>"}))


def _snapshot(conn):
    jobs = [tuple(r) for r in conn.execute("SELECT * FROM job ORDER BY id")]
    runs = [
        tuple(r)
        for r in conn.execute(
            "SELECT source_key, queries_run, stubs_found, resolved, status, error "
            "FROM run_source ORDER BY source_key"
        )
    ]
    sources = [tuple(r) for r in conn.execute("SELECT key, status, status_note FROM source")]
    state = [
        tuple(r)
        for r in conn.execute(
            "SELECT source_key, max_posted_at_seen, consecutive_failures FROM source_state "
            "ORDER BY source_key"
        )
    ]
    return jobs, runs, sources, state


@pytest.mark.parametrize("max_resolve", [1500, 7])
def test_rows_match_a_serial_run(tmp_path, max_resolve):
    rows = [
        SourceRow.model_validate(
            {
                "key": k,
                "state": "CO",
                "class": "B",
                "name": k,
                "family": "shuffled",
                "tier": "http",
                "entry": f"https://{k}.example.gov/",
                "queries": [Query(title="engineer")],
            }
        )
        for k in "abcd"
    ]
    snaps = []
    for workers in (1, 4):
        c = db.connect(":memory:")
        db.migrate(c)
        runner.run_pipeline(
            c,
            settings(tmp_path, workers),
            rows,
            adapters={"shuffled": ShuffledAdapter},
            ctx_factory=lambda src: nullcontext(),
            profile_loader=lambda: None,
            stages=["list", "resolve"],
            max_resolve=max_resolve,
            now=NOW,
        )
        snaps.append(_snapshot(c))
        c.close()
    serial, parallel = snaps
    assert parallel == serial
    jobs = serial[0]
    assert len(jobs) == 5 + 5 + 3 + 5  # c failed after three stubs; those are kept


# ─── who may run on a fan-out thread ────────────────────────────────────────


def test_registered_adapters_declare_parallel_safety():
    for family, cls in runner.ADAPTERS.items():
        assert isinstance(getattr(cls, "parallel_safe", None), bool), family
        if cls.parallel_safe:
            module_src = inspect.getsource(inspect.getmodule(cls))
            assert ".conn" not in module_src, f"{family} touches the connection"
    assert runner.ADAPTERS["mailalerts"].parallel_safe is False


def test_undeclared_adapter_runs_alone_in_plan_order(conn, tmp_path):
    seen: list[tuple[str, int]] = []
    in_flight = defaultdict(int)
    lock = threading.Lock()

    class Unmarked:
        family = "unmarked"

        def search(self, src, query, since, ctx):
            with lock:
                seen.append((src.key, sum(in_flight.values())))
            return iter(())

        def resolve(self, stub, ctx):  # pragma: no cover - nothing to resolve
            raise AssertionError

    class Marked:
        family = "marked"
        parallel_safe = True

        def search(self, src, query, since, ctx):
            with lock:
                in_flight[src.key] += 1
            time.sleep(0.05)
            with lock:
                in_flight[src.key] -= 1
            return iter(())

        def resolve(self, stub, ctx):  # pragma: no cover
            raise AssertionError

    rows = [
        source("m1", "a.example.gov", family="marked"),
        source("u", "b.example.gov", family="unmarked"),
        source("m2", "c.example.gov", family="marked"),
    ]
    runner.run_pipeline(
        conn,
        settings(tmp_path, 3),
        rows,
        adapters={"marked": Marked, "unmarked": Unmarked},
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: None,
        stages=["list"],
        now=NOW,
    )
    assert seen == [("u", 0)]  # nothing else in flight while it ran


def test_plain_sqlite_connection_runs_serially(tmp_path):
    import sqlite3

    c = sqlite3.connect(":memory:")  # no cross-thread lock: must not fan out
    c.row_factory = sqlite3.Row
    db.migrate(c)
    threads: set[str] = set()

    class Probe:
        family = "probe"
        parallel_safe = True

        def search(self, src, query, since, ctx):
            threads.add(threading.current_thread().name)
            return iter(())

        def resolve(self, stub, ctx):  # pragma: no cover
            raise AssertionError

    rows = [source(f"p{i}", f"p{i}.example.gov", family="probe") for i in range(3)]
    runner.run_pipeline(
        c,
        settings(tmp_path, 4),
        rows,
        adapters={"probe": Probe},
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: None,
        stages=["list"],
        now=NOW,
    )
    assert threads == {threading.main_thread().name}
    c.close()


def test_workers_capped_by_global_concurrency():
    s = Settings.model_validate({"fetch": {"parallel_sources": 12, "global_concurrency": 3}})
    assert runner.fetch_workers(s) == 3
    assert runner.fetch_workers(Settings()) == 6


# ─── review round: write failures, Retry-After, ordering, Ctrl-C ─────────────


def test_a_failed_stub_write_breaks_only_that_source(tmp_path, monkeypatch):
    real = runner.upsert_stubs

    def flaky(conn, stubs, now):
        if stubs and stubs[0].source_key == "b":
            raise sqlite3.OperationalError("database is locked")
        return real(conn, stubs, now)

    monkeypatch.setattr(runner, "upsert_stubs", flaky)
    c = db.connect(":memory:")
    db.migrate(c)
    rows = [
        SourceRow.model_validate(
            {
                "key": k,
                "state": "CO",
                "class": "B",
                "name": k,
                "family": "shuffled",
                "tier": "http",
                "entry": f"https://{k}.example.gov/",
                "queries": [Query(title="engineer")],
            }
        )
        for k in "abd"
    ]
    report = runner.run_pipeline(
        c,
        settings(tmp_path, 4),
        rows,
        adapters={"shuffled": ShuffledAdapter},
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: None,
        stages=["list", "resolve"],
        now=NOW,
    )
    assert report.results["b"].status == "broken"
    assert "database is locked" in report.results["b"].error
    assert {k: report.results[k].status for k in "ad"} == {"a": "ok", "d": "ok"}
    counts = dict(c.execute("SELECT source_key, COUNT(*) FROM job GROUP BY source_key"))
    assert counts == {"a": 5, "d": 5}
    assert c.execute("SELECT ended_at FROM run").fetchone()[0] is not None
    assert c.execute("SELECT status FROM source WHERE key = 'b'").fetchone()[0] == "broken"
    c.close()


def test_retry_after_holds_the_host_for_every_source(conn, tmp_path):
    clock = ThreadClock()
    hits: list[tuple[str, float]] = []

    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        hits.append((request.url.host, clock.now()))
        if len(hits) == 1:
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, text="ok")

    s = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache")}, "fetch": {"max_retries": 0}}
    )
    limiter = HostRateLimiter()
    a = source("a", "shared.example.gov", rps=1.0)
    b = source("b", "shared.example.gov", rps=1.0)
    conn.executemany(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES (?, 'B', ?, 'paged', 'http', ?, 'enabled')",
        [(r.key, r.key, r.entry) for r in (a, b)],
    )

    def ctx(src):
        return FetchContext(
            src,
            s,
            conn,
            transport=httpx.MockTransport(handle),
            clock=clock.now,
            sleep=clock.sleep,
            limiter=limiter,
        )

    with ctx(a) as ca, pytest.raises(TransientFetchError):
        ca.get("https://shared.example.gov/search")  # 429, Retry-After 30, no retries left
    told_at = hits[0][1]
    with ctx(b) as cb:
        cb.get("https://shared.example.gov/other")  # another source on the same host
    assert hits[1][1] >= told_at + 30.0
    with ctx(source("c", "elsewhere.example.gov", rps=1.0)) as cc:
        clock.reset()
        cc.get("https://elsewhere.example.gov/x")
    assert hits[2][1] < 30.0  # other hosts are not held


def test_run_alone_sources_go_last(conn, tmp_path):
    order: list[str] = []

    class Alone:
        family = "alone"

        def search(self, src, query, since, ctx):
            order.append(src.key)
            return iter(())

        def resolve(self, stub, ctx):  # pragma: no cover
            raise AssertionError

    class Pooled(Alone):
        family = "pooled"
        parallel_safe = True

    rows = [
        source("p1", "a.example.gov", family="pooled"),
        source("mail", "b.example.gov", family="alone"),
        source("p2", "c.example.gov", family="pooled"),
    ]
    for workers in (1, 3):
        order.clear()
        runner.run_pipeline(
            conn,
            settings(tmp_path, workers),
            rows,
            adapters={"pooled": Pooled, "alone": Alone},
            ctx_factory=lambda src: nullcontext(),
            profile_loader=lambda: None,
            stages=["list"],
            now=NOW,
        )
        assert order[-1] == "mail", workers


def test_ctrl_c_stops_the_run_within_about_a_request(conn, tmp_path, monkeypatch):
    """Real threads, real time: slow responses at rps 2, then SIGINT to the run thread."""
    started: list[float] = []
    lock = threading.Lock()

    def slow(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        with lock:
            started.append(time.monotonic())
        time.sleep(0.6)  # slower than the 0.5 s interval: the limiter never waits
        return httpx.Response(200, text="<p>page</p>")

    class Endless:
        family = "endless"
        parallel_safe = True

        def search(self, src, query, since, ctx):
            for n in range(30):
                ctx.get(f"{src.entry}search?page={n}")
            return iter(())

        def resolve(self, stub, ctx):  # pragma: no cover
            raise AssertionError

    # The runner's own default context factory, only with the mock transport underneath.
    real_ctx = runner.FetchContext
    monkeypatch.setattr(
        runner,
        "FetchContext",
        lambda *a, **k: real_ctx(*a, transport=httpx.MockTransport(slow), **k),
    )
    rows = [source(f"e{i}", f"e{i}.example.gov", rps=2.0, family="endless") for i in range(3)]
    main = threading.main_thread().ident
    fired: list[float] = []

    def interrupt():
        fired.append(time.monotonic())
        signal.pthread_kill(main, signal.SIGINT)

    timer = threading.Timer(1.5, interrupt)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run_pipeline(
                conn,
                settings(tmp_path, 3),
                rows,
                adapters={"endless": Endless},
                profile_loader=lambda: None,
                stages=["list"],
                now=NOW,
            )
        stopped = time.monotonic()
    finally:
        timer.cancel()
    assert fired
    assert len(started) >= 3  # it was really fetching
    assert stopped - fired[0] < 1.2  # at most one in-flight 0.6 s request per worker
    late = [t for t in started if t > fired[0] + 0.05]
    assert late == []  # no new request after Ctrl-C


def test_each_source_logs_a_fetched_line_from_its_worker(conn, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="jobhunter.pipeline.runner")
    rows = [source(f"s{i}", f"h{i}.example.gov") for i in range(3)]
    PagedAdapter.barrier = threading.Barrier(3, timeout=10)
    run(conn, tmp_path, rows, workers=3)
    fetched = {
        r.getMessage().split(":")[0]: r.threadName
        for r in caplog.records
        if r.getMessage().startswith("fetched ")
    }
    assert set(fetched) == {"fetched s0", "fetched s1", "fetched s2"}
    assert all(name != threading.main_thread().name for name in fetched.values())
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("fetched s0"))
    assert line.startswith(f"fetched s0: {PAGES} stubs from 1 queries in ")
