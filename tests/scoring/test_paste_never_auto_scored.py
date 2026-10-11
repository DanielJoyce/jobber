"""A pasted posting is scored only by Score this group now (specs/017), never automatically.

Scorers spend the user's credits, so this checks every automatic path: the daily run's score
stage, a re-score plan, and the prefilter (which must not overturn the user's choice). Also the
CLI ``score --group``: estimate first, confirm, refuse over the cap. Fake scorers only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_rescore import SPEC, FakeScorer
from test_screen import add_group, profile  # noqa: F401
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Scoring, Settings
from jobhunter.core import db
from jobhunter.pipeline import runner
from jobhunter.scoring import prefilter, rescore, screen

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
cli = CliRunner()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled'), "
        "('paste-manual', 'C', 'Pasted posting', 'manual', 'manual', 'console', 'manual'), "
        "('email-manual', 'C', 'Added from email', 'manual', 'manual', 'gmail', 'manual')"
    )
    yield c
    c.close()


def manual(conn, gid, source):
    conn.execute(
        "UPDATE job SET source_key = ? WHERE id = (SELECT canonical_job_id FROM job_group "
        "WHERE id = ?)",
        (source, gid),
    )
    # As paste.insert_pasted_posting and the email-manual creation do (specs/017 1e: the
    # group flag, not the source, is what every nightly site checks).
    conn.execute(
        "UPDATE job_group SET method = 'manual', score_on_request = 1 WHERE id = ?", (gid,)
    )


def eligible(conn, profile, **kw):  # noqa: F811
    return [
        r["group_id"] for r in screen.eligible_groups(conn, profile, scorer=SPEC, limit=50, **kw)
    ]


def test_manual_groups_are_eligible_only_when_named(conn, profile):  # noqa: F811
    ingested = add_group(conn, 1, profile)
    pasted = add_group(conn, 2, profile)
    emailed = add_group(conn, 3, profile)
    manual(conn, pasted, "paste-manual")
    manual(conn, emailed, "email-manual")
    # Even with a passing prefilter row (a failed Score now, a stale pass) they stay out.
    assert eligible(conn, profile) == [ingested]
    assert eligible(conn, profile, new_only=True) == [ingested]
    assert eligible(conn, profile, group_ids=[pasted]) == [pasted]


def test_rescore_plan_never_includes_a_pasted_group(conn, profile):  # noqa: F811
    ingested = add_group(conn, 1, profile)
    pasted = add_group(conn, 2, profile)
    manual(conn, pasted, "paste-manual")
    plan = rescore.make_plan(conn, profile, Scoring(screen_scorer=SPEC), "all", SPEC, NOW)
    assert plan.group_ids == [ingested]


def test_prefilter_never_overturns_a_user_requested_pass(conn, profile):  # noqa: F811
    pasted = add_group(conn, 1, profile, filter_version="old")
    manual(conn, pasted, "paste-manual")
    before = conn.execute("SELECT * FROM prefilter_result").fetchall()
    counts = prefilter.run_prefilter(conn, profile, now=NOW, force=True)
    assert counts["evaluated"] == 0
    assert [tuple(r) for r in conn.execute("SELECT * FROM prefilter_result")] == [
        tuple(r) for r in before
    ]


def test_daily_run_scores_ingested_groups_but_not_pasted_ones(conn, profile, monkeypatch):  # noqa: F811
    ingested = add_group(conn, 1, profile, passed=None)
    pasted = add_group(conn, 2, profile, passed=None)
    manual(conn, pasted, "paste-manual")
    conn.execute(
        "UPDATE job SET stage = 'grouped' WHERE job_group_id = ?", (ingested,)
    )  # waiting for prefilter, like a fresh ingest
    conn.execute("UPDATE job SET stage = 'normalized' WHERE job_group_id = ?", (pasted,))
    conn.commit()
    seen: list[str] = []

    class Recording(FakeScorer):
        def submit(self, requests):
            seen.extend(r.custom_id for r in requests)
            return super().submit(requests)

    monkeypatch.setattr(
        "jobhunter.scoring.scorers.scorer_from_string", lambda spec, scoring=None: Recording()
    )
    settings = Settings.model_validate({"scoring": {"screen_scorer": SPEC}})
    runner.run_pipeline(
        conn,
        settings,
        [],
        stages=["normalize", "dedupe", "prefilter", "score"],
        profile_loader=lambda: profile,
        now=NOW,
    )
    assert seen == [f"g{ingested}"]
    job = conn.execute("SELECT stage FROM job WHERE job_group_id = ?", (pasted,)).fetchone()
    assert job["stage"] == "normalized"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM prefilter_result p JOIN job j ON j.id = p.job_id "
            "WHERE j.job_group_id = ?",
            (pasted,),
        ).fetchone()[0]
        == 0
    )


# ─── CLI score --group ─────────────────────────────────────────────────────


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
    monkeypatch.setattr(
        "jobhunter.apply.score.scorer_from_string", lambda spec, scoring=None: scorer
    )
    c = db.connect(db_path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled'), "
        "('paste-manual', 'C', 'Pasted posting', 'manual', 'manual', 'console', 'manual')"
    )
    gid = add_group(c, 1, profile, passed=None)
    manual(c, gid, "paste-manual")
    c.execute("UPDATE job SET stage = 'normalized' WHERE job_group_id = ?", (gid,))
    c.commit()
    yield c, scorer, gid, cfg
    c.close()


def test_score_group_shows_estimate_asks_and_scores(env):
    c, scorer, gid, _ = env
    out = cli.invoke(app, ["score", "--group", str(gid)], input="y\n")
    assert out.exit_code == 0, out.output
    assert "estimated $" in out.output and "Score it? This spends credits." in out.output
    assert "scored:" in out.output and scorer.calls == 1
    reasons = c.execute("SELECT reasons FROM prefilter_result").fetchone()[0]
    assert json.loads(reasons) == ["user-requested"]
    assert c.execute("SELECT COUNT(*) FROM fit_score").fetchone()[0] == 1


def test_score_group_declined_runs_nothing(env):
    c, scorer, gid, _ = env
    out = cli.invoke(app, ["score", "--group", str(gid)], input="n\n")
    assert out.exit_code == 1 and "not run" in out.output
    assert scorer.calls == 0
    assert c.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 0


def test_score_group_over_cap_is_refused(env):
    c, scorer, gid, cfg = env
    cfg.write_text(cfg.read_text() + "daily_cap_usd = 0.0\n")
    out = cli.invoke(app, ["score", "--group", str(gid), "--yes"])
    assert out.exit_code == 1 and "spend cap" in out.output
    assert scorer.calls == 0
    assert c.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 0


def test_score_group_rejects_other_modes(env):
    _, _, gid, _ = env
    assert cli.invoke(app, ["score", "--group", str(gid), "--submit"]).exit_code == 2


# ─── concurrent confirms ───────────────────────────────────────────────────


def test_two_concurrent_score_now_confirms_pay_once(tmp_path, profile):  # noqa: F811
    import threading
    import time

    from jobhunter.apply import score as group_score

    path = tmp_path / "race.db"
    c = db.connect(path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled'), "
        "('paste-manual', 'C', 'Pasted posting', 'manual', 'manual', 'console', 'manual')"
    )
    gid = add_group(c, 1, profile, passed=None)
    manual(c, gid, "paste-manual")
    c.execute("UPDATE job SET stage = 'normalized' WHERE job_group_id = ?", (gid,))
    scoring = Scoring(screen_scorer=SPEC)
    token = group_score.estimate(c, profile, scoring, gid, NOW).token
    calls: list[int] = []

    class Slow(FakeScorer):
        def submit(self, requests):
            calls.append(1)
            time.sleep(0.5)
            return super().submit(requests)

    outcomes: list[str] = []
    start = threading.Barrier(2)

    def press() -> None:
        conn = db.connect(path)
        try:
            start.wait()
            out = group_score.score_now(
                conn, profile, scoring, gid, token=token, now=NOW,
                scorer_factory=lambda spec: Slow(),
            )  # fmt: skip
            outcomes.append(out.status)
        except group_score.ScoreRefused as exc:
            outcomes.append(f"refused: {exc}")
        finally:
            conn.close()

    threads = [threading.Thread(target=press) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(calls) == 1, outcomes
    assert sorted(o.split(":")[0] for o in outcomes) == ["refused", "scored"], outcomes
    assert c.execute("SELECT COUNT(*) FROM fit_score").fetchone()[0] == 1
    reasons = c.execute("SELECT reasons FROM prefilter_result").fetchone()[0]
    assert json.loads(reasons) == ["user-requested"]  # the winner's row survives
    c.close()


def _pasted_db(tmp_path, profile):  # noqa: F811
    c = db.connect(tmp_path / "claim.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled'), "
        "('paste-manual', 'C', 'Pasted posting', 'manual', 'manual', 'console', 'manual')"
    )
    gid = add_group(c, 1, profile, passed=None)
    manual(c, gid, "paste-manual")
    c.execute("UPDATE job SET stage = 'normalized' WHERE job_group_id = ?", (gid,))
    return c, gid


def test_interrupted_score_now_releases_its_claim(tmp_path, profile):  # noqa: F811
    from jobhunter.apply import score as group_score

    c, gid = _pasted_db(tmp_path, profile)
    scoring = Scoring(screen_scorer=SPEC)

    class CtrlC(FakeScorer):
        def submit(self, requests):
            raise KeyboardInterrupt

    token = group_score.estimate(c, profile, scoring, gid, NOW).token
    with pytest.raises(KeyboardInterrupt):
        group_score.score_now(
            c, profile, scoring, gid, token=token, now=NOW, scorer_factory=lambda s: CtrlC()
        )
    assert c.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 0
    est = group_score.estimate(c, profile, scoring, gid, NOW)
    assert est.refusal is None
    out = group_score.score_now(
        c, profile, scoring, gid, token=est.token, now=NOW, scorer_factory=lambda s: FakeScorer()
    )
    assert out.status == "scored"
    c.close()


def test_a_killed_run_says_when_to_retry_and_expires(tmp_path, profile):  # noqa: F811
    from datetime import timedelta

    from jobhunter.apply import score as group_score

    c, gid = _pasted_db(tmp_path, profile)
    scoring = Scoring(screen_scorer=SPEC)
    job_id = c.execute("SELECT id FROM job WHERE job_group_id = ?", (gid,)).fetchone()[0]
    # What a process killed mid-call leaves behind.
    c.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[\"user-requested\"]', ?, ?)",
        (job_id, profile.filter_version, NOW.isoformat()),
    )
    est = group_score.estimate(c, profile, scoring, gid, NOW + timedelta(minutes=5))
    retry = (NOW + group_score.CLAIM_TTL).astimezone()
    assert est.refusal and f"retry after {retry:%H:%M}" in est.refusal
    later = NOW + group_score.CLAIM_TTL + timedelta(minutes=1)
    assert group_score.estimate(c, profile, scoring, gid, later).refusal is None
    c.close()


def test_score_group_claim_starts_after_the_confirm_prompt(env, monkeypatch):
    """30610d5 (3): the claim is stamped when the user confirms, not when the estimate was
    shown, so a long wait at the prompt does not shorten the claim window."""
    import jobhunter.cli as cli_mod

    c, scorer, gid, _ = env
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    def confirm(*a, **k):
        clock[0] = NOW.replace(hour=13)  # an hour at the prompt
        return True

    monkeypatch.setattr(cli_mod, "datetime", Clock)
    monkeypatch.setattr(cli_mod.typer, "confirm", confirm)
    out = cli.invoke(app, ["score", "--group", str(gid)])
    assert out.exit_code == 0, out.output
    assert scorer.calls == 1
    claimed = c.execute("SELECT evaluated_at FROM prefilter_result").fetchone()[0]
    assert datetime.fromisoformat(claimed) == NOW.replace(hour=13)
