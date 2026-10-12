"""020 M5b (git-bug a322711): the per-model spend reservation is switched on.

A screening model holds its own per-job price on every path (batch submit and collect, sync
screen, re-score plans, Score now). A model with no history is held with its cached prefix,
sized from the real rubric and profile, priced as cache writes (review B1). Plans and runs
use one boundary comparison, in-flight batches keep and then release their hold across the
switch, packed runs and pages agree, and the deep pass is unaffected. Fake clients and
MockTransport only: no network, no paid API.

Expected dollar figures are worked out here by hand (``cold_batch_usd``, ``cold_prefix`` in
test_screen), not by calling the pricing code under test.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import math

import pytest
from test_screen import (
    NOW,
    FakeClient,
    add_group,
    cold_batch_usd,
    cold_prefix,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
    succeeded,
)
from test_sync_reservation import PRICEY, PRICEY_PER_JOB, ChatAPI, router, write_catalog

from jobhunter import model_choices as mc
from jobhunter.apply import score as group_score
from jobhunter.config import OpenRouter, Scoring
from jobhunter.scoring import catalog, deep, deep_run, rescore, screen
from jobhunter.scoring.profile import scoring_inputs
from jobhunter.scoring.rubric import RUBRIC_TEXT, SCREEN_SCHEMA

HAIKU55 = "anthropic:claude-haiku-5-5"
SONNET = "anthropic:claude-sonnet-5-5"
OPUS55 = "anthropic:claude-opus-5-5"
QUOTE = "Write Terraform modules and Go tooling."
RATES = {HAIKU55: (0.10, 0.50), SONNET: (2.0, 10.0), OPUS55: (4.0, 20.0)}  # $ per Mtok
# Warm table prices at screen.TYPICAL_USAGE (2,600 in, 3,900 cache reads, 455 out), batched:
SONNET_WARM = (2600 * 2.0 + 3900 * 2.0 * 0.05 + 455 * 10.0) * 0.5 / 1e6  # 0.00507


def add_groups(conn, profile, n: int, start: int = 1) -> list[int]:
    return [add_group(conn, i, profile) for i in range(start, start + n)]


def add_measured(conn, profile, model: str, cost: float, n: int = 3, start: int = 20_000) -> None:
    for i in range(n):
        gid = add_group(conn, start + i, profile, passed=False)
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


def spent(conn) -> float:
    return float(conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM llm_spend").fetchone()[0])


# ─── Haiku vs Sonnet vs Opus ────────────────────────────────────────────────


@pytest.mark.parametrize("model", list(RATES))
def test_each_model_with_no_history_is_held_cold(conn, profile, model):
    cold = cold_batch_usd(*RATES[model], cold_prefix(profile))
    expected = max(0.002, cold)  # never below the flat floor
    assert screen.reserve_per_request_usd(conn, model, profile) == pytest.approx(expected)
    assert screen.reserve_per_job_usd(conn, model, batch=False, profile=profile) == pytest.approx(
        max(0.002, 2 * cold)
    )  # unbatched: no 50% discount
    assert rescore.reserve_per_job(conn, model, profile=profile) == pytest.approx(expected)
    assert rescore.cost_per_job(conn, model, profile=profile)[0] == pytest.approx(expected)


def test_the_holds_in_numbers(conn, profile):
    p = cold_prefix(profile)
    assert screen.reserve_per_request_usd(conn, HAIKU55, profile) == 0.002  # cold is ~$0.0007
    sonnet = screen.reserve_per_request_usd(conn, SONNET, profile)
    opus = screen.reserve_per_request_usd(conn, OPUS55, profile)
    assert sonnet > SONNET_WARM and opus == pytest.approx(2 * sonnet)
    assert p > screen.TYPICAL_USAGE.cache_read_input_tokens  # the rubric alone is ~5,200


def test_the_cold_floor_stays_after_history(conn, profile):
    """Review B2: a measured mean below the no-cache-reuse price does not lower the hold or
    the plan's price; one above it raises both."""
    cold = cold_batch_usd(2.0, 10.0, cold_prefix(profile))
    add_measured(conn, profile, SONNET, 0.005)
    assert screen.reserve_per_request_usd(conn, SONNET, profile) == pytest.approx(cold)
    assert rescore.cost_per_job(conn, SONNET, profile=profile) == (pytest.approx(cold), "floor")
    add_measured(conn, profile, SONNET, 0.05, n=30, start=30_000)  # mean now above it
    mean = (3 * 0.005 + 30 * 0.05) / 33
    assert screen.reserve_per_request_usd(conn, SONNET, profile) == pytest.approx(mean)
    assert rescore.cost_per_job(conn, SONNET, profile=profile) == (
        pytest.approx(mean),
        "fit_score",
    )


