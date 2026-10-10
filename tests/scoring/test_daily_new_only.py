"""The daily run scores new jobs only: re-scoring stored jobs after a paid change (scoring_version,
model, prompt) never happens automatically (specs/006, specs/014). Fake scorers; no network."""

# ruff: noqa: F811

from __future__ import annotations

import sys
import types

import pytest
from test_rescore import SPEC, FakeScorer, score_all_once
from test_screen import (
    NOW,
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
)

from jobhunter.config import Scoring, Settings
from jobhunter.pipeline import runner
from jobhunter.scoring import decisions, scorers, screen
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
