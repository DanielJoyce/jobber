"""020 M5b (git-bug a322711): the per-model spend reservation is switched on.

A screening model reserves its own per-job price on every path (batch submit and collect, sync
screen, re-score plans, Score now), estimates never undercount what the cap holds, admission
stops exactly at the cap, in-flight batches keep and then release their reservation across the
switch, and the deep pass is unaffected. Fake clients and MockTransport only: no network, no
paid API.
"""

# ruff: noqa: F811

from __future__ import annotations

import math

import pytest
from test_screen import (
    NOW,
    FakeClient,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
    succeeded,
)
from test_sync_reservation import CHEAP, PRICEY, PRICEY_PER_JOB, ChatAPI, router

from jobhunter.apply import score as group_score
from jobhunter.config import Scoring
from jobhunter.scoring import catalog, deep, deep_run, rescore, screen

HAIKU55 = "anthropic:claude-haiku-5-5"
SONNET = "anthropic:claude-sonnet-5-5"
OPUS55 = "anthropic:claude-opus-5-5"
QUOTE = "Write Terraform modules and Go tooling."
# Table prices at screen.TYPICAL_USAGE (2,600 in, 3,900 cache reads, 455 out), worked by hand:
# Haiku 5.5 $0.10/$0.50, cache reads 0.1x: 260 + 19.5 + 227.5 = 507 micro-USD unbatched.
# Sonnet 5.5 $2/$10, cache reads 0.05x: 5,200 + 390 + 4,550 = 10,140 micro-USD unbatched.
# Opus 5.5 $4/$20, cache reads 0.05x: 10,400 + 780 + 9,100 = 20,280 micro-USD unbatched.
# Batch prices are half. The floor is the flat $0.002.
RESERVE = {
    HAIKU55: (0.002, 0.002),  # (batch, sync): both under the floor
    SONNET: (0.00507, 0.01014),
    OPUS55: (0.01014, 0.02028),
}


def add_groups(conn, profile, n: int, start: int = 1) -> list[int]:
    return [add_group(conn, i, profile) for i in range(start, start + n)]


def add_measured(conn, profile, model: str, cost: float, n: int = 3) -> None:
    for i in range(n):
        gid = add_group(conn, 20_000 + i, profile, passed=False)
        conn.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, cost_usd, created_at) "
            "VALUES (?, 'screen', ?, 'p', 'old', 'weak', 10, '{}', '[]', ?, 'x')",
            (gid, model, cost),
        )


def sent(client: FakeClient) -> int:
    return sum(len(b) for b in client.messages.batches.created)


def scoring_with(daily: float, model: str = HAIKU55) -> Scoring:
    return Scoring(screen_scorer=model, daily_cap_usd=daily, weekly_cap_usd=1000.0)


# ─── Haiku vs Sonnet vs Opus ────────────────────────────────────────────────


@pytest.mark.parametrize("model", list(RESERVE))
def test_each_model_reserves_its_own_price(conn, model):
    batch, sync = RESERVE[model]
    assert screen.reserve_per_request_usd(conn, model) == pytest.approx(batch)
    assert screen.reserve_per_job_usd(conn, model, batch=True) == pytest.approx(batch)
    assert screen.reserve_per_job_usd(conn, model, batch=False) == pytest.approx(sync)
    # An Anthropic re-score or Score now goes through the batch: its reservation is the batch's.
    assert rescore.reserve_per_job(conn, model) == pytest.approx(batch)


def test_which_screening_models_are_offered_now():
    assert catalog.judge("screen", HAIKU55).offered
    assert catalog.judge("screen", SONNET).offered  # within its own reservation now
    opus = catalog.judge("screen", OPUS55)
    assert not opus.offered and "thinking" in opus.reason  # never: thinking cannot be off
    assert SONNET in [o.value for o in catalog.options("screen").options]


# ─── Estimates match what is reserved ───────────────────────────────────────


def test_without_history_the_plan_price_is_the_reservation(conn):
    for model in (HAIKU55, SONNET):
        per_job, source = rescore.cost_per_job(conn, model)
        assert source == "default"
        assert per_job == pytest.approx(rescore.reserve_per_job(conn, model))
        assert per_job == pytest.approx(screen.reserve_per_request_usd(conn, model))


def test_a_measured_price_below_the_reservation_is_shown_with_it(conn, profile):
    add_measured(conn, profile, SONNET, 0.003)  # below the 0.00507 table price
    add_groups(conn, profile, 10)
    plan = rescore.make_plan(conn, profile, scoring_with(100.0, SONNET), "all", SONNET, NOW)
    assert plan.cost_per_job == pytest.approx(0.003) and plan.cost_source == "fit_score"
    assert plan.reserve_per_job == pytest.approx(0.00507)
    assert plan.reserve_differs and plan.reserved_usd == pytest.approx(10 * 0.00507)
    assert plan.refusal is None and plan.fits_now == 10