def test_which_screening_models_are_offered_now():
    assert catalog.judge("screen", HAIKU55).offered
    assert catalog.judge("screen", SONNET).offered  # within its own reservation now
    opus = catalog.judge("screen", OPUS55)
    assert not opus.offered and "thinking" in opus.reason  # never: thinking cannot be off
    assert SONNET in [o.value for o in catalog.options("screen").options]


def test_the_prefix_is_sized_from_the_real_profile(profile):
    big = profile.model_copy(update={"resume_text": "x" * 30_000})
    assert screen.prefix_tokens(big) == cold_prefix(big) > screen.prefix_tokens(profile)
    # The rubric and schema are counted, not assumed.
    assert screen.prefix_tokens(profile) * 2.75 >= len(RUBRIC_TEXT) + len(scoring_inputs(profile))


# ─── B1: the reviewer's case ────────────────────────────────────────────────


def _real_profile(profile):
    """A profile whose prefix is about the 7,100 tokens measured on real data, taking the
    newer tokenizer at 3.08 characters a token (4 / 1.3)."""
    fixed = len(RUBRIC_TEXT) + len(json.dumps(SCREEN_SCHEMA))
    pad = int(7100 * 3.08) - fixed
    return profile.model_copy(update={"resume_text": "y" * pad})


def _billed(custom_id: str, reads: int = 2130, writes: int = 4970):
    """A Sonnet answer billed as on real data: 2,600 posting tokens, a 7,100-token prefix
    (by default 30% read from the cache and 70% written), 455 out."""
    message = {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": screen_json([QUOTE])}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 2600,
            "output_tokens": 455,
            "cache_read_input_tokens": reads,
            "cache_creation_input_tokens": writes,
        },
    }
    from anthropic.types.messages import MessageBatchIndividualResponse

    return MessageBatchIndividualResponse.model_validate(
        {"custom_id": custom_id, "result": {"type": "succeeded", "message": message}}
    )


# Sonnet, batched, by hand: 2,600 x $2 + 4,970 x $2 x 1.25 + 2,130 x $2 x 0.05 + 455 x $10.
REAL_SONNET_JOB = (2600 * 2.0 + 4970 * 2.0 * 1.25 + 2130 * 2.0 * 0.05 + 455 * 10.0) * 0.5 / 1e6


def test_a_new_models_first_batch_stays_under_the_cap_at_a_30pc_cache_hit_rate(conn, profile):
    """Review B1: Sonnet, $2 cap, 400 groups. The warm hold ($0.00507) admitted 394 jobs that
    cost about $4.41 at a 30% cache hit rate. The cold hold admits what $2 really covers."""
    real = _real_profile(profile)
    assert round(REAL_SONNET_JOB, 6) == 0.011194
    assert 394 * REAL_SONNET_JOB > 2 * 2.0  # what the old hold let through: over twice the cap
    add_groups(conn, real, 400)
    scoring = scoring_with(2.0, SONNET)
    client = FakeClient()
    batch_id = screen.submit_batch(
        conn,
        client,
        real,
        limit=1000,
        now=NOW,
        scorer=SONNET,
        remaining_usd=lambda: screen.remaining_budget(conn, scoring, NOW),
    )
    n = sent(client)
    assert 0 < n < 394
    assert screen._pending_batch_usd(conn) <= 2.0
    client.messages.batches.results_by_id[batch_id] = [
        _billed(r["custom_id"]) for r in client.messages.batches.created[0]
    ]
    result = screen.collect_batch(conn, client, batch_id, real, now=NOW)
    assert result.written == n
    assert spent(conn) == pytest.approx(n * REAL_SONNET_JOB)
    assert spent(conn) <= 2.0  # what it really cost stays under the cap


def _sonnet_job(reads: int, writes: int) -> float:
    """By hand: one Sonnet batch job billed with ``reads`` and ``writes`` of the prefix."""
    return (2600 * 2.0 + writes * 2.0 * 1.25 + reads * 2.0 * 0.05 + 455 * 10.0) * 0.5 / 1e6


