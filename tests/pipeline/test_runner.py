"""Pipeline runner (specs/004). Fake adapters and registry rows; no network."""

from __future__ import annotations

import builtins
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from jobhunter.config import Paths, Settings
from jobhunter.core import db
from jobhunter.core.fetch import AccessDenied, RobotsDisallowed, TransientFetchError
from jobhunter.core.models import JobDetail, JobStub, Query, SourceRow
from jobhunter.pipeline import runner
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def row(key, family="fake", state="CO", **kw):
    return SourceRow.model_validate(
        {
            "key": key,
            "state": state,
            "class": "B",
            "name": key,
            "family": family,
            "tier": "http",
            "entry": "https://example.com/jobs",
            "queries": [Query(title="engineer")],
            **kw,
        }
    )


def stub(key, n, *, desc="<p>Build and run synthetic systems.</p>", needs_resolve=False, **kw):
    return JobStub(
        source_key=key,
        external_id=f"{key}-{n}",
        title=kw.pop("title", f"Systems Engineer {n}"),
        url=f"https://example.com/{key}/{n}",
        posted_at=kw.pop("posted_at", NOW - timedelta(days=n)),
        location_raw="Denver, CO",
        agency_raw=f"Agency {key}",
        description_raw=desc,
        needs_resolve=needs_resolve,
        **kw,
    )


class FakeAdapter:
    family = "fake"
    stubs: ClassVar[dict[str, list[JobStub]]] = {}
    raises: ClassVar[dict[str, Exception]] = {}
    calls: ClassVar[list[tuple]] = []
    resolve_calls: ClassVar[list[str]] = []

    def search(self, src, query, since, ctx):
        FakeAdapter.calls.append((src.key, query.title, since))
        yield from FakeAdapter.stubs.get(src.key, [])
        if src.key in FakeAdapter.raises:
            raise FakeAdapter.raises[src.key]

    def resolve(self, s, ctx):
        FakeAdapter.resolve_calls.append(s.external_id)
        data = s.model_dump() | {"description_raw": "<p>Resolved text for the job.</p>"}
        return JobDetail(**data)


@pytest.fixture(autouse=True)
def _reset():
    FakeAdapter.stubs, FakeAdapter.raises = {}, {}
    FakeAdapter.calls, FakeAdapter.resolve_calls = [], []


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


PROFILE = Profile.model_validate({"hard": {"states_allowed": ["CO"]}})
ADAPTERS = {"fake": FakeAdapter}


def go(conn, rows, *, profile=PROFILE, **kw):
    return runner.run_pipeline(
        conn,
        Settings(),
        rows,
        adapters=kw.pop("adapters", ADAPTERS),
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: profile,
        now=NOW,
        **kw,
    )


def test_dry_run_prints_plan_and_calls_nothing(conn):
    rows = [row("alpha"), row("beta", family="nope"), row("gamma", policy="blocked")]
    plan = runner.dry_run_plan(
        conn, rows, adapters=ADAPTERS, profile=PROFILE, stages=list(runner.STAGES)
    )
    assert "alpha" in plan and "no adapter yet" in plan
    assert "gamma" not in plan  # blocked row excluded
    assert FakeAdapter.calls == []
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0


def test_full_run_advances_jobs(conn):
    FakeAdapter.stubs = {"a": [stub("a", 1), stub("a", 2)]}
    report = go(conn, [row("a")])
    assert report.exit_code == 0
    assert FakeAdapter.calls[0][1] == "engineer"
    stages = {r[0] for r in conn.execute("SELECT stage FROM job")}
    assert stages == {"grouped", "prefiltered"}  # dupes stay grouped
    assert conn.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] >= 1
    assert report.counts["list"] == {"stubs": 2}


def test_run_records_and_watermark(conn):
    FakeAdapter.stubs = {"a": [stub("a", 1), stub("a", 3)]}
    report = go(conn, [row("a")])
    r = conn.execute("SELECT * FROM run").fetchone()
    assert r["exit_status"] == "ok" and r["ended_at"] and json.loads(r["counts"])
    rs = conn.execute("SELECT * FROM run_source WHERE run_id = ?", (report.run_id,)).fetchone()
    assert (rs["queries_run"], rs["stubs_found"], rs["status"]) == (1, 2, "ok")
    st = conn.execute("SELECT * FROM source_state WHERE source_key='a'").fetchone()
    assert st["max_posted_at_seen"].startswith("2026-10-08")
    assert st["last_ok_at"]
    # next run's since = max_posted - overlap
    go(conn, [row("a")])
    assert FakeAdapter.calls[-1][2] == NOW - timedelta(days=1) - timedelta(days=2)


def test_since_and_full_override_watermark(conn):
    go(conn, [row("a")], since=datetime(2026, 9, 1, tzinfo=UTC))
    assert FakeAdapter.calls[-1][2] == datetime(2026, 9, 1, tzinfo=UTC)
    go(conn, [row("a")], full=True)
    assert FakeAdapter.calls[-1][2] == NOW - timedelta(days=60)


def test_stage_selection(conn):
    FakeAdapter.stubs = {"a": [stub("a", 1)]}
    go(conn, [row("a")], stages=runner.parse_stages(["list"]))
    assert conn.execute("SELECT stage FROM job").fetchone()[0] == "listed"
    go(conn, [row("a")], stages=runner.parse_stages(["normalize,dedupe"]))
    assert conn.execute("SELECT stage FROM job").fetchone()[0] == "grouped"
    assert len(FakeAdapter.calls) == 1
    assert runner.parse_stages(["prefilter", "list"]) == ["list", "prefilter"]
    with pytest.raises(ValueError):
        runner.parse_stages(["bogus"])


