"""Resolve skips detail pages already pulled (bug 0cd0320). MockTransport counts requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext, reset_shared_state
from jobhunter.core.models import JobDetail, JobStub, Query, SourceRow
from jobhunter.pipeline import runner

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
HOST = "https://jobs.example.gov"


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Site:
    """Fake board: robots allow all; a detail page returns 500 for paths in ``broken``."""

    def __init__(self) -> None:
        self.details: list[str] = []
        self.broken: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        self.details.append(request.url.path)
        if request.url.path in self.broken:
            return httpx.Response(404, text="gone")
        return httpx.Response(200, text=f"<p>Detail for {request.url.path}</p>")


class DetailAdapter:
    """Lists ``stubs[key]`` without a request; resolve GETs the stub URL through ctx."""

    family = "detail"
    stubs: ClassVar[dict[str, list[JobStub]]] = {}

    def search(self, src, query, since, ctx):
        yield from DetailAdapter.stubs.get(src.key, [])

    def resolve(self, s, ctx):
        resp = ctx.get(s.url)
        if resp.status != 200:
            raise ValueError(f"detail {resp.status}")
        return JobDetail(**(s.model_dump() | {"description_raw": resp.text}))


def stub(key: str, n: int, *, ext: str | None = None, url: str | None = None) -> JobStub:
    return JobStub(
        source_key=key,
        external_id=ext or f"{key}-{n}",
        title=f"Systems Engineer {n}",
        url=url or f"{HOST}/job/{n}",
        posted_at=NOW - timedelta(days=n),
        location_raw="Denver, CO",
        agency_raw="Agency",
        needs_resolve=True,
    )


def row(key: str) -> SourceRow:
    return SourceRow.model_validate(
        {
            "key": key,
            "state": "CO",
            "class": "B",
            "name": key,
            "family": "detail",
            "tier": "http",
            "entry": f"{HOST}/",
            "queries": [Query(title="engineer")],
        }
    )


@pytest.fixture(autouse=True)
def _state():
    reset_shared_state()
    DetailAdapter.stubs = {}
    yield
    reset_shared_state()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def site():
    return Site()


def go(conn, site, tmp_path, keys=("a",), **kw):
    settings = Settings.model_validate(
        {
            "paths": {"cache_dir": str(tmp_path / "cache")},
            "fetch": {"max_retries": 0},
        }
    )
    clock = Clock()
    transport = httpx.MockTransport(site.handle)
    return runner.run_pipeline(
        conn,
        settings,
        [row(k) for k in keys],
        adapters={"detail": DetailAdapter},
        ctx_factory=lambda s: FetchContext(
            s, settings, conn, transport=transport, clock=clock, sleep=clock.sleep
        ),
        profile_loader=lambda: None,
        stages=kw.pop("stages", ["list", "resolve", "normalize", "dedupe"]),
        apply_ctx_factory=lambda url: (_ for _ in ()).throw(AssertionError("no apply fetch")),
        now=NOW,
        **kw,
    )


def test_second_run_with_same_stubs_fetches_nothing(conn, site, tmp_path):
    DetailAdapter.stubs = {"a": [stub("a", 1), stub("a", 2)]}
    first = go(conn, site, tmp_path)
    assert sorted(site.details) == ["/job/1", "/job/2"]
    assert first.counts["resolve"] == {"resolved": 2, "skipped": 0, "apply_link_login_walls": 0}

    site.details.clear()
    second = go(conn, site, tmp_path)
    assert site.details == []
    assert second.counts["resolve"]["resolved"] == 0


def test_same_url_under_new_id_reuses_stored_detail(conn, site, tmp_path):
    """The board renumbers its rows, and another source lists the same page with tracking
    params: no request, the stored text is copied, and the rows still normalize and dedupe."""
    DetailAdapter.stubs = {"a": [stub("a", 1)]}
    go(conn, site, tmp_path)
    site.details.clear()

    DetailAdapter.stubs = {
        "a": [stub("a", 1, ext="renumbered-1")],
        "b": [stub("b", 1, url=f"{HOST}/job/1/?utm_source=feed")],
    }
    report = go(conn, site, tmp_path, keys=("a", "b"))
    assert site.details == []
    assert report.counts["resolve"]["skipped"] == 2
    assert report.results["a"].skipped == 1 and report.results["b"].skipped == 1
    rows = conn.execute(
        "SELECT description_raw, needs_resolve, stage, job_group_id FROM job ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert {r["description_raw"] for r in rows} == {"<p>Detail for /job/1</p>"}
    assert all(r["needs_resolve"] == 0 for r in rows)
    assert all(r["stage"] not in ("listed", "resolved") for r in rows)  # normalized onward
    assert len({r["job_group_id"] for r in rows}) == 1  # deduped into one group


def test_new_urls_are_still_fetched(conn, site, tmp_path):
    DetailAdapter.stubs = {"a": [stub("a", 1)]}
    go(conn, site, tmp_path)
    site.details.clear()
    DetailAdapter.stubs = {"a": [stub("a", 1), stub("a", 3)]}
    report = go(conn, site, tmp_path)
    assert site.details == ["/job/3"]
    assert report.counts["resolve"]["resolved"] == 1


def test_previously_failed_fetch_is_retried_and_never_reused(conn, site, tmp_path):
    site.broken = {"/job/1"}
    DetailAdapter.stubs = {"a": [stub("a", 1)]}
    go(conn, site, tmp_path)
    assert site.details == ["/job/1"]
    assert runner.pulled_details(conn) == {}

    # Same URL under another id while the first fetch is still failed: fetched, not reused.
    site.broken = set()
    site.details.clear()
    DetailAdapter.stubs = {"a": [stub("a", 1)], "b": [stub("b", 9, url=f"{HOST}/job/1")]}
    report = go(conn, site, tmp_path, keys=("a", "b"))
    # One fetch for the failed row; the second row reuses it within the same run.
    assert site.details == ["/job/1"]
    assert report.counts["resolve"]["resolved"] == 1
    assert report.counts["resolve"]["skipped"] == 1


def test_partial_detail_is_not_pulled(conn, site, tmp_path):
    DetailAdapter.stubs = {"a": [stub("a", 1)]}
    go(conn, site, tmp_path)
    conn.execute("UPDATE job SET description_completeness = 'partial'")
    conn.commit()
    assert runner.pulled_details(conn) == {}


def test_refetch_details_fetches_again(conn, site, tmp_path):
    DetailAdapter.stubs = {"a": [stub("a", 1)], "b": [stub("b", 2)]}
    go(conn, site, tmp_path, keys=("a", "b"))
    site.details.clear()

    go(conn, site, tmp_path, keys=("a", "b"), refetch_details={"b"})
    assert site.details == ["/job/2"]  # scoped to source b

    site.details.clear()
    report = go(conn, site, tmp_path, keys=("a", "b"), refetch_details=True)
    assert sorted(site.details) == ["/job/1", "/job/2"]
    assert report.counts["resolve"] == {"resolved": 2, "skipped": 0, "apply_link_login_walls": 0}
    # Rows already past normalize keep their stage.
    stages = {r[0] for r in conn.execute("SELECT stage FROM job")}
    assert "resolved" not in stages and "listed" not in stages


def test_refetch_that_fails_keeps_stored_text(conn, site, tmp_path):
    DetailAdapter.stubs = {"a": [stub("a", 1)]}
    go(conn, site, tmp_path)
    site.broken = {"/job/1"}
    go(conn, site, tmp_path, refetch_details=True)
    stored = conn.execute("SELECT description_raw, needs_resolve FROM job").fetchone()
    assert tuple(stored) == ("<p>Detail for /job/1</p>", 0)


def test_cli_source_needs_refetch_flag():
    res = CliRunner().invoke(app, ["run", "--source", "a"])
    assert res.exit_code == 2
    assert "--source needs --refetch-details" in res.output
