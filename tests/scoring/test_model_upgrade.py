"""Moving the default models to the Claude 5.5 series (bug 15f7012) is not a paid re-score.

Stored screens from Haiku 4.5 stay; the daily run neither re-queues them under the new default
nor under a scorer the user set explicitly. Old models stay priced. Fakes only; no network.
"""

# ruff: noqa: F811

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from test_rescore import FakeScorer
from test_screen import (
    NOW,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
)

from jobhunter.config import Scoring, Settings
from jobhunter.pipeline import runner
from jobhunter.scoring import scorers, screen
from jobhunter.scoring.scorers import ScoreRequest, anthropic_params, compute_cost

OLD_SCREEN = "anthropic:claude-haiku-4-5"
NEW_SCREEN = "anthropic:claude-haiku-5-5"
JEV = "jev:typesafe/jev-1.13"


def score_with(conn, profile, spec):
    """Score every waiting group once with a fake scorer recorded under ``spec``."""
    fake = FakeScorer()
    fake.name = spec
    return screen.score_sync(conn, fake, profile, limit=50, now=NOW)


def models(conn):
    return sorted((r[0], r[1]) for r in conn.execute("SELECT job_group_id, model FROM fit_score"))


def batch_route(monkeypatch):
    """Capture what the daily run's Anthropic batch route would submit, and with which model."""
    sent: list[tuple[str, list[str]]] = []

    class FakeBatch:
        def __init__(self, client, model):
            self.model = model

        def submit(self, requests):
            sent.append((self.model, [r.custom_id for r in requests]))
            return "batch_fake"

    monkeypatch.setattr(screen, "AnthropicScorer", FakeBatch)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: None))
    return sent


def test_the_default_screen_scorer_is_haiku_5_5():
    assert Settings().scoring.screen_scorer == NEW_SCREEN
    assert Settings().scoring.deep_scorer == "anthropic:claude-opus-5-5"


def test_default_users_keep_their_haiku_4_5_screens(conn, profile, monkeypatch):
    """Rows scored by the old default are not re-queued for the new default by a run."""
    old = [add_group(conn, n, profile) for n in (1, 2, 3)]
    assert score_with(conn, profile, OLD_SCREEN).written == 3
    before = models(conn)
    sent = batch_route(monkeypatch)
    report = runner.RunReport()
    runner._score_stage(conn, Settings(), profile, NOW, report)  # default config
    assert sent == []  # nothing submitted, nothing paid
    assert models(conn) == before
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0
    # Without the daily run's new-only rule they would be eligible again: the guard matters.
    assert len(screen.eligible_groups(conn, profile, scorer=NEW_SCREEN, limit=10)) == len(old)


def test_default_users_get_new_jobs_screened_by_haiku_5_5(conn, profile, monkeypatch):
    for n in (1, 2):
        add_group(conn, n, profile)
    score_with(conn, profile, OLD_SCREEN)
    new = add_group(conn, 3, profile)
    sent = batch_route(monkeypatch)
    runner._score_stage(conn, Settings(), profile, NOW, runner.RunReport())
    assert sent == [(NEW_SCREEN, [f"g{new}"])]


def test_an_explicit_screen_scorer_is_untouched_and_not_rescored(conn, profile, monkeypatch):
    """A user who set screen_scorer (here Jev) sees no change at all from the new default."""
    for n in (1, 2):
        add_group(conn, n, profile)
    score_with(conn, profile, JEV)
    before = models(conn)
    fake = FakeScorer()
    fake.name = JEV
    made: list[str] = []
    monkeypatch.setattr(
        scorers, "scorer_from_string", lambda s, scoring=None: made.append(s) or fake
    )
    settings = Settings(scoring=Scoring(screen_scorer=JEV))
    runner._score_stage(conn, settings, profile, NOW, runner.RunReport())
    assert made == [JEV]  # the configured scorer, never the new default
    assert fake.calls == 0
    assert models(conn) == before


# ─── Prices (platform.claude.com/docs/en/about-claude/pricing, 2026-10-11) ──


def usage(i=0, o=0, w=0, r=0):
    return SimpleNamespace(
        input_tokens=i, output_tokens=o, cache_creation_input_tokens=w, cache_read_input_tokens=r
    )


@pytest.mark.parametrize(
    "model, rates",
    [
        ("anthropic:claude-haiku-5-5", (0.10, 0.50)),
        ("anthropic:claude-sonnet-5-5", (2.00, 10.00)),
        ("anthropic:claude-opus-5-5", (4.00, 20.00)),
        # Kept so stored spend, open batches and confirmed plans still price.
        ("anthropic:claude-haiku-4-5", (1.00, 5.00)),
        ("claude-opus-5", (5.00, 25.00)),
    ],
)
def test_standard_and_batch_rates(model, rates):
    u = usage(i=100_000, o=100_000)  # a 100,000-token prompt: Haiku 5.5's short rate card
    assert compute_cost(u, model, batch=False) == pytest.approx(sum(rates) / 10)
    assert compute_cost(u, model, batch=True) == pytest.approx(sum(rates) / 20)


@pytest.mark.parametrize(
    "model, read_rate",
    [
        ("claude-opus-5-5", 4.00 * 0.05),  # $0.20
        ("claude-sonnet-5-5", 2.00 * 0.05),  # $0.10
        ("claude-haiku-5-5", 0.10 * 0.1),  # $0.01
        ("claude-haiku-4-5", 1.00 * 0.1),
        ("claude-opus-5", 5.00 * 0.1),
    ],
)
def test_cache_read_multiplier_per_model(model, read_rate):
    assert compute_cost(usage(r=10_000), model, batch=False) == pytest.approx(read_rate / 100)


def test_cache_writes_are_1_25x_input():
    assert compute_cost(usage(w=1_000_000), "claude-opus-5-5", batch=False) == pytest.approx(5.00)
    assert compute_cost(usage(w=100_000), "claude-haiku-5-5", batch=False) == pytest.approx(0.0125)


def test_haiku_5_5_long_prompts_pay_the_higher_rate_card():
    at = usage(i=60_000, r=40_000, o=1_000)  # exactly 100,000 prompt tokens: short rates
    assert compute_cost(at, "claude-haiku-5-5", batch=False) == pytest.approx(
        (60_000 * 0.10 + 40_000 * 0.01 + 1_000 * 0.50) / 1e6
    )
    # One more token, counting cache reads and writes, moves the whole request to $0.50/$2.50.
    over = usage(i=60_000, w=1, r=40_000, o=1_000)
    assert compute_cost(over, "claude-haiku-5-5", batch=True) == pytest.approx(
        (60_000 * 0.50 + 1 * 0.625 + 40_000 * 0.05 + 1_000 * 2.50) / 1e6 / 2
    )


# ─── Thinking off on bounded calls ─────────────────────────────────────────

REQ = ScoreRequest(custom_id="g1", system="rubric", profile="profile", posting="p", schema={})


@pytest.mark.parametrize(
    "model, thinking",
    [
        ("claude-haiku-5-5", {"type": "disabled"}),  # on by default there
        ("claude-sonnet-5-5", {"type": "between_tools"}),  # rejects "disabled"
        ("claude-haiku-4-5", None),  # thinks only when asked
    ],
)
def test_screen_params_keep_thinking_off(model, thinking):
    p = anthropic_params(REQ, model)
    assert p.get("thinking") == thinking
    assert "effort" not in p["output_config"]
    assert not {"temperature", "top_p", "top_k"} & p.keys()
