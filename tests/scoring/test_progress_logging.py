"""INFO progress logging for scoring (bug 85e102e): batch submit and collect, sync chunks,
re-score. The sentinel test checks that no secret, contact, resume or posting text reaches a
log record, at DEBUG."""

# ruff: noqa: F811

from __future__ import annotations

import logging

from test_rescore import SPEC, FakeScorer, now, queue, scoring
from test_screen import (
    NOW,
    SCORER,
    FakeClient,
    add_group,
    conn,  # noqa: F401
    errored,
    profile,  # noqa: F401
    screen_json,
    succeeded,
)

from jobhunter.scoring import rescore, screen

QUOTE = "Write Terraform modules and Go tooling."
KEY_SENTINEL = "sk-ant-api03-SENTINELKEY0123456789abcdefghijklmnop"
EMAIL_SENTINEL = "sentinel.person@example.com"
RESUME_SENTINEL = "RESUMESENTINEL-quokka-7731"
POSTING_SENTINEL = "POSTINGSENTINEL-wombat-5519"


def infos(caplog, logger_prefix="jobhunter") -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.name.startswith(logger_prefix)
    ]


def test_batch_submit_and_collect_log_ids_counts_and_cost(conn, profile, caplog):
    caplog.set_level(logging.INFO)
    for n in (1, 2, 3):
        add_group(conn, n, profile)
    client = FakeClient()
    batch_id = screen.submit_batch(
        conn, client, profile, limit=10, now=NOW, scorer=SCORER, remaining_usd=lambda: 5.0
    )
    submitted = [m for m in infos(caplog) if "submitted screen batch" in m]
    assert len(submitted) == 1
    assert batch_id in submitted[0] and "3 requests" in submitted[0]
    assert "claude-haiku-4-5" in submitted[0] and "hold $" in submitted[0]

    client.messages.batches.results_by_id[batch_id] = [
        succeeded("g1", screen_json([QUOTE])),
        succeeded("g2", screen_json([QUOTE])),
        errored("g3"),
    ]
    caplog.clear()
    screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    collected = [m for m in infos(caplog) if "collected:" in m]
    assert len(collected) == 1
    assert batch_id in collected[0]
    assert "2 succeeded, 2 written" in collected[0] and "1 errored" in collected[0]
    assert "cost $" in collected[0]


def test_collect_logs_in_progress_without_collecting(conn, profile, caplog):
    caplog.set_level(logging.INFO)
    add_group(conn, 1, profile)
    client = FakeClient()
    batch_id = screen.submit_batch(conn, client, profile, limit=10, now=NOW, scorer=SCORER)
    client.messages.batches.status = "in_progress"
    caplog.clear()
    assert screen.collect_batch(conn, client, batch_id, profile, now=NOW).status == "in_progress"
    assert any(batch_id in m and "still in progress" in m for m in infos(caplog))


def test_anthropic_batch_state_change_is_logged_once(caplog):
    from types import SimpleNamespace

    from jobhunter.scoring.scorers import AnthropicScorer

    states = iter(["in_progress", "in_progress", "ended"])

    class Batches:
        def retrieve(self, handle):
            counts = SimpleNamespace(processing=3, succeeded=0, errored=0)
            return SimpleNamespace(processing_status=next(states), request_counts=counts)

    scorer = AnthropicScorer(SimpleNamespace(messages=SimpleNamespace(batches=Batches())))
    caplog.set_level(logging.INFO)
    assert [scorer.ready("msgbatch_poll") for _ in range(3)] == [False, False, True]
    lines = [m for m in infos(caplog, "jobhunter.scoring.scorers") if "msgbatch_poll" in m]
    assert len(lines) == 2  # in_progress once, then ended
    assert "in_progress" in lines[0] and "ended" in lines[1]


def test_sync_scoring_logs_start_and_totals(conn, profile, caplog):
    caplog.set_level(logging.INFO)
    for n in range(1, 4):
        add_group(conn, n, profile)
    res = screen.score_sync(conn, FakeScorer(cost=0.002), profile, limit=50, now=NOW)
    assert res.written == 3
    msgs = infos(caplog)
    assert any("sync scoring 3 groups" in m and "openrouter:test/model" in m for m in msgs)
    done = [m for m in msgs if "sync scoring" in m and "done in" in m]
    assert len(done) == 1 and "3 written" in done[0] and "cost $0.0060" in done[0]


