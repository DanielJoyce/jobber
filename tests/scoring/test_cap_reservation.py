"""Spend-cap reservations are priced per model (bug 026b50c). Fake client, no network."""

# ruff: noqa: F811

from __future__ import annotations

import math

import pytest
from test_screen import (
    NOW,
    FakeClient,
    add_group,
    cold_batch_usd,
    cold_prefix,
    conn,  # noqa: F401
    default_prefix,
    profile,  # noqa: F401
)

from jobhunter.config import Scoring
from jobhunter.scoring import rescore, screen

SONNET = "anthropic:claude-sonnet-5-5"
HAIKU55 = "anthropic:claude-haiku-5-5"
# Table prices at screen.TYPICAL_USAGE (2,600 in, 3,900 cache reads, 455 out), batched:
# Sonnet 5.5: (2600*2 + 3900*2*0.05 + 455*10) * 0.5 / 1e6
SONNET_TABLE = 0.00507
# Haiku 5.5: (2600*0.1 + 3900*0.1*0.1 + 455*0.5) * 0.5 / 1e6, under the $0.002 floor
HAIKU55_TABLE = 0.00026325


def add_groups(conn, profile, n: int) -> None:
    for i in range(1, n + 1):
        add_group(conn, i, profile)


def add_measured(conn, profile, model: str, cost: float, n: int = 3) -> None:
    """Scored history for ``model`` at ``cost`` per job, under an old scoring_version."""
    for i in range(n):
        gid = add_group(conn, 10_000 + i, profile, passed=False)
        conn.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, cost_usd, created_at) "
            "VALUES (?, 'screen', ?, 'p', 'old', 'weak', 10, '{}', '[]', ?, 'x')",
            (gid, model, cost),
        )


def sent(client: FakeClient) -> int:
    return sum(len(b) for b in client.messages.batches.created)


def test_table_prices():
    assert screen.table_price_per_request_usd(SONNET) == pytest.approx(SONNET_TABLE)
    assert screen.table_price_per_request_usd(HAIKU55) == pytest.approx(HAIKU55_TABLE)
    assert screen.table_price_per_request_usd("openrouter:x/y") == 0.0
    assert screen.table_price_per_request_usd("anthropic:claude-unknown") == 0.0


def test_haiku_55_still_reserves_the_flat_default(conn):
    assert screen.reserve_per_request_usd(conn, HAIKU55) == screen.ESTIMATED_COST_PER_REQUEST_USD
    assert screen.ESTIMATED_COST_PER_REQUEST_USD == 0.002


def test_sonnet_screening_never_submits_more_than_the_daily_cap_covers(conn, profile):
    add_groups(conn, profile, 500)
    scoring = Scoring(screen_scorer=SONNET, daily_cap_usd=2.0, weekly_cap_usd=100.0)
    client = FakeClient()

    def remaining() -> float:
        return screen.remaining_budget(conn, scoring, NOW)

    assert screen.submit_batch(
        conn, client, profile, limit=1000, now=NOW, scorer=SONNET, remaining_usd=remaining
    )
    n = sent(client)
    # No Sonnet history: held with its prefix all cache writes (the flat $0.002 allowed 1000,
    # the warm table price 394).
    cold = cold_batch_usd(2.0, 10.0, cold_prefix(profile))
    assert n == math.floor(2.0 / cold) < 394
    assert n * cold <= 2.0
    # The uncollected batch stays held at that price, so nothing more fits today even though
    # groups are still eligible.
    assert remaining() == pytest.approx(2.0 - n * cold)
    assert (
        screen.submit_batch(
            conn, client, profile, limit=1000, now=NOW, scorer=SONNET, remaining_usd=remaining
        )
        is None
    )
    assert sent(client) == n


def test_haiku_55_screening_unchanged(conn, profile):
    add_groups(conn, profile, 20)
    client = FakeClient()
    screen.submit_batch(
        conn, client, profile, limit=100, now=NOW, scorer=HAIKU55, remaining_usd=lambda: 0.021
    )
    assert sent(client) == 10  # 0.021 / 0.002


def test_pending_batches_reserve_per_model(conn):
    for bid, model, n in (("b1", HAIKU55, 100), ("b2", SONNET, 100), ("b3", "openrouter:x", 5)):
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "request_count, submitted_at) VALUES (?, 'screen', ?, 'p', 'v', ?, 'x')",
            (bid, model, n),
        )
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at, collected_at) VALUES ('b4', 'screen', ?, 'p', 'v', 999, "
        "'x', 'y')",
        (SONNET,),
    )
    # Rows from before the stored hold: Sonnet with no history is held with its prefix all
    # cache writes (default size, no profile at hand); openrouter:x has no price (no
    # catalog): over the line at the unpriced reserve.
    sonnet = cold_batch_usd(2.0, 10.0, default_prefix())
    expected = 100 * 0.002 + 100 * sonnet + 5 * screen.UNPRICED_RESERVE_PER_JOB_USD
    assert screen._pending_batch_usd(conn) == pytest.approx(expected)
    assert screen.remaining_daily_budget(conn, 2.0, NOW) == pytest.approx(2.0 - expected)


