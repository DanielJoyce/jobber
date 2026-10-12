"""The deep pass from the console (specs/020 M11): plan, confirm, gate, run, caps.

No network and no real client: every test injects a fake streaming client through
``app.state.deep_client_factory``, and the no-spend tests use a factory that fails the test.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Scoring, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db, runlock
from jobhunter.scoring import catalog, deep, deep_run
from jobhunter.scoring.profile import load_profile
from jobhunter.scoring.scorers import compute_cost

NOW = datetime.now(UTC)
# Opus 5.5 at 006's typical deep tokens (5,000 in, 3,000 cached, 1,500 out): the price table.
PER_JOB = compute_cost(catalog.DEEP_TYPICAL_USAGE, "anthropic:claude-opus-5-5", batch=False)
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.0.2.7", 50000)
GOOD = "Write Terraform modules and Go tooling"
DESC = f"Own the Linux fleet.\n{GOOD}."
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
REPORT = json.dumps(
    {
        "verdict": "strong",
        "dimensions": {
            "skills": {"score": 90, "why": "Daily work."},
            "seniority": {"score": 80, "why": "match: senior IC."},
            "domain": {"score": 80, "why": "Fleet."},
        },
        "raw_skills": 90,
        "recency_weighted_skills": 90,
        "stale_skills": [],
        "current_focus_overlap": 85,
        "done_with_hits": [],
        "evidence": [{"claim": "Tooling", "quote": GOOD}],
        "blockers": [],
        "missing_info": [],
        "shape_flags": [],
        "tailoring_hints": [],
        "recommendation": "apply",
        "summary": "Good fit.",
        "requirement_gaps": [{"requirement": "Terraform", "status": "met", "evidence": GOOD}],
        "emphasize": ["Fleet work"],
        "questions_to_ask": [],
        "salary_read": "No pay stated.",
        "screen_disagreement": {"flag": False, "reason": ""},
    }
)


def message(*, output_tokens: int = 1500, model: str = "claude-opus-5-5", iterations=None):
    usage = SimpleNamespace(
        input_tokens=5000,
        output_tokens=output_tokens,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=0,
    )
    if iterations is not None:
        usage.iterations = iterations
    return SimpleNamespace(
        model=model,
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=REPORT)],
        usage=usage,
    )


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    """A streaming Anthropic stand-in. ``gate`` holds every call until it is set."""

    def __init__(self, make=message, gate: threading.Event | None = None):
        self.make = make
        self.gate = gate
        self.calls: list[dict[str, Any]] = []
        self.entered = threading.Event()
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.calls.append(params)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        return FakeStream(self.make())


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
    sv = load_profile(pdir).scoring_version
    for n in range(1, 4):
        c.execute(
            "INSERT INTO job (id, source_key, external_id, url, title, employer, "
            "description_text, location_scope, employment_type, stage, first_seen_at, "
            "last_seen_at) VALUES (?, 'co', ?, 'https://example.com/j', ?, 'Acme', ?, "
            "'single', 'full_time', 'scored', 'x', 'x')",
            (n, str(n), f"Systems Engineer {n}", DESC),
        )
        c.execute(
            "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
            "VALUES (?, ?, 'exact_hash', 'x')",
            (n, n),
        )
        c.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
        c.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (n,))
        skills = 95 - n * 5  # group 1 ranks first
        dims = {
            "skills": {"score": skills, "why": "x"},
            "seniority": {"score": 80, "why": "match: x"},
            "domain": {"score": 80, "why": "x"},
            "raw_skills": skills,
            "recency_weighted_skills": skills,
            "seniority_direction": "match",
        }
        c.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, blockers, created_at) VALUES "
            "(?, 'screen', 'anthropic:claude-haiku-5-5', 'p', ?, 'strong', 0, ?, '[]', '[]', 'x')",
            (n, sv, json.dumps(dims)),
        )
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def anthropic_key(monkeypatch):
    """A credential is present (so the plan is not refused); a real client is never built."""
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    def no_real_client(*a, **k):
        raise AssertionError("a real Anthropic client was built in a test")

    monkeypatch.setattr(anthropic, "Anthropic", no_real_client)


def make_client(pdir, db_path, *, peer=LOCAL, allow_remote=False, **scoring) -> TestClient:
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path), scoring=Scoring(**scoring))
    app = create_app(
        settings, lambda: db.connect(db_path), clock=lambda: NOW, allow_remote=allow_remote
    )
    return TestClient(app, client=peer)


class Factory:
    """Records every client it builds; ``forbid`` fails the test on any build."""

    def __init__(self, client: FakeClient | None = None, forbid: bool = False):
        self.client = client or FakeClient()
        self.forbid = forbid
        self.built = 0

    def __call__(self):
        if self.forbid:
            raise AssertionError("an Anthropic client was built without a confirmed run")
        self.built += 1
        return self.client


@pytest.fixture
def client(pdir, db_path):
    c = make_client(pdir, db_path)
    c.app.state.deep_client_factory = Factory(forbid=True)
    return c


def token_of(html: str) -> str:
    m = re.search(r'name="plan" value="([0-9a-f]+)"', html)
    assert m, "the panel shows no confirmable plan"
    return m.group(1)


def confirm(client, **params) -> Any:
    """What the browser does: render the panel (it shows the plan), then post its token."""
    html = client.get("/deep/panel", params=params).text
    data = {k: str(v) for k, v in params.items()}
    data["plan"] = token_of(html)
    return client.post("/deep/run", data=data)


def counts(conn) -> tuple[int, int]:
    return (
        conn.execute("SELECT count(*) FROM deep_request").fetchone()[0],
        conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0],
    )


# ─── No spend without the confirm ───────────────────────────────────────────


def test_rendering_every_place_plans_only_and_shows_the_estimate(client, conn):
    worst = deep.worst_case_usd()
    for params in (
        {},
        {"mode": "next", "n": "5"},
        {"mode": "rerun", "n": "20"},
        {"group": "2"},
    ):
        r = client.get("/deep/panel", params=params)
        assert r.status_code == 200
        assert "may use more money than screening" in r.text
        assert "runs only when you confirm" in r.text
    html = client.get("/deep/panel", params={"mode": "next", "n": "10"}).text
    # The estimate: 3 groups x the deep model's table price at 006's typical tokens.
    assert f"Estimate <strong>about ${3 * PER_JOB:.2f}</strong>" in html
    assert f"${worst:.2f}" in html  # worst case for one job
    assert "Deep-score 3 jobs, up to $" in html
    assert 'hx-confirm="Deep-score 3 jobs with Claude Opus 5.5? Estimated $' in html
    assert "This spends API credits" in html
    # The pages that host the panel load it only on click (or ?deep=1): plans only too.
    assert 'id="deep-open"' in client.get("/").text
    assert 'hx-get="/deep/panel?group=1"' in client.get("/job/1?deep=1").text
    assert counts(conn) == (0, 0)


def test_estimate_uses_the_price_table_per_job(client):
    html = client.get("/deep/panel", params={"group": "1"}).text
    assert f"about ${PER_JOB:.2f} a job (price table)" in html
    assert f"Estimate <strong>about ${PER_JOB:.2f}</strong>" in html


def test_missing_or_stale_token_runs_nothing(client, conn):
    r = client.post("/deep/run", data={"mode": "next", "n": "10"})
    assert r.status_code == 200 and "plan changed" in r.text
    r = client.post("/deep/run", data={"mode": "next", "n": "10", "plan": "0" * 16})
    assert "plan changed" in r.text
    # A token for another N is not this plan's.
    other = token_of(client.get("/deep/panel", params={"n": "5", "mode": "rerun"}).text)
    r = client.post("/deep/run", data={"mode": "next", "n": "10", "plan": other})
    assert "plan changed" in r.text and 'name="plan"' in r.text  # the new plan to confirm
    assert counts(conn) == (0, 0)


# ─── The gate ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("allow_remote", [False, True])
def test_remote_peer_gets_403_and_the_panel_gives_the_command(pdir, db_path, conn, allow_remote):
    c = make_client(pdir, db_path, peer=REMOTE, allow_remote=allow_remote)
    c.app.state.deep_client_factory = Factory(forbid=True)
    html = c.get("/deep/panel", params={"n": "10"}).text
    assert 'id="deep-run"' not in html
    assert "jobhunter score --deep 10" in html
    token = token_of(html)
    r = c.post("/deep/run", data={"mode": "next", "n": "10", "plan": token})
    assert r.status_code == 403
    assert c.post("/deep/1/cancel").status_code == 403
    assert counts(conn) == (0, 0)


# ─── Running it ─────────────────────────────────────────────────────────────


def test_confirmed_run_deep_scores_the_listed_groups_once(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    fake = Factory()
    c.app.state.deep_client_factory = fake
    r = confirm(c, mode="next", n="10")
    assert r.status_code == 200 and "Deep pass" in r.text
    c.app.state.deep.join()
    row = conn.execute("SELECT * FROM deep_request").fetchone()
    assert row["status"] == "done" and row["scored"] == 3 and row["done"] == 3
    assert json.loads(row["group_ids"]) == [1, 2, 3]
    assert len(fake.client.calls) == 3
    spent = conn.execute(
        "SELECT sum(cost_usd), sum(calls) FROM llm_spend WHERE tier = 'deep'"
    ).fetchone()
    assert spent[1] == 3 and spent[0] == pytest.approx(row["spent_usd"])
    assert row["spent_usd"] <= row["max_usd"]
    # Progress and the activity band show the result.
    assert "3 deep reports" in c.get(f"/deep/{row['id']}/progress").text
    assert "3 deep reports" in c.get("/activity").text
    # Nothing left to do: the next plan lists nothing, and the run lock was released.
    assert 'id="deep-run" disabled' in c.get("/deep/panel").text
    assert runlock.RunLock(db_path, "probe").try_acquire()


def test_one_group_from_the_job_page_actually_runs(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    fake = Factory()
    c.app.state.deep_client_factory = fake
    confirm(c, group="2")
    c.app.state.deep.join()
    assert len(fake.client.calls) == 1
    row = conn.execute("SELECT * FROM deep_request").fetchone()
    assert row["mode"] == "group" and row["scored"] == 1
    # The ceiling holds one worst-case call beyond the estimate's headroom.
    assert row["max_usd"] >= deep.worst_case_usd()


def test_double_submit_does_not_run_twice(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    gate = threading.Event()
    fake = Factory(FakeClient(gate=gate))
    c.app.state.deep_client_factory = fake
    params = {"mode": "next", "n": "10"}
    token = token_of(c.get("/deep/panel", params=params).text)
    data = {**params, "plan": token}
    first = c.post("/deep/run", data=data)
    assert first.status_code == 200
    assert fake.client.entered.wait(10)
    second = c.post("/deep/run", data=data)  # the double click, while the first runs
    assert "already running" in second.text
    assert "Deep pass 0/3" in c.get("/activity").text
    gate.set()
    c.app.state.deep.join()
    third = c.post("/deep/run", data=data)  # the same confirm again, after it finished
    assert "plan changed" in third.text
    assert conn.execute("SELECT count(*) FROM deep_request").fetchone()[0] == 1
    assert len(fake.client.calls) == 3
    assert fake.built == 1


def test_cancel_stops_after_the_call_in_flight(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    gate = threading.Event()
    fake = Factory(FakeClient(gate=gate))
    c.app.state.deep_client_factory = fake
    confirm(c, mode="next", n="10")
    assert fake.client.entered.wait(10)
    rid = conn.execute("SELECT id FROM deep_request").fetchone()[0]
    assert "stopping after the call in flight" in c.post(f"/deep/{rid}/cancel").text
    gate.set()
    c.app.state.deep.join()
    row = conn.execute("SELECT * FROM deep_request").fetchone()
    assert row["status"] == "canceled" and len(fake.client.calls) == 1


def test_run_lock_held_refuses_and_spends_nothing(client, conn, db_path, run_lock_holder):
    holder = run_lock_holder(db_path)
    r = confirm(client, mode="next", n="10")
    assert "nothing spent" in r.text and "Deep pass not started" in r.text
    assert counts(conn) == (0, 0)
    holder.release()
    holder.wait()


# ─── The cap and the reservation ────────────────────────────────────────────


def test_cap_below_one_worst_case_disables_and_refuses(pdir, db_path, conn):
    c = make_client(pdir, db_path, daily_cap_usd=0.50)
    c.app.state.deep_client_factory = Factory(forbid=True)
    html = c.get("/deep/panel", params={"n": "10"}).text
    assert 'id="deep-run" disabled' in html
    want = (
        "Your daily cap $0.50 is below one call&#39;s worst case $0.83; "
        "raise [scoring] daily_cap_usd to at least $0.83."
    )
    assert want in html and "Try again" not in html
    r = c.post("/deep/run", data={"mode": "next", "n": "10", "plan": token_of(html)})
    assert "Your daily cap $0.50 is below" in r.text
    assert counts(conn) == (0, 0)


def test_the_weekly_cap_is_named_when_it_binds(pdir, db_path, conn):
    conn.execute(
        "INSERT INTO llm_spend (day, model, tier, calls, cost_usd) VALUES (?, 'x', 'deep', 1, 9.5)",
        (NOW.date().isoformat(),),
    )
    c = make_client(pdir, db_path, daily_cap_usd=20.0)
    html = c.get("/deep/panel").text
    assert "Only $0.50 of your weekly cap ($10.00) is left" in html
    assert "raise [scoring] weekly_cap_usd" in html


def test_plan_says_how_many_fit_under_the_cap(pdir, db_path):
    # $0.83 worst case plus $0.05 a job leaves room for 2 calls
    c = make_client(pdir, db_path, daily_cap_usd=0.90)
    html = c.get("/deep/panel", params={"n": "10"}).text
    assert "Only 2 of 3 fit under the cap now" in html


def long_report():
    return message(output_tokens=deep.MAX_TOKENS)


def fallback_report():
    """A refused Opus 5.5 attempt, then Opus 4.8 answering at the full 32,000 tokens."""
    first = SimpleNamespace(
        model="claude-opus-5-5",
        input_tokens=5000,
        output_tokens=50,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=0,
    )
    second = SimpleNamespace(
        model="claude-opus-4-8",
        input_tokens=5000,
        output_tokens=deep.MAX_TOKENS - 50,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=0,
    )
    return message(
        output_tokens=deep.MAX_TOKENS - 50, model="claude-opus-4-8", iterations=[first, second]
    )


@pytest.mark.parametrize("make", [long_report, fallback_report])
def test_a_run_of_worst_case_calls_never_ends_above_the_cap(pdir, db_path, conn, make):
    cap = 1.50
    c = make_client(pdir, db_path, daily_cap_usd=cap)
    fake = Factory(FakeClient(make=make))
    c.app.state.deep_client_factory = fake
    confirm(c, mode="next", n="10")
    c.app.state.deep.join()
    row = conn.execute("SELECT * FROM deep_request").fetchone()
    spent = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM llm_spend").fetchone()[0]
    assert len(fake.client.calls) < 3  # it stopped before the cap, not after
    assert spent <= cap and row["spent_usd"] <= row["max_usd"]
    assert row["status"] == "done" and "stopped before the ceiling" in (row["note"] or "")


@pytest.mark.parametrize("make", [long_report, fallback_report])
def test_the_cap_admits_a_call_only_while_it_covers_the_worst_case(conn, pdir, make):
    """With a ceiling far above it, the scoring cap is what stops the run, never after."""
    cap = 1.00
    profile = load_profile(pdir)
    scoring = Scoring(daily_cap_usd=cap, weekly_cap_usd=10.0)
    plan = deep_run.make_plan(conn, profile, scoring, mode="next", n=10, now=NOW)
    plan.max_usd = 100.0
    rid = deep_run.create_request(conn, plan, NOW)
    fake = FakeClient(make=make)
    deep_run.run_request(conn, rid, profile, scoring, now=lambda: NOW, client_factory=lambda: fake)
    row = deep_run.get_request(conn, rid)
    spent = conn.execute("SELECT sum(cost_usd) FROM llm_spend").fetchone()[0]
    assert len(fake.calls) == 1 and spent <= cap
    assert "spend cap reached" in row["note"] and f"${deep.worst_case_usd():.2f}" in row["note"]


def test_the_ceiling_stops_a_run_before_its_worst_case(conn, pdir):
    """A run whose ceiling cannot cover one more worst case sends nothing more."""
    profile = load_profile(pdir)
    scoring = Scoring(daily_cap_usd=100.0, weekly_cap_usd=100.0)
    plan = deep_run.make_plan(conn, profile, scoring, mode="next", n=10, now=NOW)
    assert plan.total == 3 and not plan.refusal
    plan.max_usd = deep.worst_case_usd() - 0.01  # not enough for even one call
    rid = deep_run.create_request(conn, plan, NOW)
    fake = FakeClient()
    deep_run.run_request(conn, rid, profile, scoring, now=lambda: NOW, client_factory=lambda: fake)
    row = deep_run.get_request(conn, rid)
    assert fake.calls == [] and row["status"] == "done" and "ceiling" in row["note"]
    assert conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0


def test_a_failing_report_write_stops_the_run_and_its_call_counts(conn, pdir, monkeypatch):
    """Bug 6c33b40: the billed call is in llm_spend and in the run's spent_usd."""
    import sqlite3

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(deep, "_write", broken)
    profile = load_profile(pdir)
    scoring = Scoring(daily_cap_usd=100.0, weekly_cap_usd=100.0)
    plan = deep_run.make_plan(conn, profile, scoring, mode="next", n=10, now=NOW)
    rid = deep_run.create_request(conn, plan, NOW)
    fake = FakeClient()
    deep_run.run_request(conn, rid, profile, scoring, now=lambda: NOW, client_factory=lambda: fake)
    row = deep_run.get_request(conn, rid)
    spent = conn.execute("SELECT sum(calls), sum(cost_usd) FROM llm_spend").fetchone()
    assert len(fake.calls) == 1 and row["status"] == "failed"
    assert row["error"] == "DeepWriteError"
    assert spent[0] == 1 and spent[1] == pytest.approx(PER_JOB)
    assert row["spent_usd"] == pytest.approx(PER_JOB)


