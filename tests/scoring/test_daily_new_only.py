"""The daily run scores new jobs only: re-scoring stored jobs after a paid change (scoring_version,
model, prompt) never happens automatically (specs/006, specs/014). Fake scorers; no network."""

# ruff: noqa: F811

from __future__ import annotations

import sys
import types
from datetime import timedelta

import pytest
from test_rescore import SPEC, FakeScorer, queue_raw, score_all_once
from test_screen import (
    NOW,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
)

from jobhunter.config import Scoring, Settings
from jobhunter.pipeline import runner
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import decisions, rescore, scorers, screen
from jobhunter.scoring.profile import Narrative


def new_narrative(profile):
    """Only the narrative changes: a new scoring_version, the same filter_version."""
    p2 = profile.model_copy(update={"narrative": Narrative(want="something else entirely")})
    assert p2.scoring_version != profile.scoring_version
    assert p2.filter_version == profile.filter_version
    return p2


def daily(conn, profile, monkeypatch, spec=SPEC):
    """``run``'s score stage with a fake sync scorer; returns (scorer, report)."""
    scorer = FakeScorer()
    scorer.name = spec
    monkeypatch.setattr(scorers, "scorer_from_string", lambda s, scoring=None: scorer)
    report = runner.RunReport()
    settings = Settings(scoring=Scoring(screen_scorer=spec))
    runner._score_stage(conn, settings, profile, NOW, report)
    return scorer, report


def rows_for(conn, version):
    return conn.execute(
        "SELECT job_group_id FROM fit_score WHERE scoring_version = ? ORDER BY job_group_id",
        (version,),
    ).fetchall()


def test_daily_run_does_not_rescore_after_a_scoring_version_change(conn, profile, monkeypatch):
    for n in range(1, 4):
        add_group(conn, n, profile)
    assert score_all_once(conn, profile).written == 3
    p2 = new_narrative(profile)
    scorer, report = daily(conn, p2, monkeypatch)
    assert rows_for(conn, p2.scoring_version) == []
    assert scorer.calls == 0
    assert report.counts.get("score") == "0"
    # The re-score is still there for the user to confirm explicitly.
    assert len(screen.eligible_groups(conn, p2, scorer=SPEC, limit=10)) == 3


def test_daily_run_still_scores_new_jobs(conn, profile, monkeypatch):
    for n in range(1, 3):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    p2 = new_narrative(profile)
    new = add_group(conn, 3, p2)
    _, report = daily(conn, p2, monkeypatch)
    assert [r[0] for r in rows_for(conn, p2.scoring_version)] == [new]
    assert report.counts["score"] == "1"


def test_daily_run_does_not_rescore_with_a_different_model(conn, profile, monkeypatch):
    for n in range(1, 3):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    scorer, _ = daily(conn, profile, monkeypatch, spec="test:other-model")
    assert scorer.calls == 0
    n = conn.execute("SELECT count(*) FROM fit_score WHERE model = 'test:other-model'").fetchone()
    assert n[0] == 0


def test_daily_run_scores_a_pasted_description_again(conn, profile, monkeypatch):
    g1 = add_group(conn, 1, profile)
    add_group(conn, 2, profile)
    score_all_once(conn, profile)
    conn.execute("UPDATE job_group SET description_rev = description_rev + 1 WHERE id = ?", (g1,))
    _, report = daily(conn, profile, monkeypatch)
    assert report.counts["score"] == "1"
    revs = conn.execute(
        "SELECT input_rev FROM fit_score WHERE job_group_id = ? ORDER BY input_rev", (g1,)
    ).fetchall()
    assert [r[0] for r in revs] == [0, 1]


def test_daily_batch_route_submits_new_jobs_only(conn, profile, monkeypatch):
    for n in range(1, 4):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    p2 = new_narrative(profile)
    new = add_group(conn, 4, p2)
    sent: list[list[str]] = []

    class FakeBatch:
        def __init__(self, client, model):
            pass

        def submit(self, requests):
            sent.append([r.custom_id for r in requests])
            return "batch_fake"

    monkeypatch.setattr(screen, "AnthropicScorer", FakeBatch)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: None))
    report = runner.RunReport()
    runner._score_stage(conn, Settings(), p2, NOW, report)
    assert sent == [[f"g{new}"]]
    assert report.counts["score"] == "batch_fake"


