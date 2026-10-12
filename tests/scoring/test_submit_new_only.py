"""Plain ``score --submit`` scores new groups only; ``--scorer`` keeps per-scorer eligibility
(specs/020 M9). Fake client and scorer: no network, no credits."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_rescore import FakeScorer
from test_screen import FakeClient, add_group, profile  # noqa: F401
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.scoring import screen

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
runner = CliRunner()
NEW_SCREENER = "anthropic:claude-haiku-5-5"


@pytest.fixture
def env(tmp_path, monkeypatch, profile):  # noqa: F811
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'[scoring]\nscreen_scorer = "{NEW_SCREENER}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: profile)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for n in (1, 2):
        add_group(conn, n, profile)
    # Groups 1 and 2 were scored under the old model (FakeScorer); then the screen model changed.
    screen.score_sync(conn, FakeScorer(), profile, limit=50, now=NOW)
    conn.commit()
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 2
    add_group(conn, 3, profile)  # new since the model change
    conn.commit()
    client = FakeClient()
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: client)
    yield conn, client
    conn.close()


def submitted_ids(client) -> set[str]:
    return {r["custom_id"] for batch in client.messages.batches.created for r in batch}


def test_plain_submit_after_model_change_submits_only_new_groups(env):
    _, client = env
    out = runner.invoke(app, ["score", "--submit"])
    assert out.exit_code == 0, out.output
    assert submitted_ids(client) == {"g3"}
    assert "2 already scored; use a re-score to score them again" in out.output


def test_submit_with_scorer_keeps_per_scorer_eligibility(env):
    _, client = env
    out = runner.invoke(app, ["score", "--submit", "--scorer", NEW_SCREENER])
    assert out.exit_code == 0, out.output
    assert submitted_ids(client) == {"g1", "g2", "g3"}
    assert "already scored" not in out.output


def test_submit_help_mentions_new_only():
    out = runner.invoke(app, ["score", "--help"])
    assert "only new job groups" in " ".join(out.output.split())