def test_an_api_error_stops_the_run_with_a_fixed_reason(pdir, db_path, conn):
    import anthropic
    import httpx

    class Broken(FakeClient):
        def _stream(self, **params):
            self.calls.append(params)
            raise anthropic.AuthenticationError(
                "secret detail",
                response=httpx.Response(401, request=httpx.Request("POST", "http://localhost")),
                body=None,
            )

    c = make_client(pdir, db_path)
    fake = Factory(Broken())
    c.app.state.deep_client_factory = fake
    confirm(c, mode="next", n="10")
    c.app.state.deep.join()
    row = conn.execute("SELECT * FROM deep_request").fetchone()
    assert row["status"] == "failed" and row["error"] == "API error: AuthenticationError"
    assert len(fake.client.calls) == 1
    assert "Deep pass failed: API error: AuthenticationError" in c.get("/activity").text


# ─── Review round 1 ─────────────────────────────────────────────────────────


def test_a_stale_running_request_is_marked_failed(client, conn, pdir):
    from datetime import timedelta

    from jobhunter.pipeline.listing import to_iso

    profile = load_profile(pdir)
    plan = deep_run.make_plan(conn, profile, Scoring(), mode="next", n=10, now=NOW)
    rid = deep_run.create_request(conn, plan, NOW - timedelta(minutes=20))
    conn.execute(
        "UPDATE deep_request SET heartbeat_at = ? WHERE id = ?",
        (to_iso(NOW - timedelta(minutes=16)), rid),
    )
    assert "Deep pass failed: interrupted" in client.get("/activity").text
    row = deep_run.get_request(conn, rid)
    assert row["status"] == "failed" and row["error"].startswith("interrupted")
    # The panel offers a plan again instead of showing a run in progress.
    assert 'id="deep-run"' in client.get("/deep/panel").text