def test_new_only_skips_groups_in_another_models_uncollected_batch(conn, profile):
    g = add_group(conn, 1, profile)
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at) VALUES ('b1', 'screen', 'test:other', 'p', 'v', 1, 'x')"
    )
    conn.execute(
        "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id, input_rev) "
        "VALUES ('b1', ?, ?, 0)",
        (f"g{g}", g),
    )
    assert screen.eligible_groups(conn, profile, scorer=SPEC, limit=5, new_only=True) == []
    assert len(screen.eligible_groups(conn, profile, scorer=SPEC, limit=5)) == 1


@pytest.mark.parametrize("new_only, expected", [(True, 0), (False, 2)])
def test_decisions_eligibility_honours_new_only(conn, profile, new_only, expected):
    for n in range(1, 3):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    got = decisions.eligible(conn, profile, "jev:typesafe/jev-1.13", 10, new_only=new_only)
    assert len(got) == expected


# ─── Groups a re-score plan covers, and the weekly cap (23142c9) ───────────

OTHER = "test:plan-model"  # the scorer the user confirmed the re-score for


def confirmed_plan(conn, profile, status="pending", heartbeat=NOW):
    """A confirmed "all" re-score for ``OTHER`` covering every waiting group."""
    plan = rescore.make_plan(conn, profile, Scoring(), "all", OTHER, NOW)
    rid = rescore.create_request(conn, profile, plan, NOW)
    if status == "running":
        conn.execute(
            "UPDATE rescore_request SET status = 'running', started_at = ?, heartbeat_at = ? "
            "WHERE id = ?",
            (to_iso(heartbeat), to_iso(heartbeat), rid),
        )
    return rid, plan


@pytest.mark.parametrize("status", ["pending", "running"])
def test_daily_run_leaves_groups_in_a_confirmed_plan_to_that_plan(
    conn, profile, monkeypatch, status
):
    for n in range(1, 3):
        add_group(conn, n, profile)
    score_all_once(conn, profile)
    p2 = new_narrative(profile)
    new = add_group(conn, 3, p2)  # arrived after the paid change, before the plan was made
    _, plan = confirmed_plan(conn, p2, status=status)
    assert new in plan.group_ids
    later = add_group(conn, 4, p2)  # arrived after the plan: the daily run's job
    scorer, report = daily(conn, p2, monkeypatch)
    assert [r[0] for r in rows_for(conn, p2.scoring_version)] == [later]
    assert report.counts["score"] == "1"
    assert scorer.calls == 1


def test_plan_group_is_paid_once_when_the_daily_run_comes_first(conn, profile, monkeypatch):
    """Without the exclusion the daily run paid the default scorer for a planned group, and the
    plan (eligibility is per scorer) then paid OTHER for it again."""
    p2 = new_narrative(profile)
    new = add_group(conn, 1, p2)
    rid, _ = confirmed_plan(conn, p2)
    daily(conn, p2, monkeypatch)
    plan_scorer = FakeScorer()
    plan_scorer.name = OTHER
    rescore.run_request(
        conn, rid, p2, Scoring(), now=lambda: NOW, scorer_factory=lambda s: plan_scorer
    )
    models = conn.execute(
        "SELECT model FROM fit_score WHERE job_group_id = ? AND scoring_version = ?",
        (new, p2.scoring_version),
    ).fetchall()
    assert [m[0] for m in models] == [OTHER]


def test_queued_request_without_a_plan_does_not_hold_new_jobs(conn, profile, monkeypatch):
    p2 = new_narrative(profile)
    new = add_group(conn, 1, p2)
    queue_raw(conn, p2)  # a /prefs save: no group list until something plans it
    daily(conn, p2, monkeypatch)
    assert [r[0] for r in rows_for(conn, p2.scoring_version)] == [new]


