"""``jobhunter score --deep`` and the worst-case admission (specs/020 M11), through the CLI.

The Anthropic client is a fake that records calls; nothing touches the network.
"""

from __future__ import annotations

import pytest
from test_deep import FakeClient, add_group, add_screen, final, profile, report  # noqa: F401
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db

cli = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch, profile):  # noqa: F811
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "profile").mkdir()
    db_path = tmp_path / "jh.db"

    def configure(daily: float, weekly: float = 10.0) -> None:
        (tmp_path / "config.toml").write_text(
            f'[paths]\ndata_dir = "{data}"\ndb_path = "{db_path}"\n'
            f'profile_dir = "{tmp_path / "profile"}"\n'
            f"[scoring]\ndaily_cap_usd = {daily}\nweekly_cap_usd = {weekly}\n"
        )

    configure(2.0)
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: profile)
    client = FakeClient(*[final(report()) for _ in range(3)])
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: client)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for n in range(1, 4):
        add_screen(conn, add_group(conn, n), profile)
    conn.commit()
    yield configure, client, conn
    conn.close()


def test_a_daily_cap_below_the_worst_case_runs_nothing_and_fails(env):
    configure, client, conn = env
    configure(0.50)
    out = cli.invoke(app, ["score", "--deep", "3"])
    assert out.exit_code == 1
    assert "Your daily cap $0.50 is below one call's worst case $0.83" in out.output
    assert "raise [scoring] daily_cap_usd to at least $0.83" in out.output
    assert client.calls == []
    assert conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0


def test_the_weekly_cap_is_named_when_it_binds(env):
    _, client, conn = env
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) "
        "VALUES (date('now', '-2 days'), 'anthropic:claude-opus-5-5', 'deep', 1, 9.5)"
    )
    conn.commit()
    out = cli.invoke(app, ["score", "--deep-group", "1"])
    assert out.exit_code == 1
    assert "of your weekly cap ($10.00) is left" in out.output
    assert "weekly_cap_usd" in out.output and client.calls == []


def test_with_room_under_the_cap_it_runs_until_the_worst_case_no_longer_fits(env):
    configure, client, conn = env
    configure(0.90)  # room for 0.83 worst case + about 0.05 spent, then capped
    out = cli.invoke(app, ["score", "--deep", "3"])
    assert out.exit_code == 0, out.output
    assert len(client.calls) == 2
    assert "not started, spend cap reached" in out.output
    spent = conn.execute("SELECT sum(cost_usd) FROM llm_spend").fetchone()[0]
    assert spent <= 0.90
