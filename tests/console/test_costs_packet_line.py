"""/costs shows packet drafting on its own line, against its own cap (specs/017)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from jobhunter.apply import runner_state
from jobhunter.config import Apply, Paths, Scoring, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def test_costs_page_has_a_packet_line_and_scoring_totals_exclude_it(tmp_path):
    db_path = tmp_path / "t.db"
    c = db.connect(db_path)
    db.migrate(c)
    c.executemany(
        "INSERT INTO llm_spend VALUES (?, ?, ?, ?, 0, 0, ?)",
        [
            ("2026-10-10", "anthropic:claude-haiku-4-5", "screen", 10, 0.40),
            ("2026-10-10", "claude-opus-5", "packet", 2, 0.30),
            ("2026-10-09", "claude-opus-5", "packet", 1, 0.15),
            ("2026-10-10", "claude-opus-5", "packet-cli", 7, 0.0),
        ],
    )
    c.close()
    settings = Settings(
        paths=Paths(db_path=db_path, data_dir=tmp_path / "data", profile_dir=tmp_path),
        scoring=Scoring(daily_cap_usd=2.0, weekly_cap_usd=10.0),
        apply=Apply(daily_cap_usd=1.0),
    )
    runner_state.turn_off(settings.paths.data_dir, "the CLI would authenticate with an API key")
    client = TestClient(create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW))
    html = client.get("/costs?days=7").text
    packet = html[html.index('id="packet-costs"') :]
    assert re.search(
        r"API today vs apply cap</span><span class=\"big\">\$0\.30</span>of \$1\.00", packet
    )
    assert re.search(r"API, 7d</span><span class=\"big\">\$0\.45</span>3 calls", packet)
    assert re.search(r"Subscription calls, 7d</span><span class=\"big\">7</span>", packet)
    # Scoring totals against the scoring caps see only the $0.40 screen row.
    assert re.search(r"Scoring spend, 7d</span><span class=\"big\">\$0\.40</span>", html)
    assert re.search(r"Today vs daily cap</span><span class=\"big\">\$0\.40</span>", html)
    assert "CLI runner off." in html and 'id="runner-on"' in html