@pytest.mark.parametrize("status", ["done", "failed", "canceled"])
def test_finished_plans_do_not_hold_groups(conn, profile, monkeypatch, status):
    p2 = new_narrative(profile)
    new = add_group(conn, 1, p2)
    rid, _ = confirmed_plan(conn, p2)
    conn.execute("UPDATE rescore_request SET status = ? WHERE id = ?", (status, rid))
    daily(conn, p2, monkeypatch)
    assert [r[0] for r in rows_for(conn, p2.scoring_version)] == [new]


def test_dead_running_plan_is_failed_and_releases_its_groups(conn, profile, monkeypatch):
    p2 = new_narrative(profile)
    new = add_group(conn, 1, p2)
    rid, _ = confirmed_plan(conn, p2, status="running", heartbeat=NOW - timedelta(hours=1))
    daily(conn, p2, monkeypatch)
    assert rescore.get_request(conn, rid)["status"] == "failed"
    assert [r[0] for r in rows_for(conn, p2.scoring_version)] == [new]


def spent_this_week(conn, usd, days_ago=3):
    day = (NOW - timedelta(days=days_ago)).date().isoformat()
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) VALUES (?, 'm', 'screen', 1, ?)",
        (day, usd),
    )


def test_daily_run_respects_the_weekly_cap(conn, profile, monkeypatch):
    add_group(conn, 1, profile)
    spent_this_week(conn, 12.0)  # weekly cap 10; nothing spent today, daily cap untouched
    scorer, report = daily_with_caps(conn, profile, monkeypatch, daily=2.0, weekly=10.0)
    assert scorer.calls == 0
    assert report.counts["score"] == "0"
    assert any("spend cap reached" in m for m in report.messages)
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 0


def test_daily_batch_route_respects_the_weekly_cap(conn, profile, monkeypatch):
    add_group(conn, 1, profile)
    spent_this_week(conn, 12.0)
    submitted = []
    monkeypatch.setattr(
        screen,
        "AnthropicScorer",
        lambda c, m: types.SimpleNamespace(submit=lambda r: submitted.append(r) or "b1"),
    )
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: None))
    report = runner.RunReport()
    runner._score_stage(conn, Settings(scoring=Scoring(weekly_cap_usd=10.0)), profile, NOW, report)
    assert submitted == []


def test_weekly_cap_limits_the_daily_run_to_what_is_left(conn, profile, monkeypatch):
    for n in range(1, 6):
        add_group(conn, n, profile)
    # $0.005 left this week covers two jobs at the planning estimate of $0.002 each.
    spent_this_week(conn, 10.0 - 0.005)
    _, report = daily_with_caps(conn, profile, monkeypatch, daily=2.0, weekly=10.0)
    assert report.counts["score"] == "2"


def daily_with_caps(conn, profile, monkeypatch, *, daily, weekly):
    scorer = FakeScorer()
    monkeypatch.setattr(scorers, "scorer_from_string", lambda s, scoring=None: scorer)
    report = runner.RunReport()
    settings = Settings(
        scoring=Scoring(screen_scorer=SPEC, daily_cap_usd=daily, weekly_cap_usd=weekly)
    )
    runner._score_stage(conn, settings, profile, NOW, report)
    return scorer, report


def test_remaining_budget_is_the_tighter_cap_and_counts_uncollected_batches(conn, profile):
    spent_this_week(conn, 9.0)
    s = Scoring(daily_cap_usd=2.0, weekly_cap_usd=10.0)
    assert screen.remaining_budget(conn, s, NOW) == pytest.approx(1.0)
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at) VALUES ('b1', 'screen', 'm', 'p', 'v', 100, 'x')"
    )
    pending = 100 * screen.ESTIMATED_COST_PER_REQUEST_USD
    assert screen.remaining_budget(conn, s, NOW) == pytest.approx(1.0 - pending)
    # Spend older than seven days no longer counts against the week.
    assert screen.weekly_remaining(conn, 10.0, NOW + timedelta(days=7)) == pytest.approx(
        10.0 - pending
    )