@pytest.mark.parametrize(("reads", "writes"), [(2130, 4970), (0, 7100)])
def test_a_lucky_first_collect_does_not_lower_the_next_hold(conn, profile, reads, writes):
    """Review B2: a first batch collected at 98% cache hits made the measured mean cheap,
    and the next batch at 30% or 0% hits overran the cap 2.3-2.9x. The cold floor stays."""
    real = _real_profile(profile)
    add_groups(conn, real, 800)
    client = FakeClient()
    lucky = screen.submit_batch(
        conn, client, real, limit=50, now=NOW, scorer=SONNET, remaining_usd=lambda: 100.0
    )
    client.messages.batches.results_by_id[lucky] = [
        _billed(r["custom_id"], reads=6958, writes=142) for r in client.messages.batches.created[-1]
    ]
    screen.collect_batch(conn, client, lucky, real, now=NOW)
    lucky_job = _sonnet_job(6958, 142)
    assert screen.measured_cost_per_job_usd(conn, SONNET) == pytest.approx(lucky_job)
    before = spent(conn)

    # The next day's $2 cap, and the cache mostly misses this time.
    cap = 2.0
    nxt = screen.submit_batch(
        conn,
        client,
        real,
        limit=1000,
        now=NOW,
        scorer=SONNET,
        remaining_usd=lambda: cap - screen._pending_batch_usd(conn),
    )
    n = len(client.messages.batches.created[-1])
    assert n < math.floor(cap / lucky_job)  # the measured mean would have let this many go
    client.messages.batches.results_by_id[nxt] = [
        _billed(r["custom_id"], reads=reads, writes=writes)
        for r in client.messages.batches.created[-1]
    ]
    screen.collect_batch(conn, client, nxt, real, now=NOW)
    cost = spent(conn) - before
    assert cost == pytest.approx(n * _sonnet_job(reads, writes))
    assert cost <= 1.2 * cap
    assert cost <= cap  # in fact inside it: the floor prices every prefix token as a write


def test_save_rerun_all_on_a_new_model_is_planned_at_the_cold_hold(conn, profile):
    """The flow the review named: "Save + re-score -> All" on a model with no history."""
    real = _real_profile(profile)
    add_groups(conn, real, 400)
    plan = rescore.make_plan(conn, real, scoring_with(2.0, SONNET), "all", SONNET, NOW)
    cold = cold_batch_usd(2.0, 10.0, cold_prefix(real))
    assert plan.cost_per_job == pytest.approx(cold) and plan.reserve_per_job == pytest.approx(cold)
    assert plan.fits_now == math.floor(2.0 / cold)
    assert plan.fits_now * REAL_SONNET_JOB <= 2.0


# ─── Estimates match what is reserved ───────────────────────────────────────


def test_a_measured_price_below_the_reservation_is_shown_with_it(conn, profile):
    add_measured(conn, profile, HAIKU55, 0.0001)  # below its floors
    add_groups(conn, profile, 10)
    plan = rescore.make_plan(conn, profile, scoring_with(100.0), "all", HAIKU55, NOW)
    # Priced at its no-cache-reuse floor (about $0.0006), held at the $0.002 floor.
    cold = cold_batch_usd(0.10, 0.50, cold_prefix(profile))
    assert plan.cost_per_job == pytest.approx(cold) and plan.cost_source == "floor"
    assert plan.reserve_per_job == pytest.approx(0.002)
    assert plan.reserve_differs and plan.reserved_usd == pytest.approx(10 * 0.002)
    assert plan.refusal is None and plan.fits_now == 10


def test_a_jev_plan_reserves_its_own_estimate(conn):
    """Decisions models check the cap per request on their own estimate (unchanged)."""
    assert rescore.reserve_per_job(conn, "jev:typesafe/jev-1.13") == rescore.DECISIONS_FALLBACK_COST