def test_a_jev_plan_reserves_its_own_estimate(conn, profile):
    """Decisions models check the cap per request on their own estimate (unchanged)."""
    spec = "jev:typesafe/jev-1.13"
    assert rescore.reserve_per_job(conn, spec) == rescore.DECISIONS_FALLBACK_COST


def test_a_packed_openrouter_plan_keeps_its_own_estimate(conn):
    """Packed requests check the cap per wave; the plan does not invent a per-job hold."""
    from jobhunter.config import OpenRouter

    packed = Scoring(openrouter=OpenRouter(jobs_per_request=8))
    per_job, _ = rescore.cost_per_job(conn, f"openrouter:{CHEAP}", packed)
    assert rescore.reserve_per_job(conn, f"openrouter:{CHEAP}", packed) == per_job


# ─── Cap admission at the boundary ──────────────────────────────────────────


@pytest.mark.parametrize("model", [HAIKU55, SONNET])
def test_batch_admission_stops_exactly_at_the_cap(conn, profile, model):
    add_groups(conn, profile, 20)
    per = screen.reserve_per_request_usd(conn, model)
    client = FakeClient()
    screen.submit_batch(
        conn, client, profile, limit=100, now=NOW, scorer=model, remaining_usd=lambda: 7 * per
    )
    assert sent(client) == 7  # exactly seven fit
    client = FakeClient()
    conn.execute("DELETE FROM score_batch_item")
    conn.execute("DELETE FROM score_batch")
    screen.submit_batch(
        conn,
        client,
        profile,
        limit=100,
        now=NOW,
        scorer=model,
        remaining_usd=lambda: 7 * per - 1e-9,
    )
    assert sent(client) == 6  # a hair under seven: six


def test_a_sonnet_batch_after_the_switch_never_reserves_past_the_cap(conn, profile):
    add_groups(conn, profile, 600)
    scoring = scoring_with(2.0, SONNET)
    client = FakeClient()

    def remaining() -> float:
        return screen.remaining_budget(conn, scoring, NOW)

    screen.submit_batch(
        conn, client, profile, limit=1000, now=NOW, scorer=SONNET, remaining_usd=remaining
    )
    n = sent(client)
    assert n == math.floor(2.0 / 0.00507) == 394  # the flat $0.002 would have let 1000 go
    assert screen._pending_batch_usd(conn) <= 2.0
    assert remaining() >= 0


def test_sync_admission_stops_exactly_at_the_cap(conn, profile, tmp_path):
    add_groups(conn, profile, 20)
    api = ChatAPI(cost=PRICEY_PER_JOB)
    scorer = router(tmp_path, PRICEY, api)
    per = screen.reserve_per_job_usd(conn, scorer.name, screen.scoring_for(scorer))
    assert per == pytest.approx(PRICEY_PER_JOB)
    res = screen.score_sync(
        conn, scorer, profile, limit=100, now=NOW, remaining_usd=lambda: 3 * per
    )
    assert res.submitted == api.calls == 3
    assert res.cost_usd <= 3 * per + 1e-12
    res = screen.score_sync(
        conn, scorer, profile, limit=100, now=NOW, remaining_usd=lambda: per - 1e-9
    )
    assert res.submitted == 0 and api.calls == 3  # less than one job's reservation: nothing


def test_plan_fits_now_at_the_boundary(conn, profile):
    add_groups(conn, profile, 10)
    add_measured(conn, profile, HAIKU55, 0.0001)  # the estimate; the cap holds $0.002
    # The estimate ($0.001 for 10) fits a $0.01 cap, but only 5 jobs fit the $0.002 hold.
    plan = rescore.make_plan(conn, profile, scoring_with(0.01), "all", HAIKU55, NOW)
    assert plan.estimated_usd == pytest.approx(0.001)
    assert plan.refusal is None and plan.fits_now == 5
    # Less than one job's hold left: refused up front instead of sending nothing after Run.
    plan = rescore.make_plan(conn, profile, scoring_with(0.0019), "all", HAIKU55, NOW)
    assert plan.fits_now == 0
    assert plan.refusal and "holds $0.0020 a job" in plan.refusal
    # Exactly one job's hold: one fits, not refused.
    plan = rescore.make_plan(conn, profile, scoring_with(0.002), "all", HAIKU55, NOW)
    assert plan.refusal is None and plan.fits_now == 1


def test_score_now_is_refused_when_the_hold_does_not_fit(conn, profile):
    gid = add_group(conn, 1, profile)
    add_measured(conn, profile, HAIKU55, 0.0003)
    est = group_score.estimate(conn, profile, scoring_with(0.001), gid, NOW)
    assert est.estimated_usd == pytest.approx(0.0003)  # the estimate fits the $0.001 left
    assert est.reserve_usd == pytest.approx(0.002) and est.reserve_differs
    assert est.refusal and "holds $0.0020" in est.refusal
    est = group_score.estimate(conn, profile, scoring_with(0.002), gid, NOW)
    assert est.refusal is None  # exactly the hold: admitted


