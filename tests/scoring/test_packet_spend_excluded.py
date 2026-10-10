"""Packet drafting spend never counts against the scoring caps (specs/017 "Spend cap").

One test per site the spec names: ``screen.remaining_daily_budget``, ``weekly_remaining``,
``backfill.estimate`` (``spent_today_usd``), the ``/costs`` totals against the scoring caps,
the dashboard spend KPI and its ``over_cap``, and ``inbox.weekly_cost``. Each seeds the same
day with a small scoring row plus large ``packet`` and ``packet-cli`` rows and checks that only
the scoring row is counted (each would fail if the packet rows leaked in).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobhunter.console import dashboard, inbox, pages
from jobhunter.core import db
from jobhunter.pipeline import backfill
from jobhunter.scoring import screen
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
DAY = "2026-10-10"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    rows = [
        (DAY, "anthropic:claude-haiku-4-5", "screen", 10, 0.50),
        (DAY, "claude-opus-5", "packet", 3, 7.00),
        (DAY, "opus", "packet-cli", 4, 9.00),  # never written non-zero, but must not count
    ]
    c.executemany(
        "INSERT INTO llm_spend (day, model, tier, calls, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, 0, 0, ?)",
        rows,
    )
    yield c
    c.close()


def test_remaining_daily_budget_ignores_packet_tiers(conn):
    assert screen.remaining_daily_budget(conn, 2.0, NOW) == pytest.approx(1.5)


def test_weekly_remaining_ignores_packet_tiers(conn):
    assert screen.weekly_remaining(conn, 10.0, NOW) == pytest.approx(9.5)


class _Scorer:
    name = "anthropic:claude-haiku-4-5"
    supports_batching = True

    def cost(self, usage, batch=False):
        return 0.001


def test_backfill_estimate_spent_today_ignores_packet_tiers(conn):
    est = backfill.estimate(conn, Profile(), _Scorer(), budget_usd=2.0, now=NOW)
    assert est.spent_today_usd == pytest.approx(0.5)


def test_costs_totals_ignore_packet_tiers_but_list_them(conn):
    c = pages.costs(conn, NOW, 7, 2.0, 10.0)
    assert c.today == pytest.approx(0.5)
    assert c.last7 == pytest.approx(0.5)
    assert c.total == pytest.approx(0.5)
    assert sum(cost for _, cost in c.weekly) == pytest.approx(0.5)
    assert dict(c.daily)[DAY] == pytest.approx(0.5)
    # The by-model breakdown still lists the packet rows (they are real calls).
    assert {m["tier"] for m in c.by_model} == {"screen", "packet", "packet-cli"}


def test_dashboard_spend_kpi_and_over_cap_ignore_packet_tiers(conn):
    k = dashboard.kpis(conn, Profile(), 7, NOW, weekly_cap_usd=1.0)
    assert k["llm_spend"]["week_usd"] == pytest.approx(0.5)
    assert k["llm_spend"]["over_cap"] is False
    assert max(k["llm_spend"]["series"]) == pytest.approx(0.5)


def test_inbox_weekly_cost_ignores_packet_tiers(conn):
    assert inbox.weekly_cost(conn, NOW) == pytest.approx(0.5)