def test_a_live_heartbeat_is_not_stale(conn, pdir):
    from datetime import timedelta

    profile = load_profile(pdir)
    plan = deep_run.make_plan(conn, profile, Scoring(), mode="next", n=10, now=NOW)
    rid = deep_run.create_request(conn, plan, NOW - timedelta(minutes=5))
    assert deep_run.running_request(conn, NOW)["id"] == rid


@pytest.mark.parametrize(
    "header", [{"X-Forwarded-For": "203.0.113.9"}, {"Forwarded": "for=203.0.113.9"}]
)
def test_a_forwarded_request_counts_as_remote(client, conn, header):
    html = client.get("/deep/panel", params={"n": "10"}, headers=header).text
    assert 'id="deep-run"' not in html and "jobhunter score --deep 10" in html
    data = {"mode": "next", "n": "10", "plan": token_of(html)}
    assert client.post("/deep/run", data=data, headers=header).status_code == 403
    assert counts(conn) == (0, 0)


def test_group_mode_without_a_group_is_a_400(client, conn):
    assert client.post("/deep/run", data={"mode": "group", "plan": "x"}).status_code == 400
    assert client.post("/deep/run", data={"mode": "bogus", "plan": "x"}).status_code == 400
    assert counts(conn) == (0, 0)


def test_the_n_select_shows_a_planned_n_outside_the_presets(client):
    html = client.get("/deep/panel", params={"n": "7"}).text
    assert '<option value="7" selected>7</option>' in html