def test_packed_holds_agree_with_what_the_packed_run_checks(conn, tmp_path):
    """Review N2: the page and plan show the per-wave figure the packed run uses."""
    path = tmp_path / "openrouter_models.json"
    write_catalog(path, {PRICEY: (1.0, 5.0)})
    cfg = OpenRouter(catalog_cache=path, jobs_per_request=8)
    scoring = Scoring(screen_scorer=f"openrouter:{PRICEY}", openrouter=cfg)
    api = ChatAPI(cost=0.0)
    scorer = router(tmp_path, PRICEY, api)
    scorer.jobs_per_request = 8
    run = screen.estimated_request_cost(conn, scorer, NOW, 8)
    # By hand: the $0.04 default per request, or 8 postings + one shared prefix at $1 / $5.
    pack = ((3900 + 2600 * 8) * 1.0 + 455 * 8 * 5.0) / 1e6
    assert run == pytest.approx(max(0.04, pack))
    spec = f"openrouter:{PRICEY}"
    assert rescore.reserve_per_job(conn, spec, scoring, now=NOW) == pytest.approx(run / 8)
    assert rescore.reserve_per_job(conn, spec, scoring, now=NOW) < PRICEY_PER_JOB
    # Score now sends a pack of one, held at that pack's figure.
    one = screen.estimated_request_cost(conn, scorer, NOW, 1)
    assert rescore.reserve_per_job(conn, spec, scoring, now=NOW, jobs=1) == pytest.approx(one)
    assert api.calls == 0


# ─── Cap admission at the boundary ──────────────────────────────────────────


@pytest.mark.parametrize("model", [HAIKU55, SONNET])
def test_batch_admission_stops_exactly_at_the_cap(conn, profile, model):
    add_groups(conn, profile, 20)
    per = max(0.002, cold_batch_usd(*RATES[model], cold_prefix(profile)))
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
        remaining_usd=lambda: 7 * per * 0.999,
    )
    assert sent(client) == 6  # a hair under seven: six


def test_the_plan_and_the_batch_agree_at_the_boundary(conn, profile):
    """Review N6: ``fits_now`` and ``submit_batch`` make the same comparison."""
    add_groups(conn, profile, 20)
    per = cold_batch_usd(2.0, 10.0, cold_prefix(profile))
    for left in (7 * per, 7 * per * (1 - 1e-12), 7 * per * 0.999):
        conn.execute("DELETE FROM score_batch_item")
        conn.execute("DELETE FROM score_batch")
        plan = rescore.make_plan(conn, profile, scoring_with(left, SONNET), "all", SONNET, NOW)
        client = FakeClient()
        screen.submit_batch(
            conn,
            client,
            profile,
            limit=100,
            now=NOW,
            scorer=SONNET,
            remaining_usd=lambda left=left: left,
        )
        assert sent(client) == plan.fits_now, left


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
        conn, scorer, profile, limit=100, now=NOW, remaining_usd=lambda: per * 0.999
    )
    assert res.submitted == 0 and api.calls == 3  # less than one job's reservation: nothing


def test_plan_fits_now_at_the_boundary(conn, profile):
    # Haiku 5.5: estimated at its no-cache-reuse floor (about $0.0006), held at $0.002.
    cold = cold_batch_usd(0.10, 0.50, cold_prefix(profile))
    assert 2 * cold < 0.0019
    add_groups(conn, profile, 2)
    add_measured(conn, profile, HAIKU55, 0.0001)
    # The estimate for 2 fits, but less than one job's hold is left: refused up front
    # instead of sending nothing after Run.
    plan = rescore.make_plan(conn, profile, scoring_with(0.0019), "all", HAIKU55, NOW)
    assert plan.estimated_usd == pytest.approx(2 * cold)
    assert plan.fits_now == 0
    assert plan.refusal and "holds $0.0020 a job" in plan.refusal
    assert "$0.0019 is left" in plan.refusal  # under a cent: four decimals, not "$0.00"
    # Exactly one job's hold: one fits, not refused.
    plan = rescore.make_plan(conn, profile, scoring_with(0.002), "all", HAIKU55, NOW)
    assert plan.refusal is None and plan.fits_now == 1
    # Ten groups: the estimate fits a $0.01 cap, but only 5 jobs fit the $0.002 hold.
    add_groups(conn, profile, 8, start=3)
    plan = rescore.make_plan(conn, profile, scoring_with(0.01), "all", HAIKU55, NOW)
    assert plan.estimated_usd == pytest.approx(10 * cold)
    assert plan.refusal is None and plan.fits_now == 5