def test_measured_history_above_table_price_is_used(conn, profile):
    add_measured(conn, profile, SONNET, 0.009)
    assert screen.reserve_per_request_usd(conn, SONNET) == pytest.approx(0.009)
    add_groups(conn, profile, 50)
    client = FakeClient()
    screen.submit_batch(
        conn, client, profile, limit=100, now=NOW, scorer=SONNET, remaining_usd=lambda: 0.1
    )
    assert sent(client) == 11  # 0.1 / 0.009, not 0.1 / 0.00507 = 19


def test_measured_history_below_floor_keeps_the_floor(conn, profile):
    add_measured(conn, profile, HAIKU55, 0.0003)
    assert screen.reserve_per_request_usd(conn, HAIKU55) == 0.002


def test_rescore_fallback_cost_per_job_is_priced_per_model(conn):
    cold = cold_batch_usd(2.0, 10.0, default_prefix())
    assert rescore.cost_per_job(conn, SONNET) == (pytest.approx(cold), "default")
    assert rescore.cost_per_job(conn, HAIKU55) == (0.002, "default")
    # No cached catalog price: priced at the unpriced reserve, not the flat $0.002.
    assert rescore.cost_per_job(conn, "openrouter:x/y") == (0.04, "default")
    assert rescore.cost_per_job(conn, "jev:x") == (rescore.DECISIONS_FALLBACK_COST, "default")


def _run(conn, profile, spec, rid, client):
    return rescore.run_request(
        conn,
        rid,
        profile,
        Scoring(daily_cap_usd=100.0, weekly_cap_usd=100.0),
        now=lambda: NOW,
        client_factory=lambda: client,
        scorer=spec,
    )


def _queue(conn, profile, spec) -> int:
    plan = rescore.make_plan(
        conn, profile, Scoring(daily_cap_usd=100.0, weekly_cap_usd=100.0), "all", spec, NOW
    )
    return rescore.create_request(conn, profile, plan, NOW)


def test_rescore_batch_never_exceeds_the_confirmed_max_usd(conn, profile):
    add_groups(conn, profile, 50)
    rid = _queue(conn, profile, SONNET)
    # A plan confirmed under the old flat price: $0.002 a job, ceiling $0.10.
    conn.execute(
        "UPDATE rescore_request SET cost_per_job_usd = 0.002, max_usd = 0.10 WHERE id = ?",
        (rid,),
    )
    client = FakeClient()
    st = _run(conn, profile, SONNET, rid, client)
    n = sent(client)
    # No Sonnet history: cut at its first batch's hold, the prefix all cache writes. Before
    # 026b50c all 50 (about $0.25) were sent; before M5b's review, 19 at the warm $0.00507.
    cold = cold_batch_usd(2.0, 10.0, cold_prefix(profile))
    assert n == math.floor(0.10 / cold) < 19
    assert n * cold <= 0.10
    assert st.attempted == n
    note = rescore.get_request(conn, rid)["note"]
    assert f"batch cut to {n} of 50" in note


def test_rescore_batch_uses_a_higher_planned_price(conn, profile):
    add_groups(conn, profile, 50)
    rid = _queue(conn, profile, SONNET)
    conn.execute(
        "UPDATE rescore_request SET cost_per_job_usd = 0.01, max_usd = 0.10 WHERE id = ?", (rid,)
    )
    client = FakeClient()
    _run(conn, profile, SONNET, rid, client)
    assert sent(client) == 10


def test_rescore_batch_as_confirmed_sends_every_group(conn, profile):
    add_groups(conn, profile, 30)
    client = FakeClient()  # one client, so its batch ids stay unique
    for spec in (SONNET, HAIKU55):
        rid = _queue(conn, profile, spec)
        st = _run(conn, profile, spec, rid, client)
        assert len(client.messages.batches.created[-1]) == 30 and st.attempted == 30


def test_rescore_batch_priced_on_measured_history_below_table_sends_every_group(conn, profile):
    add_measured(conn, profile, SONNET, 0.003)  # measured, below the 0.00507 table price
    add_groups(conn, profile, 50)
    rid = _queue(conn, profile, SONNET)
    r = rescore.get_request(conn, rid)
    assert r["cost_per_job_usd"] == pytest.approx(0.003)
    assert r["max_usd"] < 50 * SONNET_TABLE  # the table price would cut this plan
    client = FakeClient()
    st = _run(conn, profile, SONNET, rid, client)
    assert sent(client) == 50 and st.attempted == 50
