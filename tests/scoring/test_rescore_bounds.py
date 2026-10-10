"""Re-score money path: each group once per run, the confirmed plan and spend bound the run,
recent scope, per-job cost, superseding, lost claims, read-only planning. Fake scorers only."""

# ruff: noqa: F811

from __future__ import annotations

from collections import Counter

import pytest
from test_rescore import (
    NOW,
    QUOTE,
    SPEC,
    FakeScorer,
    now,
    queue,
    queue_raw,
    row,
    score_all_once,
    scoring,
    second_profile,
)
from test_screen import (
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
)

from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore, screen
from jobhunter.scoring.scorers import ScoreResult


class CountingScorer(FakeScorer):
    """Answers unusably for the ids in ``bad`` (still at a cost) and counts every send."""

    def __init__(self, bad=(), cost: float = 0.0, on_submit=None) -> None:
        super().__init__(cost=cost)
        self.bad = set(bad)
        self.sends: Counter[str] = Counter()
        self.on_submit = on_submit

    def submit(self, requests):
        if self.on_submit is not None:
            self.on_submit(self)
        self.calls += 1
        out = []
        for r in requests:
            self.sends[r.custom_id] += 1
            text = "not json" if r.custom_id in self.bad else screen_json([QUOTE])
            out.append(ScoreResult(custom_id=r.custom_id, status="succeeded", text=text))
        return out


def run(conn, rid, profile, scorer, **kw):
    return rescore.run_request(
        conn, rid, profile, scoring(), now=now, scorer_factory=lambda s: scorer, **kw
    )


def test_failing_groups_are_sent_once_and_the_whole_plan_is_attempted(conn, profile):
    ids = [add_group(conn, n, profile) for n in range(1, 11)]
    first = [r["group_id"] for r in screen.eligible_groups(conn, profile, scorer=SPEC, limit=2)]
    scorer = CountingScorer(bad={f"g{g}" for g in first}, cost=0.002)
    rid = queue(conn, profile)
    st = run(conn, rid, profile, scorer, chunk=3)
    # Every planned group was sent, and none twice: the two that fail are not re-sent (and
    # paid for again) at the head of every chunk.
    assert set(scorer.sends) == {f"g{g}" for g in ids}
    assert max(scorer.sends.values()) == 1
    r = row(conn, rid)
    assert (r["status"], r["scored"], r["errored"], r["total"]) == ("done", 8, 2, 10)
    assert st.cost_usd == pytest.approx(0.02)
    assert "2 errored or came back unusable" in r["note"] and "not sent" not in r["note"]
    # They stay waiting for a later run.
    waiting = screen.eligible_groups(conn, profile, scorer=SPEC, limit=10)
    assert sorted(g["group_id"] for g in waiting) == sorted(first)


def test_a_stopped_run_says_how_much_of_the_plan_was_not_sent(conn, profile):
    for n in range(1, 7):
        add_group(conn, n, profile)
    scorer = CountingScorer(bad={f"g{n}" for n in range(1, 7)})  # everything unusable
    rid = queue(conn, profile)
    run(conn, rid, profile, scorer, chunk=2)
    r = row(conn, rid)
    assert r["status"] == "failed" and sum(scorer.sends.values()) == 2
    assert "4 of 6 planned groups were not sent" in r["note"]


