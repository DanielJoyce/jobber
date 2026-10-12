"""An invalid models.toml blocks every path that spends on that use (specs/020 M2, fail closed).

Each test writes a models.toml whose ``screen_scorer`` (or ``deep_scorer``) is invalid over a
config.toml whose value is valid, then drives one spending path. None may fall back to the
config.toml value or the default, and none may construct a scorer or an Anthropic client: the
factories are patched to fail the test. No network, no credits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_rescore import SPEC, FakeScorer, priced_test_model, queue_raw  # noqa: F401
from test_screen import add_group, profile  # noqa: F401
from typer.testing import CliRunner

from jobhunter.apply import score as group_score
from jobhunter.cli import app
from jobhunter.config import load_settings
from jobhunter.core import db
from jobhunter.scoring import rescore

cli = CliRunner()
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
BAD_SCREEN = '[scoring]\nscreen_scorer = "anthropic:claude-opus-5-5"\n'  # thinking always on
BAD_DEEP = '[scoring]\ndeep_scorer = "openrouter:openai/gpt-oss-120b"\n'  # deep is Anthropic only
MESSAGE = "models.toml screen_scorer is invalid"


class Spent(AssertionError):
    pass


def _fail(*a, **k):
    raise Spent("a scorer or client was constructed on a blocked use")


@pytest.fixture
def env(tmp_path, monkeypatch, profile):  # noqa: F811
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "profile").mkdir()  # the nightly run's loader looks for the folder
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{data}"\ndb_path = "{db_path}"\n'
        f'profile_dir = "{tmp_path / "profile"}"\n'
        f'[scoring]\nscreen_scorer = "{SPEC}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.setattr("jobhunter.scoring.profile.load_profile", lambda *a, **k: profile)
    for target in (
        "anthropic.Anthropic",
        "jobhunter.scoring.scorers.scorer_from_string",
        "jobhunter.scoring.rescore.scorer_from_string",
    ):
        monkeypatch.setattr(target, _fail)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for n in range(1, 4):
        add_group(conn, n, profile)
    conn.commit()
    yield SimpleNamespace(conn=conn, models=data / "models.toml", profile=profile)
    conn.close()


def spent(conn) -> int:
    return (
        conn.execute("SELECT count(*) FROM fit_score").fetchone()[0]
        + conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0]
    )


def test_the_valid_config_value_would_otherwise_run(env):
    """Control: without models.toml the configured scorer has no problem."""
    assert load_settings().scoring.scorer_problems() == {}
    env.models.write_text(BAD_SCREEN)
    settings = load_settings()
    assert settings.scoring.screen_scorer != SPEC
    assert MESSAGE in settings.scoring.scorer_problems()["screen_scorer"]


def test_nightly_run_score_stage_skips_with_the_message(env):
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["run", "--stage", "score"])
    assert f"score: skipped ({MESSAGE}" in out.output, out.output
    assert "survivors stay queued" in out.output
    assert spent(env.conn) == 0


def test_score_submit_is_blocked(env):
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["score", "--submit"])
    assert out.exit_code == 2 and MESSAGE in out.output
    assert spent(env.conn) == 0


def test_an_explicit_scorer_flag_beats_the_block(env, monkeypatch):
    """--scorer is a per-command choice, above models.toml in precedence."""
    env.models.write_text(BAD_SCREEN)
    calls = []
    monkeypatch.setattr(
        "jobhunter.scoring.screen.score_sync",
        lambda *a, **k: calls.append(k) or SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "jobhunter.scoring.scorers.scorer_from_string", lambda spec, scoring=None: object()
    )
    out = cli.invoke(app, ["score", "--submit", "--scorer", SPEC])
    assert out.exit_code == 0, out.output
    assert calls and MESSAGE not in out.output


@pytest.mark.parametrize("args", [["--deep", "3"], ["--deep-group", "1"]])
def test_deep_is_blocked(env, args):
    env.models.write_text(BAD_DEEP)
    out = cli.invoke(app, ["score", *args])
    assert out.exit_code == 2
    assert "models.toml deep_scorer is invalid" in out.output
    assert spent(env.conn) == 0


def test_rescore_scope_is_blocked(env):
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["score", "--rescore", "all", "--yes"])
    assert out.exit_code == 2 and MESSAGE in out.output
    assert spent(env.conn) == 0


def test_score_group_is_blocked(env):
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["score", "--group", "1", "--yes"])
    assert out.exit_code != 0 and MESSAGE in out.output
    assert spent(env.conn) == 0


def test_drain_skips_requests_that_follow_the_blocked_model(env):
    rid = queue_raw(env.conn, env.profile)
    env.conn.commit()
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["score", "--rescore-pending"])
    assert f"request {rid}: skipped: {MESSAGE}" in out.output, out.output
    assert "stays queued" in out.output
    assert rescore.get_request(env.conn, rid)["status"] == "pending"
    assert spent(env.conn) == 0


def test_drain_runs_a_request_kept_on_its_own_model(env, monkeypatch):
    """ "Keep queued re-scores on the old model" writes the spec on the row: it still runs."""
    from jobhunter import model_choices as mc

    rid = queue_raw(env.conn, env.profile)
    env.conn.commit()
    assert mc.keep_queued(env.conn, SPEC) == 1
    env.models.write_text(BAD_SCREEN)
    scorer = FakeScorer()
    monkeypatch.setattr(rescore, "scorer_from_string", lambda spec, scoring=None: scorer)
    out = cli.invoke(app, ["score", "--rescore-pending"])
    assert out.exit_code == 0, out.output
    assert scorer.calls == 1
    assert rescore.get_request(env.conn, rid)["status"] == "done"


def test_backfill_is_blocked(env):
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["backfill", "--days", "3", "--dry-run"])
    assert out.exit_code == 2 and MESSAGE in out.output


def test_llm_bench_without_scorer_is_blocked(env):
    env.models.write_text(BAD_SCREEN)
    out = cli.invoke(app, ["llm", "bench"])
    assert out.exit_code == 2 and MESSAGE in out.output


def test_score_now_is_refused(env):
    env.models.write_text(BAD_SCREEN)
    scoring = load_settings().scoring
    est = group_score.estimate(env.conn, env.profile, scoring, 1, NOW)
    assert est.refusal and MESSAGE in est.refusal
    with pytest.raises(group_score.ScoreRefused, match=r"models\.toml"):
        group_score.score_now(
            env.conn,
            env.profile,
            scoring,
            1,
            token=est.token,
            now=NOW,
            scorer_factory=_fail,
            client_factory=_fail,
        )
    assert spent(env.conn) == 0


def test_score_collect_is_not_blocked(env, monkeypatch):
    """Collecting spends nothing new and uses the model stored on the batch."""
    env.models.write_text(BAD_SCREEN)
    built = []
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: built.append(1) or object())
    seen = []

    def collect_batch(conn, client, batch_id, prof, now):
        seen.append(batch_id)
        return SimpleNamespace(as_dict=lambda: {"status": "ok"})

    monkeypatch.setattr("jobhunter.scoring.screen.collect_batch", collect_batch)
    out = cli.invoke(app, ["score", "--collect", "msgbatch_1"])
    assert out.exit_code == 0, out.output
    assert seen == ["msgbatch_1"] and built == [1]


def test_apply_draft_on_the_host_sees_models_toml(env):
    """`apply draft` reads models.toml too: an invalid drafting key prints the fallback notice."""
    env.models.write_text('[apply]\ncli_model = "haiku"\n')
    out = cli.invoke(app, ["apply", "draft", "999"])
    assert "notice: models.toml's apply.cli_model is invalid; using opus" in out.output


def test_the_console_command_gets_models_toml(env, monkeypatch):
    """`jobhunter console` (host or container) builds its app from load_settings."""
    env.models.write_text('[scoring]\nscreen_scorer = "local:qwen3"\n')
    seen = {}

    def fake_create_app(settings, *a, **k):
        seen["settings"] = settings
        raise SystemExit(0)

    monkeypatch.setattr("jobhunter.console.app.create_app", fake_create_app)
    cli.invoke(app, ["console"])
    assert seen["settings"].scoring.screen_scorer == "local:qwen3"


# ─── Queued re-scores (specs/020#queued-re-scores) ──────────────────────────


def _queue(conn, profile, scope: str, scorer: str | None = None) -> int:  # noqa: F811
    rid = queue_raw(conn, profile)
    conn.execute(
        "UPDATE rescore_request SET scope = ?, scorer = ? WHERE id = ?", (scope, scorer, rid)
    )
    conn.commit()
    return rid


def test_queued_line_prices_the_widest_scope_once(env):
    from jobhunter import model_choices as mc

    first = _queue(env.conn, env.profile, "recent")
    second = _queue(env.conn, env.profile, "all")
    _queue(env.conn, env.profile, "all", scorer=SPEC)  # carries its own scorer: not affected
    scoring = load_settings().scoring
    impact = mc.queued_impact(env.conn, env.profile, scoring, SPEC, "local:qwen3", NOW)
    assert impact is not None
    assert impact.request_ids == [first, second] and impact.scope == "all"
    assert impact.superseded == [second]
    assert impact.new_usd is not None  # priced as the drain would plan it
    assert impact.old_usd is not None and impact.old_usd > 0
    assert impact.line.startswith("2 queued re-scores would run tonight with local:qwen3: ")
    assert f"instead of about ${impact.old_usd:.2f} with {SPEC}" in impact.line
    assert spent(env.conn) == 0


def test_keep_writes_the_old_spec_into_unplanned_rows_only(env):
    from jobhunter import model_choices as mc

    a = _queue(env.conn, env.profile, "all")
    b = _queue(env.conn, env.profile, "all", scorer="local:other")
    assert mc.keep_queued(env.conn, SPEC) == 1
    assert rescore.get_request(env.conn, a)["scorer"] == SPEC
    assert rescore.get_request(env.conn, b)["scorer"] == "local:other"


def test_models_set_screen_asks_about_queued_rescores(env):
    rid = _queue(env.conn, env.profile, "all")
    out = cli.invoke(app, ["models", "set", "screen", "local:qwen3"])
    assert out.exit_code == 2 and "--queued" in out.output
    assert not env.models.exists()
    out = cli.invoke(app, ["models", "set", "screen", "local:qwen3", "--queued", "keep"])
    assert out.exit_code == 0, out.output
    assert "1 queued re-score would run tonight with local:qwen3" in out.output
    assert rescore.get_request(env.conn, rid)["scorer"] == SPEC
    assert load_settings().scoring.screen_scorer == "local:qwen3"
    row = env.conn.execute("SELECT * FROM model_change").fetchone()
    assert (row["field"], row["old_value"], row["new_value"], row["source"]) == (
        "scoring.screen_scorer",
        None,
        "local:qwen3",
        "cli",
    )


def test_models_reset_screen_with_new_leaves_rows_following(env):
    rid = _queue(env.conn, env.profile, "all")
    env.models.write_text('[scoring]\nscreen_scorer = "local:qwen3"\n')
    out = cli.invoke(app, ["models", "reset", "screen", "--queued", "new"])
    assert out.exit_code == 0, out.output
    assert f"with {SPEC}" in out.output  # the reset brings back config.toml's scorer
    assert rescore.get_request(env.conn, rid)["scorer"] is None
    assert load_settings().scoring.screen_scorer == SPEC
