"""/sources, /rejected, /search and /costs on a seeded tmp DB. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Scoring, Settings
from jobhunter.console import pages
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.core.models import SourceRow
from jobhunter.scoring.profile import load_profile
from jobhunter.sources.registry import sync_sources_table

PREFS = """\
hard:
  states_allowed: [CO, WA]
  remote_ok: true
soft:
  state_ranking: [CO, WA]
resume_path: resume.md
"""
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
TS = NOW.isoformat()


def reg(key, state, policy="enabled", family="x", robots="allow", min_jobs=5):
    return SourceRow.model_validate(
        {
            "key": key,
            "state": state,
            "class": "A",
            "name": f"Source {key}",
            "family": family,
            "tier": "http",
            "entry": "https://example.com/",
            "policy": policy,
            "robots": {"status": robots, "checked": "2026-10-09"},
            "expect": {"min_jobs_per_week": min_jobs},
        }
    )


REGISTRY = [
    reg("co-ok", "CO"),
    reg("az-blocked", "AZ", policy="blocked", robots="disallow_all", min_jobs=0),
    reg("wa-broken", "WA"),
    reg("us-nlx", None, family="nlx"),
]


def dims(skills=90):
    return {
        "skills": {"score": skills, "why": "w"},
        "seniority": {"score": 90, "why": "w"},
        "domain": {"score": 90, "why": "w"},
        "raw_skills": skills,
        "recency_weighted_skills": skills,
        "seniority_direction": "match",
    }


@pytest.fixture
def profile_dir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n", encoding="utf-8")
    return d


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    sync_sources_table(c, REGISTRY)
    yield c
    c.close()


def add_job(conn, n, title, *, source="co-ok", desc="", state="CO", salary=None, posted_days=2):
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "salary_min, salary_max, salary_period, salary_stated, location_scope, posted_at, "
        "needs_resolve, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, 'https://example.com/j', ?, 'Acme', ?, ?, ?, 'year', ?, 'single', ?, "
        "?, ?, ?)",
        (
            n,
            source,
            str(n),
            title,
            desc,
            salary,
            salary,
            1 if salary else 0,
            (NOW - timedelta(days=posted_days)).isoformat(),
            0 if desc else 1,
            TS,
            TS,
        ),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, TS),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, ?, 'Town', 1)",
        (n, state),
    )


def add_score(conn, n, skills, *, inp=None, cache=None, batch=None, created=TS):
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, input_tokens, cache_read_tokens, "
        "batch_id, created_at) VALUES (?, 'screen', 'm', 'p', 's', 'strong', 0, ?, '[]', '[]', "
        "?, ?, ?, ?)",
        (n, json.dumps(dims(skills)), inp, cache, batch, created),
    )


@pytest.fixture
def client(tmp_path, profile_dir, conn):
    settings = Settings(
        paths=Paths(profile_dir=profile_dir, db_path=tmp_path / "t.db"),
        scoring=Scoring(daily_cap_usd=2.0, weekly_cap_usd=10.0),
    )
    app = create_app(settings, lambda: db.connect(tmp_path / "t.db"), clock=lambda: NOW)
    app.state.registry_loader = lambda: REGISTRY
    return TestClient(app)


# ─── sources ────────────────────────────────────────────────────────────────


def test_sources_ordering_blocked_and_nlx(client, conn):
    conn.execute(
        "UPDATE source SET status='broken', status_note='HTTP 500 x3' WHERE key='wa-broken'"
    )
    conn.execute(
        "INSERT INTO source_state (source_key, last_ok_at, jobs_last_7d) VALUES ('co-ok', ?, 2)",
        (TS,),
    )
    r = client.get("/sources")
    assert r.status_code == 200
    t = r.text
    assert t.index("wa-broken") < t.index("az-blocked") < t.index("co-ok")
    assert "HTTP 500 x3" in t
    assert "blocked: robots.txt disallows generic agents (checked 2026-10-09)" in t
    assert "covered via national NLx" in t
    assert "2 / 5" in t and "below floor" in t
    assert "Recent runs" in t


def test_blocked_without_nlx_needs_decision(client, conn):
    conn.execute("UPDATE source SET status='broken' WHERE key='us-nlx'")
    t = client.get("/sources").text
    assert "needs your decision" in t
    assert "covered via national NLx" not in t


def test_resolve_backlog_and_error_rate(client, conn):
    add_job(conn, 1, "No desc a")
    add_job(conn, 2, "No desc b")
    add_job(conn, 3, "Has desc", desc="full text")
    for i, status in enumerate(["ok", "timeout", "ok"], start=1):
        conn.execute("INSERT INTO run (id, started_at, stages) VALUES (?, ?, 'list')", (i, TS))
        conn.execute(
            "INSERT INTO run_source (run_id, source_key, status) VALUES (?, 'co-ok', ?)",
            (i, status),
        )
    rows = {s.key: s for s in pages.source_health(conn, REGISTRY)}
    assert rows["co-ok"].backlog == 2
    assert rows["co-ok"].error_rate == "1/3"
    assert rows["wa-broken"].error_rate == "no runs"


def test_recent_runs_table(client, conn):
    conn.execute(
        "INSERT INTO run (id, started_at, ended_at, stages, exit_status, counts) VALUES "
        "(7, '2026-10-09T10:00:00+00:00', '2026-10-09T10:02:05+00:00', 'list', 'degraded', ?)",
        (json.dumps({"list": {"stubs": 4}}),),
    )
    t = client.get("/sources").text
    assert "2m05s" in t and "degraded" in t and "stubs 4" in t


# ─── rejected ───────────────────────────────────────────────────────────────


@pytest.fixture
def rejected_seed(conn, profile_dir):
    version = load_profile(profile_dir).filter_version
    add_job(conn, 1, "Salary low", salary=40000)
    add_job(conn, 2, "Wrong state", state="TX")
    add_job(conn, 3, "Both", state="TX")
    add_job(conn, 4, "Passed")
    for jid, passed, reasons, ver in [
        (1, 0, ["salary_floor"], version),
        (2, 0, ["state"], version),
        (3, 0, ["state", "salary_floor"], version),
        (4, 1, [], version),
    ]:
        conn.execute(
            "INSERT INTO prefilter_result VALUES (?, ?, ?, ?, ?)",
            (jid, passed, json.dumps(reasons), ver, TS),
        )
    return conn


def test_rejected_counts_and_filter(client, rejected_seed):
    t = client.get("/rejected").text
    assert "salary_floor 2" in t and "state 2" in t
    assert "Salary low" in t and "Wrong state" in t and "Passed" not in t
    assert 'href="/job/1"' in t
    f = client.get("/rejected?reason=state").text
    assert "Wrong state" in f and "Both" in f and "Salary low" not in f
    assert "2 rejected jobs" in f


def test_rejected_ignores_old_filter_version(client, rejected_seed):
    rejected_seed.execute("UPDATE prefilter_result SET filter_version = 'stale'")
    assert "No rejections" in client.get("/rejected").text


# ─── search ─────────────────────────────────────────────────────────────────


@pytest.fixture
def search_seed(conn):
    add_job(conn, 1, "Network Engineer", desc="manage kubernetes clusters", salary=120000)
    add_job(conn, 2, "Systems Admin", desc="kubernetes and linux", state="WA", posted_days=60)
    add_job(conn, 3, "Cook", desc="kitchen work", state="CO")
    add_score(conn, 1, 90)
    add_score(conn, 2, 20)
    conn.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (2, 'not_interesting', ?)",
        (TS,),
    )
    return conn


def test_search_by_description_term(client, search_seed):
    t = client.get("/search", params={"q": "kubernetes"}).text
    assert "Network Engineer" in t and "Systems Admin" in t and "Cook" not in t
    assert 'href="/job/1"' in t
    assert "dismissed" in t  # job 2 carries the badge


@pytest.mark.parametrize("q", ['"foo AND (', "NEAR/", "NEAR(", "a OR", '"', "*", "title:"])
def test_search_survives_hostile_input(client, search_seed, q):
    r = client.get("/search", params={"q": q})
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text


def test_fts_query_quotes_terms():
    assert pages.fts_query('"foo AND (') == '"foo" "AND"'
    assert pages.fts_query("( )") is None


def test_search_filters(client, search_seed):
    def titles(**p):
        t = client.get("/search", params={"q": "kubernetes", **p}).text
        return {x for x in ("Network Engineer", "Systems Admin") if x in t}

    assert titles(state="WA") == {"Systems Admin"}
    assert titles(salary_floor="100000") == {"Network Engineer"}
    assert titles(posted_after="2026-09-01") == {"Network Engineer"}
    assert titles(bucket_from="A", bucket_to="B") == {"Network Engineer"}
    assert titles(bucket_from="G", bucket_to="G") == {"Systems Admin"}
    assert titles(source_class="C") == set()


def test_search_empty_and_no_matches(client, search_seed):
    assert "Searches every job" in client.get("/search").text
    assert "0 results" in client.get("/search", params={"q": "zzzz"}).text


# ─── costs ──────────────────────────────────────────────────────────────────


def spend(conn, day, model, tier, cost, inp=1000, out=100):
    conn.execute(
        "INSERT INTO llm_spend VALUES (?, ?, ?, 3, ?, ?, ?)", (day, model, tier, inp, out, cost)
    )


def test_costs_cache_rate_and_caps(client, conn):
    spend(conn, "2026-10-09", "claude-haiku-4-5", "screen", 2.5)
    spend(conn, "2026-10-05", "claude-opus-5", "deep", 1.5)
    add_job(conn, 1, "A")
    add_job(conn, 2, "B")
    add_score(conn, 1, 90, inp=1000, cache=3000, batch="b1")
    add_score(conn, 2, 90, inp=1000, cache=3000, batch="b1")
    conn.execute(
        "INSERT INTO label (job_group_id, label, labeled_at) VALUES (1, 'interesting', ?)", (TS,)
    )
    t = client.get("/costs").text
    assert "75.0%" in t
    assert "Warning: no cache hits" not in t
    assert "$2.50" in t and "of $2.00 cap" in t and "over cap" in t
    assert "of $10.00 cap" in t
    assert "claude-haiku-4-5" in t and "claude-opus-5" in t
    assert "$4.00" in t  # total and cost per shortlisted job (1 shortlisted)
    assert "1 shortlisted" in t


def test_costs_zero_cache_warns(client, conn):
    add_job(conn, 1, "A")
    add_job(conn, 2, "B")
    add_score(conn, 1, 90, inp=1000, cache=0, batch="b9")
    add_score(conn, 2, 90, inp=1000, cache=0, batch="b9")
    t = client.get("/costs").text
    assert "0.0%" in t
    assert "Warning: no cache hits" in t and "b9" in t


def test_costs_empty_renders(client):
    t = client.get("/costs?days=7").text
    assert "n/a" in t and "No spend recorded" in t
    assert client.get("/costs?days=3").status_code == 200


def test_all_pages_render_without_profile(tmp_path, conn):
    settings = Settings(paths=Paths(profile_dir=tmp_path / "missing", db_path=tmp_path / "t.db"))
    app = create_app(settings, lambda: db.connect(tmp_path / "t.db"), clock=lambda: NOW)
    app.state.registry_loader = lambda: REGISTRY
    c = TestClient(app)
    for path in ("/sources", "/rejected", "/search?q=x", "/costs"):
        assert c.get(path).status_code == 200
