"""Re-score service: scope, status transitions, failure, cost cap, single-flight. Fake scorers."""

# ruff: noqa: F811

from __future__ import annotations

from datetime import timedelta

import pytest
from test_screen import (
    NOW,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
)

from jobhunter.config import Scoring
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import rescore, screen
from jobhunter.scoring.profile import Narrative as ProfileNarrative
from jobhunter.scoring.profile import SalaryFloor
from jobhunter.scoring.scorers import ScoreResult

SPEC = "test:model"
QUOTE = "Write Terraform modules and Go tooling."


class FakeScorer:
    name = SPEC

    def __init__(self, cost: float = 0.0, conn=None, fail: Exception | None = None) -> None:
        self.cost_each = cost
        self.conn = conn
        self.fail = fail
        self.calls = 0
        self.statuses: list[str] = []

    def submit(self, requests):
        self.calls += 1
        if self.conn is not None:
            self.statuses.append(
                self.conn.execute("SELECT status FROM rescore_request").fetchone()[0]
            )
        if self.fail:
            raise self.fail
        return [
            ScoreResult(custom_id=r.custom_id, status="succeeded", text=screen_json([QUOTE]))
            for r in requests
        ]

    def cost(self, usage, batch):
        return self.cost_each


def now():
    return NOW


def scoring(**kw) -> Scoring:
    return Scoring(**kw)


def second_profile(profile):
    """Both versions change: a new salary floor (filter_version) and a new narrative."""
    return profile.model_copy(
        update={
            "hard": profile.hard.model_copy(
                update={"salary_floor": SalaryFloor(amount=1, period="year")}
            ),
            "narrative": ProfileNarrative(want="something new"),
        }
    )


def queue(conn, profile, scope="all", scorer=None) -> int:
    plan = rescore.make_plan(conn, profile, scoring(), scope, scorer or SPEC, NOW)
    rid = rescore.create_request(conn, profile, plan, NOW)
    return rid


def queue_raw(conn, profile) -> int:
    cur = conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd) VALUES (?, 'all', ?, 0, 0, 0)",
        (to_iso(NOW), profile.scoring_version),
    )
    return int(cur.lastrowid)


def score_all_once(conn, profile):
    return screen.score_sync(conn, FakeScorer(), profile, limit=50, now=NOW)


def row(conn, rid):
    return rescore.get_request(conn, rid)


