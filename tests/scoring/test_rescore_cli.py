"""jobhunter score --rescore / --rescore-pending with a fake scorer (no network, no credits)."""

from __future__ import annotations

import pytest
from test_rescore import SPEC, FakeScorer, queue
from test_screen import add_group, profile  # noqa: F401
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.core import db
from jobhunter.scoring import rescore

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch, profile):  # noqa: F811
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{tmp_path / "profile"}"\n'
        f'[scoring]\nscreen_scorer = "{SPEC}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: profile)
    scorer = FakeScorer()
    monkeypatch.setattr(rescore, "scorer_from_string", lambda spec, scoring=None: scorer)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for n in range(1, 4):
        add_group(conn, n, profile)
    yield conn, scorer
    conn.close()


def scored(conn) -> int:
    return conn.execute("SELECT count(*) FROM fit_score").fetchone()[0]


def test_rescore_shows_estimate_and_runs_on_yes(env):
    conn, _ = env
    out = runner.invoke(app, ["score", "--rescore", "all"], input="y\n")
    assert out.exit_code == 0, out.output
    assert "estimated $0.01" in out.output and "Run it?" in out.output
    assert "done: scored 3 of 3" in out.output
    assert scored(conn) == 3


def test_rescore_declined_runs_nothing(env):
    conn, scorer = env
    out = runner.invoke(app, ["score", "--rescore", "all"], input="n\n")
    assert out.exit_code == 1 and "not run" in out.output
    assert scored(conn) == 0 and scorer.calls == 0


def test_rescore_yes_skips_prompt(env):
    conn, _ = env
    out = runner.invoke(app, ["score", "--rescore", "all", "--yes"])
    assert out.exit_code == 0, out.output
    assert "Run it?" not in out.output and scored(conn) == 3


def test_rescore_refused_over_cap(env, tmp_path):
    _, scorer = env
    (tmp_path / "config.toml").write_text(
        (tmp_path / "config.toml").read_text() + "daily_cap_usd = 0.001\n"
    )
    out = runner.invoke(app, ["score", "--rescore", "all", "--yes"])
    assert out.exit_code == 1 and "spend cap" in out.output
    assert scorer.calls == 0


def test_rescore_rejects_bad_scope_and_mixed_modes(env):
    assert runner.invoke(app, ["score", "--rescore", "week"]).exit_code == 2
    assert runner.invoke(app, ["score", "--rescore", "all", "--submit"]).exit_code == 2
    assert runner.invoke(app, ["score", "--yes", "--submit"]).exit_code == 2


def test_rescore_pending_drains_queue(env, profile):  # noqa: F811
    conn, _ = env
    rid = queue(conn, profile)
    out = runner.invoke(app, ["score", "--rescore-pending"])
    assert out.exit_code == 0, out.output
    assert f"request {rid}: done scored 3 of 3" in out.output
    assert rescore.get_request(conn, rid)["status"] == "done"


def test_rescore_pending_with_empty_queue(env):
    out = runner.invoke(app, ["score", "--rescore-pending"])
    assert out.exit_code == 0 and "no pending re-score requests" in out.output


def test_rescore_pending_failure_exits_nonzero(env, profile, monkeypatch):  # noqa: F811
    conn, _ = env
    rid = queue(conn, profile)
    bad = FakeScorer(fail=RuntimeError("provider down"))
    monkeypatch.setattr(rescore, "scorer_from_string", lambda spec, scoring=None: bad)
    out = runner.invoke(app, ["score", "--rescore-pending"])
    assert out.exit_code == 1 and "provider down" in out.output
    assert rescore.get_request(conn, rid)["status"] == "failed"
