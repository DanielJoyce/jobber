"""Backfill (specs/006 Cost, specs/004 watermarks). Fake adapters and clients; no network."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest
from anthropic.types.messages import MessageBatchIndividualResponse

from jobhunter.config import Settings
from jobhunter.core import db
from jobhunter.core.models import JobDetail, JobStub, Query, SourceRow
from jobhunter.pipeline import backfill as bf
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import ScoreResult

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SCORER = "anthropic:claude-haiku-4-5"
HAIKU_BATCH_PER_JOB = 0.002025  # compute_cost of TYPICAL_USAGE, batched Haiku
PROFILE = Profile.model_validate({"hard": {"states_allowed": ["CO"]}})
ADAPTERS = {"fake": lambda: FakeAdapter()}


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


TOPICS = ["linux fleet", "network", "data pipelines", "security audit", "help desk", "storage"]


def stub(key, n, *, posted_days=None):
    """Distinct titles and descriptions so near-duplicate dedupe does not merge them."""
    days = n if posted_days is None else posted_days
    topic = TOPICS[n % len(TOPICS)]
    return JobStub(
        source_key=key,
        external_id=f"{key}-{n}",
        title=f"{topic.title()} specialist",
        url=f"https://example.com/{key}/{n}",
        posted_at=NOW - timedelta(days=days),
        location_raw="Denver, CO",
        agency_raw=f"Agency {key}",
        description_raw=f"<p>Own the {topic} work for the synthetic team {n}.</p>",
        needs_resolve=False,
    )


class FakeAdapter:
    family = "fake"
    stubs: ClassVar[dict[str, list[JobStub]]] = {}
    calls: ClassVar[list[tuple]] = []

    def search(self, src, query, since, ctx):
        FakeAdapter.calls.append((src.key, since))
        yield from FakeAdapter.stubs.get(src.key, [])

    def resolve(self, s, ctx):
        return JobDetail(**(s.model_dump() | {"description_raw": "<p>x</p>"}))


@pytest.fixture(autouse=True)
def _reset():
    FakeAdapter.stubs = {"a": [stub("a", n) for n in range(1, 6)]}
    FakeAdapter.calls = []


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


# ─── fake Anthropic batch client (shape as in tests/scoring/test_screen.py) ──


def screen_json(quote: str = "Build and run synthetic systems.") -> str:
    return json.dumps(
        {
            "verdict": "strong",
            "dimensions": {
                "skills": {"score": 88, "why": "Daily work."},
                "seniority": {"score": 80, "why": "match: senior IC."},
                "domain": {"score": 70, "why": "Public sector."},
            },
            "raw_skills": 90,
            "recency_weighted_skills": 88,
            "stale_skills": [],
            "current_focus_overlap": 85,
            "done_with_hits": [],
            "evidence": [{"claim": "Systems work", "quote": quote}],
            "blockers": [],
            "missing_info": ["salary range"],
            "shape_flags": [],
            "tailoring_hints": ["Lead with systems work."],
        }
    )


def succeeded(custom_id: str) -> MessageBatchIndividualResponse:
    message = {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": screen_json()}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 350,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 0,
        },
    }
    return MessageBatchIndividualResponse.model_validate(
        {"custom_id": custom_id, "result": {"type": "succeeded", "message": message}}
    )


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict]] = []
        self.results_by_id: dict[str, list] = {}
        self.status = "ended"

    def create(self, *, requests):
        self.created.append(list(requests))
        batch_id = f"msgbatch_{len(self.created)}"
        self.results_by_id[batch_id] = [succeeded(r["custom_id"]) for r in requests]
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def results(self, batch_id):
        return iter(self.results_by_id[batch_id])


class FakeClient:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(batches=FakeBatches())

    @property
    def jobs_sent(self) -> int:
        return sum(len(b) for b in self.messages.batches.created)


class FakeSyncScorer:
    """A non-batching scorer with a fixed per-job cost."""

    name = "fake:scorer"
    supports_batching = False
    per_job = 0.01

    def __init__(self) -> None:
        self.sent = 0

    def submit(self, requests):
        self.sent += len(requests)
        return [
            ScoreResult(
                custom_id=r.custom_id,
                text=screen_json(),
                usage=SimpleNamespace(
                    input_tokens=2000,
                    output_tokens=350,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
                model="fake-model",
                stop_reason="end_turn",
            )
            for r in requests
        ]

    def ready(self, handle):
        return True

    def collect(self, handle):
        raise AssertionError("sync scorer has nothing to collect")

    def score_one(self, request):
        raise AssertionError("not used")

    def cost(self, usage, *, batch=False):
        return self.per_job


def run(conn, *, scorer=None, client=None, **kw):
    lines: list[str] = []
    clock_s = [0.0]

    def sleep(seconds):
        clock_s[0] += seconds

    result = bf.run_backfill(
        conn,
        Settings(),
        [row("a")],
        scorer=scorer or _batch_scorer(client),
        client=client,
        days=kw.pop("days", 30),
        budget_usd=kw.pop("budget_usd", 10.0),
        adapters=ADAPTERS,
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: PROFILE,
        now=NOW,
        out=lines.append,
        sleep=sleep,
        clock=lambda: clock_s[0],
        **kw,
    )
    return result, "\n".join(lines)


def _batch_scorer(client):
    from jobhunter.scoring.scorers import AnthropicScorer

    return AnthropicScorer(client, SCORER)


def eligible_count(conn, scorer=None) -> int:
    scorer = scorer or _batch_scorer(FakeClient())
    return bf.estimate(conn, PROFILE, scorer, budget_usd=0.0, now=NOW).eligible


def scored_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM fit_score").fetchone()[0]


# ─── tests ──────────────────────────────────────────────────────────────────


def test_dry_run_prints_estimate_and_submits_nothing(conn):
    client = FakeClient()
    result, text = run(conn, client=client, dry_run=True, confirm=lambda est: True)
    assert result.status == "dry_run"
    assert "Estimate: 5 eligible groups" in text
    assert "Dry run" in text
    assert "Daily cap bypassed" in text
    assert client.messages.batches.created == []
    assert conn.execute("SELECT COUNT(*) FROM score_batch").fetchone()[0] == 0


def test_without_yes_nothing_is_submitted(conn):
    client = FakeClient()
    result, text = run(conn, client=client)  # default confirm refuses
    assert result.status == "declined"
    assert result.exit_code == 2
    assert "Pass --yes" in text
    assert client.messages.batches.created == []


def test_budget_stops_submission_mid_way(conn):
    client = FakeClient()
    budget = 2.5 * HAIKU_BATCH_PER_JOB  # covers exactly two jobs
    result, text = run(
        conn, client=client, budget_usd=budget, chunk_size=1, confirm=lambda est: True
    )
    assert result.status == "ok"
    assert result.submitted == 2
    assert [len(b) for b in client.messages.batches.created] == [1, 1]
    assert "budget reached" in text
    assert eligible_count(conn) == 3  # three groups wait for the next run
    assert result.committed_usd <= budget


def test_budget_counts_spend_already_today(conn):
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) "
        "VALUES ('2026-10-09', 'x', 'screen', 1, 0.009)"
    )
    client = FakeClient()
    budget = 0.009 + 1.5 * HAIKU_BATCH_PER_JOB
    result, _ = run(conn, client=client, budget_usd=budget, confirm=lambda est: True)
    assert result.submitted == 1
    assert client.jobs_sent == 1


def test_exhausted_budget_submits_nothing(conn):
    client = FakeClient()
    result, text = run(conn, client=client, budget_usd=0.0, confirm=lambda est: True)
    assert result.status == "budget_exhausted"
    assert client.messages.batches.created == []
    assert "Budget exhausted" in text


def test_watermarks_never_move_backward_and_window_ignores_them(conn):
    run_once = dict(confirm=lambda est: True, dry_run=True)
    FakeAdapter.stubs = {"a": [stub("a", 1, posted_days=1)]}
    run(conn, client=FakeClient(), **run_once)
    newest = conn.execute("SELECT max_posted_at_seen FROM source_state").fetchone()[0]

    # Older postings only: the watermark stays where it was.
    FakeAdapter.stubs = {"a": [stub("a", 9, posted_days=9)]}
    run(conn, client=FakeClient(), **run_once)
    assert conn.execute("SELECT max_posted_at_seen FROM source_state").fetchone()[0] == newest

    # The window is now - days, not the watermark.
    assert FakeAdapter.calls[-1] == ("a", NOW - timedelta(days=30))

    # A newer posting moves it forward.
    FakeAdapter.stubs = {"a": [stub("a", 0, posted_days=0)]}
    run(conn, client=FakeClient(), **run_once)
    assert conn.execute("SELECT max_posted_at_seen FROM source_state").fetchone()[0] > newest


def test_rerun_with_same_args_submits_nothing_new(conn):
    client = FakeClient()
    first, _ = run(conn, client=client, confirm=lambda est: True)
    assert first.submitted == 5
    second, text = run(conn, client=client, confirm=lambda est: True)
    assert second.status == "nothing"
    assert len(client.messages.batches.created) == 1
    assert "Nothing to screen" in text


def test_wait_collects_batches_and_records_spend(conn):
    client = FakeClient()
    result, text = run(conn, client=client, wait=True, confirm=lambda est: True)
    assert result.uncollected == []
    assert scored_count(conn) == 5
    assert "collected msgbatch_1: 5 written" in text
    assert conn.execute("SELECT SUM(calls) FROM llm_spend").fetchone()[0] == 5


def test_without_wait_prints_collect_command(conn):
    result, text = run(conn, client=FakeClient(), confirm=lambda est: True)
    assert result.uncollected == result.batch_ids
    assert "jobhunter score --collect-pending" in text
    assert scored_count(conn) == 0


def test_wait_gives_up_after_max_wait(conn):
    client = FakeClient()
    client.messages.batches.status = "in_progress"
    result, text = run(
        conn,
        client=client,
        wait=True,
        max_wait_s=120.0,
        poll_s=60.0,
        confirm=lambda est: True,
    )
    assert result.uncollected == result.batch_ids
    assert "stopped waiting" in text
    assert scored_count(conn) == 0


def test_sync_scorer_respects_budget(conn):
    scorer = FakeSyncScorer()
    budget = 0.035  # three jobs at a fixed $0.01 each
    result, text = run(
        conn, scorer=scorer, budget_usd=budget, chunk_size=2, confirm=lambda est: True
    )
    assert result.status == "ok"
    assert scorer.sent == 3
    assert scored_count(conn) == 3
    assert result.committed_usd == pytest.approx(0.03)
    assert "budget reached" in text
    assert eligible_count(conn, scorer) == 2


def test_sync_scorer_rerun_is_idempotent(conn):
    scorer = FakeSyncScorer()
    run(conn, scorer=scorer, confirm=lambda est: True)
    assert scored_count(conn) == 5
    second, _ = run(conn, scorer=scorer, confirm=lambda est: True)
    assert second.status == "nothing"
    assert scorer.sent == 5


def test_per_job_cost_uses_scorer_price_then_measured_history(conn):
    scorer = FakeSyncScorer()
    per = bf.per_job_cost(conn, scorer)
    assert per.usd == pytest.approx(0.01) and "scorer price" in per.source
    assert bf.per_job_cost(conn, _batch_scorer(FakeClient())).usd == pytest.approx(
        HAIKU_BATCH_PER_JOB
    )
    run(conn, scorer=scorer, confirm=lambda est: True)
    scorer.per_job = 0.05  # history wins over the price table once spend is measured
    per = bf.per_job_cost(conn, scorer)
    assert per.usd == pytest.approx(0.01) and "measured" in per.source


def test_unpriced_scorer_falls_back_to_default(conn):
    class Unpriced(FakeSyncScorer):
        def cost(self, usage, *, batch=False):
            return 0.0

    per = bf.per_job_cost(conn, Unpriced())
    assert per.usd == pytest.approx(bf.FALLBACK_COST_PER_JOB_USD)
    assert "default" in per.source


def test_window_notes_name_adapter_caps():
    rows = [row("u", family="usajobs", state="US"), row("n", family="nlx", state="NE")]
    notes = bf.window_notes(rows, None, 90)
    assert any("caps DatePosted at 60 days" in n for n in notes)
    assert any("max_pages=20" in n and "nlx n" in n for n in notes)
    short = bf.window_notes(rows, None, 30)
    assert not any("DatePosted" in n for n in short)