def test_big_sync_run_logs_few_info_lines(conn, profile, caplog):
    """Per-chunk detail is DEBUG: 40 groups in chunks of 8 stay a handful of INFO lines."""
    caplog.set_level(logging.DEBUG)
    for n in range(1, 41):
        add_group(conn, n, profile)
    screen.score_sync(conn, FakeScorer(), profile, limit=50, now=NOW)
    sync_lines = [r for r in caplog.records if r.getMessage().startswith("sync scoring")]
    assert sum(r.levelno == logging.INFO for r in sync_lines) <= 3
    assert sum(r.levelno == logging.DEBUG for r in sync_lines) >= 5


def test_rescore_logs_start_progress_and_finish(conn, profile, caplog):
    caplog.set_level(logging.INFO)
    for n in range(1, 4):
        add_group(conn, n, profile)
    rid = queue(conn, profile)
    scorer = FakeScorer(cost=0.002)
    rescore.run_request(
        conn, rid, profile, scoring(), now=now, scorer_factory=lambda s: scorer, scorer=SPEC
    )
    msgs = infos(caplog)
    start = [m for m in msgs if m.startswith(f"re-score #{rid} start")]
    assert len(start) == 1 and "3 groups" in start[0] and SPEC in start[0]
    end = [m for m in msgs if m.startswith(f"re-score #{rid} done")]
    assert len(end) == 1 and "3 scored" in end[0] and "0 errored" in end[0]


def test_rescore_failure_is_logged_as_failed(conn, profile, caplog):
    caplog.set_level(logging.INFO)
    add_group(conn, 1, profile)
    rid = queue(conn, profile)
    scorer = FakeScorer(fail=RuntimeError("provider down"))
    rescore.run_request(
        conn, rid, profile, scoring(), now=now, scorer_factory=lambda s: scorer, scorer=SPEC
    )
    assert any(m.startswith(f"re-score #{rid} failed in") for m in infos(caplog))


def test_hidden_scorer_spec_never_reaches_the_log(conn, profile, caplog):
    caplog.set_level(logging.DEBUG)
    add_group(conn, 1, profile)
    hidden = "openai-compat:" + KEY_SENTINEL
    scorer = FakeScorer()
    scorer.name = hidden
    screen.score_sync(conn, scorer, profile, limit=5, now=NOW)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY_SENTINEL not in text
    assert "(hidden value " in text


def test_no_sentinel_reaches_any_record_at_debug(conn, profile, caplog, monkeypatch):
    """Keys, contact addresses, resume text and posting text appear in no record, over a batch
    submit, a collect, a sync run and a re-score, with the root logger at DEBUG."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_SENTINEL)
    monkeypatch.setenv("JOBHUNTER_CONTACT", EMAIL_SENTINEL)
    profile = profile.model_copy(update={"resume_text": f"{RESUME_SENTINEL}\n- ran hosts\n"})
    caplog.set_level(logging.DEBUG)
    description = f"{QUOTE} {POSTING_SENTINEL} Run the fleet."
    for n in range(1, 5):
        add_group(conn, n, profile, description=description)
    client = FakeClient()
    batch_id = screen.submit_batch(
        conn, client, profile, limit=2, now=NOW, scorer=SCORER, remaining_usd=lambda: 5.0
    )
    client.messages.batches.results_by_id[batch_id] = [
        succeeded("g1", screen_json([QUOTE])),
        errored("g2"),
    ]
    screen.collect_batch(conn, client, batch_id, profile, now=NOW)
    screen.score_sync(conn, FakeScorer(cost=0.002), profile, limit=5, now=NOW)
    rid = queue(conn, profile)
    rescore.run_request(
        conn,
        rid,
        profile,
        scoring(),
        now=now,
        scorer_factory=lambda s: FakeScorer(),
        scorer=SPEC,
    )
    assert caplog.records, "nothing logged at all"
    for r in caplog.records:
        text = r.getMessage() + (r.exc_text or "") + str(r.args)
        for sentinel in (KEY_SENTINEL, EMAIL_SENTINEL, RESUME_SENTINEL, POSTING_SENTINEL):
            assert sentinel not in text, (sentinel, r.name, text)