def test_changing_n_or_mode_does_not_inherit_the_spend_confirm(client):
    from jobhunter.console.app import TEMPLATES_DIR

    assert 'hx-disinherit="hx-confirm"' in client.get("/deep/panel").text
    rescore = (TEMPLATES_DIR / "_rescore_panel.html").read_text()
    assert '<form id="rescore-form" class="rescore-form" hx-disinherit="hx-confirm"' in rescore


def test_done_is_not_shown_beside_a_running_line(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    gate = threading.Event()
    fake = Factory(FakeClient(gate=gate))
    c.app.state.deep_client_factory = fake
    started = confirm(c, mode="next", n="10").text
    assert "You can leave this page" in started  # inside the live progress fragment
    gate.set()
    c.app.state.deep.join()
    rid = conn.execute("SELECT id FROM deep_request").fetchone()[0]
    done = c.get(f"/deep/{rid}/progress").text
    assert "Done." in done and "You can leave this page" not in done
    assert 'id="deep-again" hx-get="/deep/panel?mode=next"' in done
    assert "A deep pass is running" not in done


def test_the_pages_load_the_deep_styles(client):
    for path in ("/", "/job/1", "/models"):
        assert "deep.css" in client.get(path).text, path


def test_a_thread_that_cannot_open_its_connection_fails_the_row_at_once(pdir, db_path, conn):
    c = make_client(pdir, db_path)
    c.app.state.deep_client_factory = Factory()
    real = c.app.state.conn_factory
    params = {"mode": "next", "n": "10"}
    token = token_of(c.get("/deep/panel", params=params).text)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:  # 1: the POST's own connection; 2: the run thread's
            raise OSError("disk gone")
        return real()

    c.app.state.conn_factory = flaky
    c.post("/deep/run", data={**params, "plan": token})
    c.app.state.deep.join()
    row = conn.execute("SELECT status, error FROM deep_request").fetchone()
    assert tuple(row) == ("failed", "OSError")
    assert runlock.RunLock(db_path, "probe").try_acquire()
