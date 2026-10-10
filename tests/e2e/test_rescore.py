"""Re-score now on /prefs in a real browser, with a fake scorer injected into the app."""

from __future__ import annotations

import json
import sqlite3

import pytest
from playwright.sync_api import expect

from jobhunter.scoring.scorers import ScoreResult

pytestmark = pytest.mark.e2e

BODY = json.dumps(
    {
        "verdict": "strong",
        "dimensions": {
            k: {"score": 90, "why": "match: ok"} for k in ("skills", "seniority", "domain")
        },
        "raw_skills": 90,
        "recency_weighted_skills": 90,
        "stale_skills": [],
        "current_focus_overlap": 50,
        "done_with_hits": [],
        "evidence": [{"claim": "linux", "quote": "manage Linux servers daily"}],
        "blockers": [],
        "missing_info": [],
        "shape_flags": [],
        "tailoring_hints": [],
    }
)


class FakeScorer:
    name = "test:model"

    def submit(self, requests):
        return [ScoreResult(custom_id=r.custom_id, status="succeeded", text=BODY) for r in requests]

    def cost(self, usage, batch):
        return 0.0


def test_rescore_now_confirms_runs_and_links_to_the_inbox(page, server):
    server.app.state.rescore_scorer_factory = lambda spec: FakeScorer()
    # Seeded jobs are only 'listed'; the run prefilters them against the current filters first.
    c = sqlite3.connect(server.db_path)
    c.execute("UPDATE job SET stage = 'grouped'")
    c.commit()
    c.close()
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(f"{server.url}/prefs")
    panel = page.locator("#sec-rescore")
    expect(panel).to_contain_text("Re-score now")
    panel.locator("select[name=scope]").select_option("all")
    expect(panel.locator("#rescore-estimate")).to_contain_text("Will score")
    panel.locator("#rescore-run").click()
    expect(panel.locator(".rescore-done")).to_contain_text("Done.", timeout=15000)
    assert dialogs and "spends credits" in dialogs[0] and "test:model" in dialogs[0]
    expect(panel.locator(".rescore-done a")).to_have_attribute("href", "/inbox")
    scored = server.rows("SELECT count(*) FROM fit_score WHERE model = 'test:model'")[0][0]
    assert scored > 0
    row = server.rows("SELECT status, scored FROM rescore_request")[0]
    assert row[0] == "done" and row[1] == scored
    assert not page.errors
