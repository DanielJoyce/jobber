"""Per-model spend-cap reservations outside the Anthropic batch path (bug df51dff).

score_sync (the nightly run's non-Anthropic route), unpriced Anthropic specs, collecting a batch
of an unpriced model, backfill's pending-batch estimate and the /prefs paid-change estimate.
httpx.MockTransport and fake clients only: no network, no paid API.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import math
import time

import httpx
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
    screen_json,
    succeeded,
)

from jobhunter.config import OpenAICompat, OpenRouter, Scoring, Settings, check_scorer_spec
from jobhunter.console import prefs as pf
from jobhunter.pipeline import backfill as bf
from jobhunter.pipeline import runner
from jobhunter.scoring import rescore, scorers, screen
from jobhunter.scoring.scorers import (
    AnthropicScorer,
    OpenAICompatScorer,
    OpenRouterScorer,
    ceiling_cost,
)

DESCRIPTION_QUOTE = "Write Terraform modules and Go tooling."
PRICEY = "pricey/model-large"  # $1 in / $5 out per Mtok in the cached catalog
CHEAP = "openai/gpt-oss-120b"  # $0.10 / $0.50
# chat_price_usd at TYPICAL_USAGE: every prompt token (2,600 + 3,900) at the input rate.
PRICEY_PER_JOB = (6500 * 1.0 + 455 * 5.0) / 1e6  # 0.008775, 4.4x the flat $0.002
SONNET = "anthropic:claude-sonnet-5-5"
HAIKU55 = "anthropic:claude-haiku-5-5"
SONNET_TABLE = 0.00507
UNPRICED_ANTHROPIC = "anthropic:claude-unknown-9"


CATALOG = {PRICEY: (1.0, 5.0), CHEAP: (0.10, 0.50)}


def write_catalog(path, prices: dict[str, tuple[float, float]]) -> None:
    """A cache written just now, so a scorer reads it rather than refetching."""
    path.write_text(json.dumps({"fetched_at": time.time(), "prices": prices}))


class ChatAPI:
    """An OpenRouter endpoint: answers every screen and reports ``cost`` per call, and serves
    the public ``/models`` catalog (``CATALOG``), counting fetches."""

    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.calls = 0
        self.catalog_fetches = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.catalog_fetches += 1
            data = [
                {"id": k, "pricing": {"prompt": str(v[0] / 1e6), "completion": str(v[1] / 1e6)}}
                for k, v in CATALOG.items()
            ]
            data.append({"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}})
            return httpx.Response(200, json={"data": data})
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "model": "served/model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": screen_json([DESCRIPTION_QUOTE]),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 6500, "completion_tokens": 455, "cost": self.cost},
            },
        )


def router(tmp_path, slug: str, api: ChatAPI, *, catalog: bool = True) -> OpenRouterScorer:
    path = tmp_path / "openrouter_models.json"
    if catalog:
        write_catalog(path, CATALOG)
    config = OpenRouter(catalog_cache=path)
    client = httpx.Client(transport=httpx.MockTransport(api))
    return OpenRouterScorer(slug, config, client=client, env={"OPENROUTER_API_KEY": "or-key"})


def add_groups(conn, profile, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        add_group(conn, i, profile)


def spent(conn) -> float:
    return float(conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM llm_spend").fetchone()[0])


def capped(conn, daily: float = 2.0):
    scoring = Scoring(daily_cap_usd=daily, weekly_cap_usd=100.0)
    return lambda: screen.remaining_budget(conn, scoring, NOW)


def sync(conn, scorer, profile, remaining):
    return screen.score_sync(conn, scorer, profile, limit=2000, now=NOW, remaining_usd=remaining)


# ─── score_sync reserves per model ──────────────────────────────────────────


def test_reservation_prices_an_openrouter_slug_from_the_cached_catalog(conn, tmp_path):
    scorer = router(tmp_path, PRICEY, ChatAPI(0.0))
    scoring = screen.scoring_for(scorer)
    assert screen.list_price_per_job_usd(scorer.name, scoring) == pytest.approx(PRICEY_PER_JOB)
    assert screen.reserve_per_job_usd(conn, scorer.name, scoring) == pytest.approx(PRICEY_PER_JOB)
    # A cheap slug keeps the $0.002 floor, so its nightly volume does not drop.
    cheap = router(tmp_path, CHEAP, ChatAPI(0.0))
    assert screen.reserve_per_job_usd(conn, cheap.name, screen.scoring_for(cheap)) == 0.002


def test_expensive_openrouter_slug_never_submits_more_than_a_2_usd_cap_covers(
    conn, profile, tmp_path
):
    add_groups(conn, profile, 400)
    api = ChatAPI(PRICEY_PER_JOB)  # billed at what the catalog says
    res = sync(conn, router(tmp_path, PRICEY, api), profile, capped(conn))
    fits = math.floor(2.0 / PRICEY_PER_JOB)
    assert fits == 227  # the flat $0.002 let 400 (up to 1000) through, about $3.51 here
    assert res.submitted == api.calls == fits
    assert spent(conn) <= 2.0
    assert api.catalog_fetches == 0  # a fresh cache is not refetched


def test_a_model_dearer_than_its_reservation_stops_at_the_cap(conn, profile, tmp_path):
    add_groups(conn, profile, 400)
    api = ChatAPI(0.03)  # 3.4x what the catalog price predicts
    res = sync(conn, router(tmp_path, PRICEY, api), profile, capped(conn))
    # The reservation is re-read before each chunk with what the sent chunks cost, so the
    # run stops at the cap rather than at the 227 groups the catalog price allowed.
    assert res.submitted == math.floor(2.0 / 0.03 + 1e-9) == 66
    assert spent(conn) <= 2.0


def test_no_catalog_cache_is_fetched_once_and_both_nights_are_priced(conn, profile, tmp_path):
    # Nothing has written the cache (the normal state on a new install): the scoring run
    # refreshes it through the scorer's own catalog(), then prices from it.
    add_groups(conn, profile, 300)
    api = ChatAPI(0.0009)
    first = sync(conn, router(tmp_path, CHEAP, api, catalog=False), profile, capped(conn))
    assert api.catalog_fetches == 1
    assert (tmp_path / "openrouter_models.json").exists()
    assert first.submitted == 300  # $2 at the $0.002 floor covers 1000; 0.04 would allow 50
    add_groups(conn, profile, 300, start=301)
    second = sync(conn, router(tmp_path, CHEAP, api, catalog=False), profile, capped(conn))
    assert second.submitted == 300 and api.catalog_fetches == 1  # the cache is now read
    # Console estimates read the same cache, never fetching.
    routed = Scoring(
        screen_scorer=f"openrouter:{CHEAP}",
        openrouter=OpenRouter(catalog_cache=tmp_path / "openrouter_models.json"),
    )
    assert screen.list_price_per_job_usd(routed.screen_scorer, routed) is not None


def test_unpriced_slug_with_measured_history_is_priced_by_it(conn, profile, tmp_path):
    # openrouter/auto has variable pricing: no catalog price. OpenRouter reports what each
    # call cost, so after the first wave the measured mean prices it, not the $0.04 guess.
    add_groups(conn, profile, 100)
    api = ChatAPI(0.0004)
    scorer = router(tmp_path, "openrouter/auto", api)
    res = sync(conn, scorer, profile, capped(conn))
    assert res.submitted == 100  # not 50 at $0.04
    assert screen.reserve_per_job_usd(conn, scorer.name, screen.scoring_for(scorer)) == 0.002
    add_groups(conn, profile, 100, start=101)
    again = sync(conn, router(tmp_path, "openrouter/auto", api), profile, capped(conn))
    assert again.submitted == 100


def test_unpriced_slug_with_nothing_known_sends_one_wave_first(conn, profile, tmp_path):
    add_groups(conn, profile, 100)
    api = ChatAPI(0.24)  # a dear router, unknown until billed
    scorer = router(tmp_path, "openrouter/auto", api)
    assert screen.reserve_per_job_usd(conn, scorer.name, screen.scoring_for(scorer)) == 0.04
    res = sync(conn, scorer, profile, capped(conn))
    # One wave of max_concurrency (2) at $0.48, then the measured $0.24 a job: 6 more.
    assert res.submitted == 2 + 6
    assert spent(conn) <= 2.0


def test_unpriced_slug_that_reports_no_cost_stays_over_the_line(conn, profile, tmp_path):
    add_groups(conn, profile, 100)
    api = ChatAPI(0.0)  # no price and no measured cost: the $0.04 guess holds
    scorer = router(tmp_path, "nobody/unlisted-model", api)
    assert screen.list_price_per_job_usd(scorer.name, screen.scoring_for(scorer)) is None
    res = sync(conn, scorer, profile, capped(conn))
    assert res.submitted == api.calls == 50  # $2 / $0.04, not 100 at $0.002


def test_configured_estimate_above_the_ceiling_is_used_for_unpriced(conn, tmp_path):
    config = OpenRouter(catalog_cache=tmp_path / "none.json", est_cost_per_request_usd=0.1)
    scoring = Scoring(openrouter=config)
    assert screen.reserve_per_job_usd(conn, "openrouter:x/y", scoring) == pytest.approx(0.1)


def test_packed_estimate_is_floored_at_the_catalog_price(conn, tmp_path):
    pricey = router(tmp_path, PRICEY, ChatAPI(0.0))
    eight = screen.estimated_request_cost(conn, pricey, NOW, 8)
    assert eight == pytest.approx((3900 + 2600 * 8) * 1.0 / 1e6 + 455 * 8 * 5.0 / 1e6)
    assert eight > pricey.est_cost_per_request_usd  # 0.0429 > the configured 0.04
    cheap = router(tmp_path, CHEAP, ChatAPI(0.0))
    assert screen.estimated_request_cost(conn, cheap, NOW, 8) == 0.04  # unchanged


def test_packed_estimate_for_an_unpriced_slug_uses_measured_cost_per_job(conn, profile, tmp_path):
    gid = add_group(conn, 1, profile)
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, cost_usd, created_at) "
        "VALUES (?, 'screen', 'openrouter:openrouter/auto', 'p', 'old', 'weak', 10, '{}', "
        "'[]', 0.01, 'x')",
        (gid,),
    )
    auto = router(tmp_path, "openrouter/auto", ChatAPI(0.0))
    assert screen.estimated_request_cost(conn, auto, NOW, 8) == pytest.approx(0.08)


# ─── backfill's synchronous route stops at its budget ───────────────────────


def test_backfill_sync_route_stops_at_its_budget(conn, profile, tmp_path):
    add_groups(conn, profile, 60)
    api = ChatAPI(PRICEY_PER_JOB)
    scorer = router(tmp_path, PRICEY, api)
    est = bf.estimate(conn, profile, scorer, budget_usd=0.1, now=NOW)
    _, submitted, committed, _ = bf._submit(
        conn, profile, scorer, None, est, chunk_size=50, now=NOW, out=lambda s: None
    )
    # Its own per-job figure undercounts the cached prompt tokens; score_sync's per-model
    # reservation and recorded spend hold it to the budget.
    assert submitted == api.calls == math.floor(0.1 / PRICEY_PER_JOB) == 11
    assert spent(conn) <= 0.1 and committed == pytest.approx(spent(conn))


# ─── Unpriced Anthropic models ──────────────────────────────────────────────


def test_unpriced_anthropic_spec_is_refused_up_front():
    with pytest.raises(ValueError, match="no price"):
        check_scorer_spec(UNPRICED_ANTHROPIC, "screen_scorer")
    problems = Scoring(screen_scorer=UNPRICED_ANTHROPIC).scorer_problems()
    assert "no price" in problems["screen_scorer"]
    with pytest.raises(scorers.ScorerError):
        scorers.scorer_from_string(UNPRICED_ANTHROPIC, client=FakeClient())
    check_scorer_spec(SONNET)  # a priced model passes


def test_unpriced_anthropic_spec_submits_nothing(conn, profile, monkeypatch):
    add_groups(conn, profile, 5)
    client = FakeClient()
    with pytest.raises(ValueError, match="no price"):
        screen.submit_batch(
            conn, client, profile, limit=10, now=NOW, scorer=UNPRICED_ANTHROPIC, remaining_usd=None
        )
    assert client.messages.batches.created == []
    # The nightly run skips scoring with the reason instead of sending anything.
    import sys
    import types

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: client))
    report = runner.RunReport()
    runner._score_stage(
        conn, Settings(scoring=Scoring(screen_scorer=UNPRICED_ANTHROPIC)), profile, NOW, report
    )
    assert client.messages.batches.created == []
    assert any("no price" in m for m in report.messages)
    assert conn.execute("SELECT count(*) FROM score_batch").fetchone()[0] == 0


def _unpriced_batch(conn, profile) -> FakeClient:
    gid = add_group(conn, 1, profile)
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at) VALUES ('b1', 'screen', ?, ?, ?, 1, 'x')",
        (UNPRICED_ANTHROPIC, screen.PROMPT_VERSION, profile.scoring_version),
    )
    conn.execute(
        "INSERT INTO score_batch_item (batch_id, custom_id, job_group_id, input_rev) "
        "VALUES ('b1', ?, ?, 0)",
        (f"g{gid}", gid),
    )
    client = FakeClient()
    client.messages.batches.results_by_id["b1"] = [
        succeeded(f"g{gid}", screen_json([DESCRIPTION_QUOTE]))
    ]
    return client


def test_pending_unpriced_batch_reserves_the_dearest_rates(conn, profile):
    _unpriced_batch(conn, profile)
    # A batch from before the stored hold, no history: the dearest rates ($5 / $25), its
    # prefix all cache writes, sized with no profile at hand.
    ceiling = cold_batch_usd(5.0, 25.0, default_prefix())
    assert ceiling > ceiling_cost(screen.TYPICAL_USAGE, batch=True) > SONNET_TABLE
    assert screen._pending_batch_usd(conn) == pytest.approx(ceiling)


def test_collecting_an_unpriced_model_does_not_raise_or_stick(conn, profile):
    client = _unpriced_batch(conn, profile)
    result = screen.collect_batch(conn, client, "b1", profile, now=NOW)
    assert (result.succeeded, result.written) == (1, 1)
    # usage from test_screen.message: 2,000 in, 3,000 cache reads, 350 out
    usage = AnthropicUsage(2000, 3000, 350)
    assert result.cost_usd == pytest.approx(ceiling_cost(usage, batch=True))
    assert spent(conn) == pytest.approx(result.cost_usd) and spent(conn) > 0
    assert conn.execute("SELECT collected_at FROM score_batch").fetchone()[0] is not None
    assert screen._pending_batch_usd(conn) == 0.0  # the reservation is released
    results = screen.collect_pending(conn, client, profile, now=NOW)
    assert results == {}


class AnthropicUsage:
    def __init__(self, fresh: int, reads: int, out: int) -> None:
        self.input_tokens = fresh
        self.cache_read_input_tokens = reads
        self.cache_creation_input_tokens = 0
        self.output_tokens = out


# ─── backfill.estimate prices pending batches per model ─────────────────────


def test_backfill_estimate_prices_pending_batches_per_model(conn, profile):
    for bid, model in (("b1", HAIKU55), ("b2", SONNET)):
        conn.execute(
            "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
            "request_count, submitted_at) VALUES (?, 'screen', ?, 'p', 'v', 100, 'x')",
            (bid, model),
        )
    est = bf.estimate(
        conn, profile, AnthropicScorer(FakeClient(), HAIKU55), budget_usd=5.0, now=NOW
    )
    # Rows from before the stored hold, no history: Haiku 5.5 stays at the $0.002 floor;
    # Sonnet is held with its prefix all cache writes (no profile at hand: the default size).
    assert cold_batch_usd(0.10, 0.50, default_prefix()) < 0.002
    expected = 100 * 0.002 + 100 * cold_batch_usd(2.0, 10.0, default_prefix())
    assert est.pending_jobs == 200
    assert est.pending_usd == pytest.approx(expected)  # not 200 x Haiku's $0.002
    assert est.committed_usd == pytest.approx(expected)
    assert est.fundable == math.floor((5.0 - expected) / est.per_job.usd + 1e-9)


# ─── /prefs paid-change estimate prices the configured screen scorer ────────


def test_prefs_estimate_prices_the_configured_scorer(conn, profile, tmp_path):
    # History for another model does not price this one.
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, cost_usd, created_at) "
        "VALUES (?, 'screen', ?, 'p', 'old', 'weak', 10, '{}', '[]', 0.0001, 'x')",
        (add_group(conn, 1, profile, passed=False), HAIKU55),
    )
    assert pf.cost_per_job(conn) == (pytest.approx(0.0001), "fit_score")  # unconfigured: as before
    # No Sonnet history: priced at its first batch's hold, the prefix all cache writes.
    sonnet = pf.cost_per_job(conn, Scoring(screen_scorer=SONNET))
    assert sonnet == (pytest.approx(cold_batch_usd(2.0, 10.0, default_prefix())), "default")
    sonnet = pf.cost_per_job(conn, Scoring(screen_scorer=SONNET), profile)
    assert sonnet == (pytest.approx(cold_batch_usd(2.0, 10.0, cold_prefix(profile))), "default")
    write_catalog(tmp_path / "cat.json", {PRICEY: (1.0, 5.0)})
    routed = Scoring(
        screen_scorer=f"openrouter:{PRICEY}",
        openrouter=OpenRouter(catalog_cache=tmp_path / "cat.json"),
    )
    assert pf.cost_per_job(conn, routed) == (pytest.approx(PRICEY_PER_JOB), "default")
    assert pf.cost_per_job(conn, Scoring(screen_scorer="jev:typesafe/jev-1.13")) == (
        rescore.DECISIONS_FALLBACK_COST,
        "default",
    )
    # Measured history for the configured model wins, as on the Re-score panel.
    # Measured below its no-cache-reuse floor: priced at the floor (review B2).
    assert pf.cost_per_job(conn, Scoring(screen_scorer=HAIKU55)) == (
        pytest.approx(cold_batch_usd(0.10, 0.50, default_prefix())),
        "floor",
    )
    est = pf.rescore_estimate(conn, profile, NOW, Scoring(screen_scorer=SONNET))
    assert est["cost_per_job"] == pytest.approx(cold_batch_usd(2.0, 10.0, cold_prefix(profile)))
    for option in est["options"]:
        assert option.cost_usd == pytest.approx(option.jobs * est["cost_per_job"])


# ─── Re-score plans price from a refreshed catalog ──────────────────────────


def test_refresh_catalog_before_planning_prices_a_new_slug(conn, tmp_path):
    api = ChatAPI(0.0)
    scoring = Scoring(
        screen_scorer=f"openrouter:{CHEAP}",
        openrouter=OpenRouter(catalog_cache=tmp_path / "openrouter_models.json"),
    )
    # Disk only, nothing cached yet: the unpriced guess.
    assert rescore.cost_per_job(conn, scoring.screen_scorer, scoring) == (0.04, "default")
    rescore.refresh_catalog(
        scoring.screen_scorer, scoring, lambda spec: router(tmp_path, CHEAP, api, catalog=False)
    )
    assert api.catalog_fetches == 1
    assert rescore.cost_per_job(conn, scoring.screen_scorer, scoring) == (0.002, "default")
    rescore.refresh_catalog("jev:typesafe/jev-1.13", scoring, lambda spec: pytest.fail("no"))


def test_backfill_sync_route_holds_a_zero_cost_scorer_to_its_budget(conn, profile):
    # An openai-compat server with no configured prices records $0 a call; each chunk is
    # still counted at the $0.002 reservation, so the budget stops it (not 300 of 300).
    add_groups(conn, profile, 300)
    api = ChatAPI(0.0)
    scorer = OpenAICompatScorer(
        "free-model",
        OpenAICompat(base_url="https://llm.example.com"),
        client=httpx.Client(transport=httpx.MockTransport(api)),
        env={"OPENAI_COMPAT_API_KEY": "k"},
    )
    est = bf.estimate(conn, profile, scorer, budget_usd=0.1, now=NOW)
    assert est.per_job.usd == 0.002
    _, submitted, committed, stop = bf._submit(
        conn, profile, scorer, None, est, chunk_size=20, now=NOW, out=lambda s: None
    )
    assert submitted == api.calls == math.floor(0.1 / 0.002) == 50
    assert committed == pytest.approx(0.1) and "budget reached" in stop