def test_changed_filter_and_scoring_version_reprefilters_then_scores(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    assert score_all_once(conn, profile).written == 3
    p2 = second_profile(profile)
    assert p2.filter_version != profile.filter_version
    assert p2.scoring_version != profile.scoring_version
    # The bug: eligibility joins prefilter_result on the new filter_version, so nothing is eligible.
    assert screen.eligible_groups(conn, p2, scorer=SPEC, limit=10) == []
    rid = queue_raw(conn, p2)  # no plan yet: the run itself must re-prefilter
    scorer = FakeScorer()
    st = rescore.run_request(
        conn, rid, p2, scoring(), now=now, scorer_factory=lambda s: scorer, scorer=SPEC
    )
    assert st.scored == 3
    r = row(conn, rid)
    assert r["status"] == "done" and r["scored"] == 3 and r["done_at"]
    assert "re-prefiltered 3 jobs (3 passed)" in r["note"]
    n = conn.execute(
        "SELECT count(*) FROM fit_score WHERE scoring_version = ?", (p2.scoring_version,)
    ).fetchone()[0]
    assert n == 3


def test_scope_all_vs_recent(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    # groups 1 and 2 were seen now; 3 is old; 4 is new and never scored
    conn.execute("UPDATE job SET first_seen_at = ? WHERE id IN (1, 2)", (to_iso(NOW),))
    conn.execute("UPDATE job SET first_seen_at = '2020-01-01T00:00:00Z' WHERE id = 3")
    add_group(conn, 4, profile)
    p2 = second_profile(profile)
    all_plan = rescore.make_plan(conn, p2, scoring(), "all", SPEC, NOW)
    recent = rescore.make_plan(conn, p2, scoring(), "recent", SPEC, NOW)
    assert all_plan.total == 4 and all_plan.waiting == 4 and all_plan.rescored == 3
    assert recent.total == 2
    assert recent.estimated_usd == pytest.approx(2 * screen.ESTIMATED_COST_PER_REQUEST_USD)
    rid = queue(conn, p2, "recent")
    st = rescore.run_request(
        conn, rid, p2, scoring(), now=now, scorer_factory=lambda s: FakeScorer(), scorer=SPEC
    )
    assert st.scored == 2
    assert len(screen.eligible_groups(conn, p2, scorer=SPEC, limit=10)) == 2


def test_status_transitions_and_cost(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    rid = queue(conn, profile)
    assert row(conn, rid)["status"] == "pending"
    scorer = FakeScorer(cost=0.01, conn=conn)
    rescore.run_request(
        conn,
        rid,
        profile,
        scoring(),
        now=now,
        scorer_factory=lambda s: scorer,
        scorer=SPEC,
        chunk=2,
    )
    assert scorer.statuses == ["running", "running"]  # two chunks of 2 and 1
    r = row(conn, rid)
    assert (r["status"], r["scored"], r["total"], r["scorer"]) == ("done", 3, 3, SPEC)
    assert r["cost_usd"] == pytest.approx(0.03)


def test_failure_is_recorded(conn, profile):
    add_group(conn, 1, profile)
    rid = queue(conn, profile)
    scorer = FakeScorer(fail=RuntimeError("provider down"))
    st = rescore.run_request(
        conn, rid, profile, scoring(), now=now, scorer_factory=lambda s: scorer, scorer=SPEC
    )
    r = row(conn, rid)
    assert r["status"] == "failed" and "provider down" in r["error"] and r["done_at"]
    assert st.scored == 0


def test_scorer_construction_failure_is_recorded(conn, profile):
    add_group(conn, 1, profile)
    rid = queue(conn, profile)

    def boom(spec):
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    rescore.run_request(conn, rid, profile, scoring(), now=now, scorer_factory=boom, scorer=SPEC)
    assert row(conn, rid)["status"] == "failed"
    assert "OPENROUTER_API_KEY" in row(conn, rid)["error"]


def test_cost_cap_refuses_and_leaves_pending(conn, profile):
    for n in range(1, 4):
        add_group(conn, n, profile)
    rid = queue(conn, profile)
    tight = scoring(daily_cap_usd=0.001)
    with pytest.raises(rescore.RescoreRefused, match="spend cap"):
        rescore.run_request(
            conn, rid, profile, tight, now=now, scorer_factory=lambda s: FakeScorer(), scorer=SPEC
        )
    assert row(conn, rid)["status"] == "pending"
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 0


def test_single_flight(conn, profile):
    add_group(conn, 1, profile)
    first = queue(conn, profile)
    second = queue(conn, profile)
    conn.execute(
        "UPDATE rescore_request SET status = 'running', started_at = ?, heartbeat_at = ? "
        "WHERE id = ?",
        (to_iso(NOW), to_iso(NOW), first),
    )
    with pytest.raises(rescore.RescoreBusy) as exc:
        rescore.run_request(
            conn, second, profile, scoring(), now=now, scorer_factory=lambda s: FakeScorer(),
            scorer=SPEC,
        )  # fmt: skip
    assert exc.value.request_id == first
    assert row(conn, second)["status"] == "pending"


def test_stale_running_row_is_failed_and_does_not_block(conn, profile):
    add_group(conn, 1, profile)
    dead = queue(conn, profile)
    live = queue(conn, profile)
    old = to_iso(NOW - timedelta(hours=1))
    conn.execute(
        "UPDATE rescore_request SET status = 'running', started_at = ?, heartbeat_at = ? "
        "WHERE id = ?",
        (old, old, dead),
    )
    st = rescore.run_request(
        conn, live, profile, scoring(), now=now, scorer_factory=lambda s: FakeScorer(), scorer=SPEC
    )
    assert st.scored == 1
    assert row(conn, dead)["status"] == "failed" and "interrupted" in row(conn, dead)["error"]


def test_anthropic_scorer_submits_a_batch(conn, profile, monkeypatch):
    for n in range(1, 3):
        add_group(conn, n, profile)
    spec = "anthropic:claude-haiku-4-5"
    rid = queue(conn, profile, scorer=spec)
    seen = {}

    def fake_submit(c, client, prof, *, limit, now, scorer, remaining_usd):
        seen.update(limit=limit, scorer=scorer, client=client)
        return "msgbatch_x"

    monkeypatch.setattr(screen, "submit_batch", fake_submit)
    rescore.run_request(
        conn, rid, profile, scoring(), now=now, client_factory=lambda: "fake-client", scorer=spec
    )
    r = row(conn, rid)
    assert r["status"] == "done" and "msgbatch_x" in r["note"] and "--collect-pending" in r["note"]
    assert seen == {"limit": 2, "scorer": spec, "client": "fake-client"}


def test_drain_pending_runs_in_order_and_skips_refused(conn, profile):
    add_group(conn, 1, profile)
    a = queue(conn, profile)
    b = queue(conn, profile)
    out = rescore.drain_pending(
        conn, profile, scoring(), now=now, scorer_factory=lambda s: FakeScorer(), scorer=SPEC
    )
    assert [rid for rid, _ in out] == [a, b]
    assert row(conn, a)["status"] == "done" and row(conn, b)["status"] == "done"
    assert row(conn, b)["scored"] == 0 and "nothing waiting" in row(conn, b)["note"]


def test_cancel_only_pending(conn, profile):
    add_group(conn, 1, profile)
    rid = queue(conn, profile)
    assert rescore.cancel_request(conn, rid, NOW) is True
    assert rescore.cancel_request(conn, rid, NOW) is False
