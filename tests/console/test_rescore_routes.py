"""/prefs Re-score now: panel, confirm, background run, progress, single-flight (fake)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Scoring, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile
from jobhunter.scoring.scorers import ScoreResult

NOW = datetime.now(UTC)
SPEC = "test:model"
QUOTE = "Write Terraform modules and Go tooling."
DESC = f"Own the Linux fleet.\n{QUOTE}"
PREFS = """\
resume_path: resume.md
target_titles: [Systems Engineer]
hard:
  states_allowed: all
  remote_ok: true
soft:
  state_ranking: [CO, WA]
narrative:
  want: Synthetic systems work.
"""
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
        "evidence": [{"claim": "tooling", "quote": QUOTE}],
        "blockers": [],
        "missing_info": [],
        "shape_flags": [],
        "tailoring_hints": [],
    }
)


class FakeScorer:
    name = SPEC

    def __init__(self, gate: threading.Event | None = None, fail: Exception | None = None):
        self.gate = gate
        self.fail = fail
        self.entered = threading.Event()

    def submit(self, requests):
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise self.fail
        return [ScoreResult(custom_id=r.custom_id, status="succeeded", text=BODY) for r in requests]

    def cost(self, usage, batch):
        return 0.0


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


@pytest.fixture
def db_path(tmp_path, pdir):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    fv = load_profile(pdir).filter_version
    for n in range(1, 4):
        c.execute(
            "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
            "location_scope, employment_type, stage, posted_at, first_seen_at, last_seen_at) "
            "VALUES (?, 'co', ?, 'https://example.com/j', ?, 'Acme', ?, 'single', 'full_time', "
            "'prefiltered', ?, ?, ?)",
            (n, str(n), f"Job {n}", DESC, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        c.execute(
            "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
            "VALUES (?, ?, 'exact_hash', 'x')",
            (n, n),
        )
        c.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
        c.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (n,))
        c.execute(
            "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
            "VALUES (?, 1, '[]', ?, 'x')",
            (n, fv),
        )
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


def make_client(pdir, db_path, **scoring) -> TestClient:
    scoring.setdefault("screen_scorer", SPEC)
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path), scoring=Scoring(**scoring))
    return TestClient(create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW))


@pytest.fixture
def client(pdir, db_path, monkeypatch):
    for var in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    c = make_client(pdir, db_path)
    c.app.state.rescore_scorer_factory = lambda spec: FakeScorer()
    return c


def finish(client):
    client.app.state.rescore.join()


def test_prefs_has_labeled_section_with_help_estimate_and_confirm(client):
    text = client.get("/prefs").text
    assert 'id="sec-rescore"' in text and "Re-score now" in text
    assert 'data-help="rescore"' in text and "spends credits" in text
    assert "<strong>3</strong> job group" in text  # waiting
    text = client.get("/prefs/rescore/panel", params={"scope": "all"}).text
    assert "estimated <strong>$0.01</strong>" in text
    assert 'hx-confirm="Re-score 3 job groups with test:model?' in text
    assert "This spends credits." in text


def test_scorer_list_names_keyed_scorers_without_revealing_keys(pdir, db_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = make_client(pdir, db_path).get("/prefs").text
    assert "jev:typesafe/jev-1.13" in text and "sk-or-secret-value" not in text
    assert "anthropic:claude-haiku-4-5" not in text  # no ANTHROPIC_API_KEY


def test_panel_fragment_follows_scope_and_scorer(client):
    text = client.get("/prefs/rescore/panel", params={"scope": "all"}).text
    assert '<option value="all" selected>' in text


def test_run_scores_in_background_and_progress_finishes(client, conn):
    r = client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    assert r.status_code == 200
    finish(client)
    row = conn.execute("SELECT * FROM rescore_request").fetchone()
    assert (row["status"], row["scored"], row["total"]) == ("done", 3, 3)
    done = client.get(f"/prefs/rescore/progress/{row['id']}").text
    assert "Done." in done and 'href="/inbox"' in done and "hx-trigger" not in done
    assert conn.execute("SELECT count(*) FROM fit_score").fetchone()[0] == 3


def test_progress_polls_while_running_then_stops(client, conn):
    gate = threading.Event()
    scorer = FakeScorer(gate=gate)
    client.app.state.rescore_scorer_factory = lambda spec: scorer
    r = client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    assert 'hx-trigger="every 1s"' in r.text  # the progress fragment polls itself
    assert scorer.entered.wait(10)
    rid = conn.execute("SELECT id FROM rescore_request").fetchone()[0]
    running = client.get(f"/prefs/rescore/progress/{rid}").text
    assert "Re-scoring" in running and "scored 0 of 3" in running and "hx-trigger" in running
    gate.set()
    finish(client)
    assert "hx-trigger" not in client.get(f"/prefs/rescore/progress/{rid}").text


def test_second_click_shows_the_running_one(client, conn):
    gate = threading.Event()
    scorer = FakeScorer(gate=gate)
    client.app.state.rescore_scorer_factory = lambda spec: scorer
    client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    assert scorer.entered.wait(10)
    second = client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    assert "already running" in second.text and "Re-scoring" in second.text
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 1
    gate.set()
    finish(client)


def test_failed_run_is_shown(client, conn):
    client.app.state.rescore_scorer_factory = lambda spec: FakeScorer(
        fail=RuntimeError("provider down")
    )
    client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    finish(client)
    rid = conn.execute("SELECT id FROM rescore_request").fetchone()[0]
    text = client.get(f"/prefs/rescore/progress/{rid}").text
    assert "Failed." in text and "provider down" in text


def test_over_cap_is_refused_without_a_request(pdir, db_path, conn):
    c = make_client(pdir, db_path, daily_cap_usd=0.001)
    c.app.state.rescore_scorer_factory = lambda spec: FakeScorer()
    r = c.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    assert "spend cap" in r.text
    assert conn.execute("SELECT count(*) FROM rescore_request").fetchone()[0] == 0


def test_queued_request_has_run_and_dismiss_with_local_time(client, conn):
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd) VALUES ('2026-10-10T01:21:00Z', 'all', 'v', 3, 0.002, "
        "0.006)"
    )
    text = client.get("/prefs").text
    assert "Queued re-scores" in text and "requested at " in text
    assert "2026-10-10T01:21" not in text  # shown as local time, not raw UTC
    assert 'hx-vals=\'{"request_id": "1"}\'' in text
    client.post("/prefs/rescore/run", data={"request_id": "1", "scorer": SPEC})
    finish(client)
    assert conn.execute("SELECT status, scored FROM rescore_request").fetchone()[:] == ("done", 3)


def test_dismiss_cancels_a_queued_request(client, conn):
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd) VALUES ('2026-10-10T01:21:00Z', 'all', 'v', 3, 0, 0)"
    )
    r = client.post("/prefs/rescore/1/cancel")
    assert "Queued re-scores" not in r.text
    assert conn.execute("SELECT status FROM rescore_request").fetchone()[0] == "canceled"


def test_stale_filter_version_is_reprefiltered_by_the_run(pdir, db_path, conn, client):
    """A /prefs edit changed filter_version too: prefilter_result is empty for the new one."""
    conn.execute("DELETE FROM prefilter_result")
    conn.execute("UPDATE job SET stage = 'grouped'")
    client.post("/prefs/rescore/run", data={"scope": "all", "scorer": SPEC})
    finish(client)
    row = conn.execute("SELECT * FROM rescore_request").fetchone()
    assert row["status"] == "done" and row["scored"] == 3
    assert "re-prefiltered 3 jobs" in row["note"]