def test_score_now_is_refused_when_the_hold_does_not_fit(conn, profile):
    gid = add_group(conn, 1, profile)
    add_measured(conn, profile, HAIKU55, 0.0003)
    cold = cold_batch_usd(0.10, 0.50, cold_prefix(profile))  # about $0.0006, the estimate
    est = group_score.estimate(conn, profile, scoring_with(0.001), gid, NOW)
    assert est.estimated_usd == pytest.approx(cold) and cold < 0.001  # fits the $0.001 left
    assert est.reserve_usd == pytest.approx(0.002) and est.reserve_differs
    # Review N4: it is the hold that is over, and it says so.
    assert est.refusal and est.refusal.startswith("the spend cap holds $0.0020 for this job")
    assert f"estimated ${cold:.4f}" in est.refusal and "the $0.0010 left" in est.refusal
    assert "is over the remaining" not in est.refusal
    est = group_score.estimate(conn, profile, scoring_with(0.002), gid, NOW)
    assert est.refusal is None  # exactly the hold: admitted


# ─── The transition: batches in flight across the switch ────────────────────


def _submit(conn, profile, client, model, ids) -> str:
    batch_id = screen.submit_batch(
        conn, client, profile, limit=len(ids), now=NOW, scorer=model, group_ids=ids
    )
    assert batch_id
    return batch_id


def test_batches_in_flight_keep_and_then_release_their_hold_across_the_switch(
    conn, profile, monkeypatch
):
    client = FakeClient()
    # Before the switch, the save rule (which reads the flag) asks for "save anyway" for
    # Sonnet; a Sonnet batch exists only because it was saved that way, beside a Haiku one.
    monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", False)
    assert mc.check_save("screen", SONNET).needs_force
    haiku_ids = add_groups(conn, profile, 4)
    sonnet_ids = add_groups(conn, profile, 3, start=5)
    haiku_batch = _submit(conn, profile, client, HAIKU55, haiku_ids)
    _submit(conn, profile, client, SONNET, sonnet_ids)
    sonnet_hold = cold_batch_usd(2.0, 10.0, cold_prefix(profile))
    before = screen._pending_batch_usd(conn)
    assert before == pytest.approx(4 * 0.002 + 3 * sonnet_hold)

    # The switch: the save rule now lets Sonnet through, and nothing in flight moves.
    monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", True)
    assert mc.check_save("screen", SONNET).ok
    assert screen._pending_batch_usd(conn) == pytest.approx(before)
    # A new Sonnet submission sees the in-flight holds: with room for exactly two more jobs
    # on top of them, it sends two.
    more = add_groups(conn, profile, 5, start=8)
    cap = before + 2 * sonnet_hold
    screen.submit_batch(
        conn,
        client,  # one client, so its batch ids stay unique
        profile,
        limit=10,
        now=NOW,
        scorer=SONNET,
        group_ids=more,
        remaining_usd=lambda: cap - screen._pending_batch_usd(conn),
    )
    assert len(client.messages.batches.created[-1]) == 2

    # Collecting the Haiku batch releases exactly its hold and records what it really cost.
    client.messages.batches.results_by_id[haiku_batch] = [
        succeeded(f"g{gid}", screen_json([QUOTE])) for gid in haiku_ids
    ]
    result = screen.collect_batch(conn, client, haiku_batch, profile, now=NOW)
    assert result.written == 4
    assert screen._pending_batch_usd(conn) == pytest.approx(5 * sonnet_hold)
    assert spent(conn) == pytest.approx(result.cost_usd) and spent(conn) > 0
    # Collecting it again releases nothing more and records nothing twice.
    assert screen.collect_batch(conn, client, haiku_batch, profile, now=NOW).status == (
        "already_collected"
    )
    assert screen._pending_batch_usd(conn) == pytest.approx(5 * sonnet_hold)


def test_a_plan_confirmed_at_the_flat_price_runs_at_the_model_hold(conn, profile):
    """A re-score confirmed before per-model pricing, at $0.002 a job for Sonnet, is cut to
    what its confirmed ceiling covers at Sonnet's hold (rescore._priced_on_flat_default)."""
    add_groups(conn, profile, 40)
    plan = rescore.make_plan(conn, profile, scoring_with(100.0, SONNET), "all", SONNET, NOW)
    rid = rescore.create_request(conn, profile, plan, NOW)
    conn.execute(
        "UPDATE rescore_request SET cost_per_job_usd = 0.002, max_usd = 0.10 WHERE id = ?", (rid,)
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
    cold = cold_batch_usd(2.0, 10.0, cold_prefix(profile))
    assert sent(client) == math.floor(0.10 / cold)


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
    assert before - after == pytest.approx(10 * cold_batch_usd(2.0, 10.0, cold_prefix(profile)))