def test_recent_scope_is_only_recent_open_a_to_e_groups(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    conn.execute("UPDATE job SET first_seen_at = ?", (to_iso(NOW),))
    # Group 2 is recent but a Mismatch (G); it sits between the two recent A-E groups.
    conn.execute(
        "UPDATE fit_score SET dimensions = ? WHERE job_group_id = 2",
        ('{"skills": {"score": 5}, "seniority": {"score": 50}, "domain": {"score": 5}}',),
    )
    p2 = second_profile(profile)
    _, recent = rescore.open_scored(conn, p2, NOW)
    assert recent == {1, 3}
    plan = rescore.make_plan(conn, p2, scoring(), "recent", SPEC, NOW)
    assert plan.total == 2 and plan.waiting == 3
    rid = rescore.create_request(conn, p2, plan, NOW)
    scorer = CountingScorer()
    run(conn, rid, p2, scorer)
    assert set(scorer.sends) == {"g1", "g3"}


def test_cost_per_job_is_per_job_for_packed_and_jev_requests(conn, profile):
    """llm_spend counts one call per request; a Jev request carries about 8 jobs."""
    spec = "jev:typesafe/jev-1.13"
    for n in range(1, 13):
        add_group(conn, n, profile)
    jev = CountingScorer(cost=0.001)
    jev.name = spec
    assert screen.score_sync(conn, jev, profile, limit=8, now=NOW).written == 8
    conn.execute("UPDATE llm_spend SET calls = 1 WHERE model = ?", (spec,))  # one packed request
    assert rescore.cost_per_job(conn, spec) == (pytest.approx(0.001), "fit_score")
    # The fake wrote screen-prompt rows, so all 12 wait under the decisions prompt: the
    # estimate is 12 jobs at the per-job cost, not 12 requests' worth.
    plan = rescore.make_plan(conn, profile, scoring(), "all", spec, NOW)
    assert plan.total == 12 and plan.estimated_usd == pytest.approx(0.012)


def test_the_confirmed_plan_bounds_the_run(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    plan = rescore.make_plan(conn, profile, scoring(), "all", SPEC, NOW)
    rid = rescore.create_request(conn, profile, plan, NOW)
    for n in range(4, 9):
        add_group(conn, n, profile)  # ingested after the user confirmed 3 groups
    scorer = CountingScorer()
    st = run(conn, rid, profile, scorer)
    assert set(scorer.sends) == {f"g{g}" for g in plan.group_ids} and st.scored == 3
    assert row(conn, rid)["total"] == 3


def test_a_confirmed_plan_runs_only_with_its_own_scorer(conn, profile):
    add_group(conn, 1, profile)
    rid = queue(conn, profile)
    with pytest.raises(rescore.RescoreRefused, match="confirmed for test:model"):
        rescore.run_request(
            conn, rid, profile, scoring(), now=now, scorer="anthropic:claude-haiku-4-5"
        )
    assert row(conn, rid)["status"] == "pending"


def test_the_run_stops_before_passing_the_confirmed_spend(conn, profile):
    for n in range(1, 11):
        add_group(conn, n, profile)
    plan = rescore.make_plan(conn, profile, scoring(), "all", SPEC, NOW)
    assert plan.estimated_usd == pytest.approx(0.02) and plan.max_usd == pytest.approx(0.04)
    rid = rescore.create_request(conn, profile, plan, NOW)
    scorer = CountingScorer(cost=0.01)  # five times the estimate
    st = run(conn, rid, profile, scorer, chunk=2)
    assert st.cost_usd <= plan.max_usd + 1e-9 and st.scored == 4
    r = row(conn, rid)
    assert "stopped at the confirmed spend of $0.04" in r["note"]
    assert "6 of 10 planned groups were not sent" in r["note"]


def test_a_finished_run_supersedes_the_queued_requests_it_covers(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    queued = queue_raw(conn, profile)  # a /prefs save: scope all, no scorer, no plan
    rid = queue(conn, profile)  # Re-score now, confirmed
    run(conn, rid, profile, FakeScorer())
    q = row(conn, queued)
    assert q["status"] == "canceled" and q["note"] == f"superseded by re-score #{rid}"
    # The nightly drain then has nothing to run, so no second scorer pays for the same groups.
    other = CountingScorer()
    nightly = scoring(screen_scorer="anthropic:claude-haiku-4-5")
    out = rescore.drain_pending(conn, profile, nightly, now=now, scorer_factory=lambda s: other)
    assert out == [] and other.calls == 0


def test_a_recent_run_does_not_supersede_a_queued_all(conn, profile):
    add_group(conn, 1, profile)
    queued = queue_raw(conn, profile)  # scope all
    rid = queue(conn, profile, "recent")
    run(conn, rid, profile, FakeScorer())
    assert row(conn, queued)["status"] == "pending"


def test_a_run_marked_interrupted_stops_and_stays_failed(conn, profile):
    for n in range(1, 5):
        add_group(conn, n, profile)
    rid = queue(conn, profile)

    def mark_stale(scorer):  # what running_request in another process does to a stale row
        conn.execute(
            "UPDATE rescore_request SET status = 'failed', error = 'interrupted' WHERE id = ?",
            (rid,),
        )

    scorer = CountingScorer(on_submit=mark_stale)
    run(conn, rid, profile, scorer, chunk=2)
    assert scorer.calls == 1  # the second chunk is never sent
    r = row(conn, rid)
    assert r["status"] == "failed" and r["scored"] == 2  # not flipped back to 'done'


def writes(conn, fn):
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    verbs = ("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE")
    return [s for s in seen if s.lstrip().upper().startswith(verbs)]


def test_planning_writes_nothing_when_nothing_needs_prefiltering(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    plan = writes(conn, lambda: rescore.make_plan(conn, profile, scoring(), "all", SPEC, NOW))
    assert plan == []
    assert writes(conn, lambda: rescore.running_request(conn, NOW)) == []


def test_reprefilter_commits_in_short_batches(conn, profile, monkeypatch):
    monkeypatch.setattr(rescore, "PREFILTER_BATCH", 2, raising=False)
    for n in range(1, 6):
        add_group(conn, n, profile, passed=None)
    conn.execute("UPDATE job SET stage = 'grouped'")
    stmts = writes(conn, lambda: rescore.ensure_prefilter(conn, profile, NOW))
    assert len([s for s in stmts if s.upper().startswith("BEGIN")]) == 3  # 2 + 2 + 1
