"""The nightly run's Jev volume at the default caps is untouched by per-model reservations
(bug df51dff). Jev requests check the cap per request on their own estimate (decisions.py).
httpx.MockTransport only: no network."""

# ruff: noqa: F811

from __future__ import annotations

import math

from test_decisions import (
    NOW,
    SPEC,
    FakeAPI,
    add_group,
    conn,  # noqa: F401
    make,
    profile,  # noqa: F401
)

from jobhunter.config import Scoring, Settings
from jobhunter.pipeline import runner
from jobhunter.scoring import decisions, scorers


def test_jev_nightly_sends_every_eligible_group_at_the_default_caps(conn, profile, monkeypatch):
    for n in range(1, 301):
        add_group(conn, n, profile)
    api = FakeAPI(cost=0.00013 * decisions.DEFAULT_JOBS_PER_REQUEST)  # as observed per job
    scorer = make(api)
    monkeypatch.setattr(scorers, "scorer_from_string", lambda s, scoring=None: scorer)
    settings = Settings(scoring=Scoring(screen_scorer=SPEC))
    assert (settings.scoring.daily_cap_usd, settings.scoring.weekly_cap_usd) == (2.0, 10.0)
    report = runner.RunReport()
    runner._score_stage(conn, settings, profile, NOW, report)
    assert len(api.bodies) == math.ceil(300 / decisions.DEFAULT_JOBS_PER_REQUEST) == 38
    assert report.counts["score"] == "300"
    assert not any("spend cap" in m for m in report.messages)
