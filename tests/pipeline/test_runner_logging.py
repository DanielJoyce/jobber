"""Progress logging for a pipeline run (bug 85e102e): stage start/end, one line per source,
resolve progress, and no personal text in any record. Fake adapters; no network."""

# ruff: noqa: F811

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

from test_runner import (
    ADAPTERS,
    PROFILE,
    FakeAdapter,
    _reset,  # noqa: F401
    conn,  # noqa: F401
    go,
    row,
    stub,
)

from jobhunter.config import Fetch, Settings
from jobhunter.pipeline import runner

SECRET_EMAIL = "sentinel.person@example.com"
POSTING_SENTINEL = "POSTINGSENTINEL-wombat-5519"


INGEST = ["list", "resolve", "normalize", "dedupe", "prefilter"]


def info_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_seeded_run_logs_each_stage_and_each_source(conn, caplog):
    caplog.set_level(logging.INFO)
    FakeAdapter.stubs = {"co-a": [stub("co-a", 1), stub("co-a", 2)], "wa-b": [stub("wa-b", 1)]}
    report = go(conn, [row("co-a"), row("wa-b", state="WA")], stages=INGEST)
    msgs = info_lines(caplog)

    assert any(m.startswith(f"run {report.run_id} start:") and "sources=2" in m for m in msgs)
    assert any(m.startswith(f"run {report.run_id} end: status=ok") for m in msgs)
    for stage in ("list", "resolve", "normalize", "dedupe", "prefilter"):
        assert any(m.startswith(f"stage {stage} done in") for m in msgs), stage
    # State boards appear as distinct sources, each with its own stub count.
    co = [m for m in msgs if m.startswith("list co-a:")]
    wa = [m for m in msgs if m.startswith("list wa-b:")]
    assert len(co) == 1 and "stubs=2" in co[0] and "status=ok" in co[0]
    assert len(wa) == 1 and "stubs=1" in wa[0]
    assert any("stage list done" in m and "3 stubs from 2 sources" in m for m in msgs)


def test_list_line_reports_pages_robots_skips_and_errors(conn, caplog):
    caplog.set_level(logging.INFO)
    FakeAdapter.stubs = {"a": [stub("a", 1)]}

    @contextmanager
    def ctx_with_stats(src):
        yield SimpleNamespace(stats={"pages": 4, "cached": 2, "robots_skips": 1})

    FakeAdapter.raises = {"b": RuntimeError("boom")}
    runner.run_pipeline(
        conn,
        Settings(),
        [row("a"), row("b")],
        adapters=ADAPTERS,
        ctx_factory=ctx_with_stats,
        profile_loader=lambda: PROFILE,
        stages=["list"],
    )
    msgs = info_lines(caplog)
    a = next(m for m in msgs if m.startswith("list a:"))
    assert "pages=4 (cached 2)" in a and "robots_skips=1" in a and "errors=0" in a
    b = next(m for m in msgs if m.startswith("list b:"))
    assert "status=broken" in b and "errors=1" in b


def test_resolve_logs_per_source_totals(conn, caplog):
    caplog.set_level(logging.INFO)
    FakeAdapter.stubs = {"a": [stub("a", n, desc="", needs_resolve=True) for n in (1, 2, 3)]}
    go(conn, [row("a")], stages=["list", "resolve"])
    msgs = info_lines(caplog)
    assert any(m.startswith("resolve a: 3 resolved, 0 errors") for m in msgs)
    assert any(m.startswith("stage resolve done") and "3 resolved" in m for m in msgs)


def test_resolve_progress_is_periodic_not_per_row(conn, caplog, monkeypatch):
    monkeypatch.setattr(runner, "PROGRESS_EVERY", 5)
    FakeAdapter.stubs = {"a": [stub("a", n, desc="", needs_resolve=True) for n in range(1, 13)]}
    caplog.set_level(logging.INFO)
    go(conn, [row("a")], stages=["list", "resolve"])
    progress = [m for m in info_lines(caplog) if "postings resolved so far" in m]
    assert len(progress) == 2  # after 5 and after 10, not after each of 12


def test_no_personal_text_in_any_record(conn, caplog):
    caplog.set_level(logging.DEBUG)
    settings = Settings(fetch=Fetch(user_agent_contact=SECRET_EMAIL))
    FakeAdapter.stubs = {"a": [stub("a", 1, desc=f"<p>Build systems. {POSTING_SENTINEL}</p>")]}
    go(conn, [row("a")], settings=settings, stages=INGEST)
    assert caplog.records
    for r in caplog.records:
        text = r.getMessage() + str(r.args)
        assert SECRET_EMAIL not in text and POSTING_SENTINEL not in text, text
        assert "Systems Engineer" not in text  # titles are logged as ids, not text