def test_state_filter():
    rows = [row("co", state="CO"), row("wa", state="WA"), row("fed", state=None)]

    def keys(states):
        return [r.key for r in runner.filter_sources(rows, states)]

    assert keys(None) == ["co", "wa", "fed"]
    assert keys({"CO"}) == ["co"]
    assert keys({"WA", "US"}) == ["wa", "fed"]


def test_state_filter_limits_run(conn):
    go(conn, [row("co"), row("wa", state="WA")], states={"WA"})
    assert [c[0] for c in FakeAdapter.calls] == ["wa"]


def test_transient_error_suspect_run_continues(conn):
    FakeAdapter.stubs = {"b": [stub("b", 1)], "a": [stub("a", 1)]}
    FakeAdapter.raises = {"a": TransientFetchError("https://x", "503")}
    report = go(conn, [row("a"), row("b")])
    assert report.exit_code == 1
    assert report.results["a"].status == "suspect"
    assert report.results["b"].status == "ok"
    assert conn.execute("SELECT status FROM source WHERE key='a'").fetchone()[0] == "suspect"
    st = conn.execute("SELECT * FROM source_state WHERE source_key='a'").fetchone()
    assert st["consecutive_failures"] == 1 and st["last_ok_at"] is None
    # partial stubs kept; later stages still ran for the healthy source
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2
    assert "a: suspect" in runner.format_summary(report)
    assert "b:" not in runner.format_summary(report)
    rs = conn.execute("SELECT status, error FROM run_source WHERE source_key='a'").fetchone()
    assert rs["status"] == "suspect" and "503" in rs["error"]
    assert conn.execute("SELECT exit_status FROM run").fetchone()[0] == "degraded"


def test_access_denied_broken_and_blocked(conn):
    FakeAdapter.raises = {
        "a": AccessDenied("https://x", 403),
        "b": RobotsDisallowed("https://x"),
    }
    report = go(conn, [row("a"), row("b")])
    assert report.results["a"].status == "broken"
    assert report.results["b"].status == "blocked"
    assert report.exit_code == 1


def test_unexpected_exception_is_broken_not_fatal(conn):
    FakeAdapter.raises = {"a": RuntimeError("boom")}
    report = go(conn, [row("a"), row("b")])
    assert report.results["a"].status == "broken"
    assert report.results["b"].status == "ok"


def test_source_recovers_to_ok(conn):
    FakeAdapter.raises = {"a": TransientFetchError("https://x", "503")}
    go(conn, [row("a")])
    FakeAdapter.raises = {}
    go(conn, [row("a")])
    assert conn.execute("SELECT status FROM source WHERE key='a'").fetchone()[0] == "ok"
    st = conn.execute("SELECT consecutive_failures FROM source_state").fetchone()
    assert st[0] == 0


def test_no_adapter_skipped_not_error(conn):
    report = go(conn, [row("x", family="nope")])
    assert report.exit_code == 0
    assert any("no adapter yet" in m for m in report.messages)
    assert conn.execute("SELECT COUNT(*) FROM run_source").fetchone()[0] == 0


def test_resolve_stage_and_cap(conn):
    FakeAdapter.stubs = {
        "a": [stub("a", n, desc=None, needs_resolve=True) for n in (1, 2, 3)],
    }
    go(conn, [row("a")], stages=["list", "resolve"], max_resolve=2)
    assert FakeAdapter.resolve_calls == ["a-1", "a-2"]  # newest first, capped
    got = conn.execute("SELECT stage, description_raw FROM job ORDER BY id").fetchall()
    assert [g[0] for g in got] == ["resolved", "resolved", "listed"]
    go(conn, [row("a")], stages=["resolve"])
    assert FakeAdapter.resolve_calls[-1] == "a-3"


def test_resolve_skips_closed_and_described(conn):
    FakeAdapter.stubs = {
        "a": [
            stub("a", 1, desc=None, needs_resolve=True, closes_at=NOW - timedelta(days=1)),
            stub("a", 2),
        ]
    }
    go(conn, [row("a")], stages=["list", "resolve"])
    assert FakeAdapter.resolve_calls == []


def test_no_profile_skips_profile_stages_but_ingests(conn):
    FakeAdapter.stubs = {"a": [stub("a", 1)]}
    report = go(conn, [row("a")], profile=None)
    assert report.exit_code == 0
    assert conn.execute("SELECT stage FROM job").fetchone()[0] == "grouped"
    text = "\n".join(report.messages)
    assert "prefilter: skipped" in text and "score: skipped" in text


def test_from_profile_source_skipped_without_profile(conn):
    report = go(conn, [row("a", queries="from_profile")], profile=None)
    assert FakeAdapter.calls == []
    assert any("no profile" in m for m in report.messages)


def test_score_skipped_when_screen_missing(conn, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "jobhunter.scoring" and "screen" in (fromlist or ()):
            raise ImportError("no screen")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    report = go(conn, [row("a")], stages=["score"])
    assert "screen stage not installed" in "\n".join(report.messages)
    assert report.exit_code == 0


def test_default_profile_loader_missing_dir(tmp_path):
    s = Settings(paths=Paths(profile_dir=tmp_path / "nope"))
    assert runner.default_profile_loader(s)() is None