# ─── The transition: batches in flight across the switch ────────────────────


def _submit(conn, profile, client, model, ids) -> str:
    batch_id = screen.submit_batch(
        conn, client, profile, limit=len(ids), now=NOW, scorer=model, group_ids=ids
    )
    assert batch_id
    return batch_id


def test_batches_in_flight_keep_and_then_release_their_reservation(conn, profile, monkeypatch):
    client = FakeClient()
    # Before the switch: a Haiku 5.5 batch (the flat rule, $0.002 each) and a Sonnet batch
    # that was saved with "save anyway" (``--force``) under the old rule.
    monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", False)
    assert not catalog.judge("screen", SONNET).offered
    haiku_ids = add_groups(conn, profile, 4)
    sonnet_ids = add_groups(conn, profile, 3, start=5)
    haiku_batch = _submit(conn, profile, client, HAIKU55, haiku_ids)
    _submit(conn, profile, client, SONNET, sonnet_ids)
    before = screen._pending_batch_usd(conn)
    assert before == pytest.approx(4 * 0.002 + 3 * 0.00507)

    # The switch: nothing in flight moves, because the hold was per model already.
    monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", True)
    assert catalog.judge("screen", SONNET).offered
    assert screen._pending_batch_usd(conn) == pytest.approx(before)

    # Collecting the Haiku batch releases exactly its hold and records what it really cost.
    client.messages.batches.results_by_id[haiku_batch] = [
        succeeded(f"g{gid}", screen_json([QUOTE])) for gid in haiku_ids
    ]
    result = screen.collect_batch(conn, client, haiku_batch, profile, now=NOW)
    assert result.written == 4
    assert screen._pending_batch_usd(conn) == pytest.approx(3 * 0.00507)
    spent = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM llm_spend").fetchone()[0]
    assert spent == pytest.approx(result.cost_usd) and spent > 0
    assert screen.remaining_daily_budget(conn, 1.0, NOW) == pytest.approx(1.0 - spent - 3 * 0.00507)
    # Collecting it again releases nothing more and records nothing twice.
    assert screen.collect_batch(conn, client, haiku_batch, profile, now=NOW).status == (
        "already_collected"
    )
    assert screen._pending_batch_usd(conn) == pytest.approx(3 * 0.00507)


def test_a_plan_confirmed_at_the_flat_price_runs_at_the_model_price(conn, profile):
    """A re-score confirmed before per-model pricing, at $0.002 a job for Sonnet, is cut to
    what its confirmed ceiling covers at Sonnet's price (rescore._priced_on_flat_default)."""
    add_groups(conn, profile, 40)
    plan = rescore.make_plan(conn, profile, scoring_with(100.0, SONNET), "all", SONNET, NOW)
    rid = rescore.create_request(conn, profile, plan, NOW)
    conn.execute(
        "UPDATE rescore_request SET cost_per_job_usd = 0.002, max_usd = 0.05 WHERE id = ?", (rid,)
    )
    client = FakeClient()
    rescore.run_request(
        conn,
        rid,
        profile,
        scoring_with(100.0, SONNET),
        now=lambda: NOW,
        client_factory=lambda: client,
        scorer=SONNET,
    )
    assert sent(client) == math.floor(0.05 / 0.00507) == 9
    stored = rescore.stored_plan(
        conn, rescore.get_request(conn, rid), profile, scoring_with(100.0, SONNET), NOW
    )
    assert stored.reserve_per_job == pytest.approx(0.00507)


# ─── The deep pass ──────────────────────────────────────────────────────────


def test_the_deep_pass_is_unaffected_by_the_switch(conn, profile, monkeypatch):
    scoring = Scoring(daily_cap_usd=5.0, weekly_cap_usd=50.0)
    plans = []
    for on in (False, True):
        monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", on)
        plans.append(deep_run.make_plan(conn, profile, scoring, mode="next", n=3, now=NOW))
    off, on = plans
    assert (off.worst_usd, off.daily_left, off.weekly_left, off.refusal) == (
        on.worst_usd,
        on.daily_left,
        on.weekly_left,
        on.refusal,
    )
    assert on.worst_usd == deep.worst_case_usd(scoring.deep_scorer)
    assert catalog.judge("deep", SONNET).offered and catalog.judge("deep", OPUS55).offered


def test_a_sonnet_screen_batch_in_flight_holds_its_own_price_against_the_deep_pass(conn, profile):
    scoring = Scoring(daily_cap_usd=5.0, weekly_cap_usd=50.0)
    before = deep_run.make_plan(conn, profile, scoring, mode="next", n=3, now=NOW).daily_left
    client = FakeClient()
    _submit(conn, profile, client, SONNET, add_groups(conn, profile, 10))
    after = deep_run.make_plan(conn, profile, scoring, mode="next", n=3, now=NOW).daily_left
    assert before - after == pytest.approx(10 * 0.00507)
