"""Nightly apply-link pass (bug cf23a1c). httpx.MockTransport through FetchContext: no network."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobhunter.config import Settings
from jobhunter.console import activity_log
from jobhunter.core import db
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state
from jobhunter.pipeline import apply_pass, applylink, runner
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
PROFILE = Profile.model_validate({"hard": {"states_allowed": ["CO"]}})
PAGE = "<html><body><h1>Analyst</h1><p>Apply today.</p></body></html>"


def dims(skills: int) -> dict:
    return {
        "skills": {"score": skills, "why": "x"},
        "seniority": {"score": skills, "why": "x"},
        "domain": {"score": skills, "why": "x"},
        "raw_skills": skills,
        "recency_weighted_skills": skills,
        "stale_skills": [],
        "seniority_direction": "match",
    }


class Server:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html=PAGE)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests if r.url.path != "/robots.txt"}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache")}, "fetch": {"max_retries": 0}}
    )


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def factory(settings, conn, server):
    clock = FakeClock()

    def make(url: str):
        return fetch_context_for_url(
            settings, conn, url, transport=server.transport, clock=clock, sleep=clock.sleep
        )

    return make


def host(gid: int) -> str:
    return f"careers{gid}.example.org"


def add(
    conn,
    gid: int,
    *,
    skills: int | None = None,
    label: str | None = None,
    app: str | None = None,
    closes: datetime | None = None,
) -> int:
    ts = NOW.isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, salary_stated, "
        "location_scope, closes_at, first_seen_at, last_seen_at) "
        "VALUES (?, 'wa', ?, ?, 'Analyst', 0, 'single', ?, ?, ?)",
        (gid, str(gid), f"https://{host(gid)}/j/{gid}", closes and closes.isoformat(), ts, ts),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'manual', ?)",
        (gid, gid, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, gid))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, 'CO', 'Town', 1)",
        (gid,),
    )
    if skills is not None:
        conn.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, blockers, created_at) "
            "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 0, ?, '[]', '[]', ?)",
            (gid, json.dumps(dims(skills)), ts),
        )
    if label:
        conn.execute(
            "INSERT INTO label (job_group_id, label, labeled_at) VALUES (?, ?, ?)", (gid, label, ts)
        )
    if app:
        conn.execute(
            "INSERT INTO application (job_group_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (gid, app, ts, ts),
        )
    return gid


def linked(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT job_group_id FROM apply_link")}


def resolve(conn, settings, factory, *, limit=200, profile=PROFILE):
    return apply_pass.resolve_likely(
        conn, settings, profile, now=NOW, limit=limit, ctx_factory=factory
    )


def test_only_shortlisted_and_ab_groups_are_resolved(conn, settings, factory, server):
    a = add(conn, 1, skills=90)
    b = add(conn, 2, skills=70)
    low = add(conn, 3, skills=20)
    short = add(conn, 4, label="interesting", app="interested")
    prep = add(conn, 5, app="preparing")
    applied = add(conn, 6, skills=90, label="interesting", app="applied")
    dismissed = add(conn, 7, skills=90, label="not_interesting")
    unscored = add(conn, 8)
    closed = add(conn, 9, skills=90, closes=NOW - timedelta(days=1))

    picked = apply_pass.select_groups(conn, PROFILE, now=NOW)
    assert [(c.group_id, c.why) for c in picked] == [
        (prep, "shortlist"),
        (short, "shortlist"),
        (a, "A"),
        (b, "B"),
    ]
    result = resolve(conn, settings, factory)
    assert linked(conn) == {a, b, short, prep}
    assert not linked(conn) & {low, applied, dismissed, unscored, closed}
    assert server.hosts() == {host(g) for g in (a, b, short, prep)}
    counts = result.counts()
    assert (counts["candidates"], counts["shortlist"], counts["ab"], counts["tried"]) == (
        4,
        2,
        2,
        4,
    )


def test_without_a_profile_only_the_shortlist(conn, settings, factory):
    add(conn, 1, skills=90)
    short = add(conn, 2, label="interesting")
    resolve(conn, settings, factory, profile=None)
    assert linked(conn) == {short}


def test_bound_holds_and_the_rest_waits(conn, settings, factory, server):
    for gid in range(1, 8):
        add(conn, gid, skills=90)
    result = resolve(conn, settings, factory, limit=3)
    assert len(linked(conn)) == 3
    assert len(server.hosts()) == 3
    assert result.counts()["left"] == 4
    resolve(conn, settings, factory, limit=3)
    assert len(linked(conn)) == 6  # next run takes the next three; resolved ones are skipped


def test_user_set_link_untouched(conn, settings, factory, server):
    gid = add(conn, 1, skills=90, label="interesting", app="interested")
    applylink.set_user_link(conn, gid, "https://mine.example.com/apply", NOW - timedelta(days=1))
    other = add(conn, 2, skills=90)
    resolve(conn, settings, factory)
    link = applylink.get_apply_link(conn, gid)
    assert link.chosen_by == "user" and link.final_url == "https://mine.example.com/apply"
    assert host(gid) not in server.hosts() and "mine.example.com" not in server.hosts()
    assert linked(conn) == {gid, other}


def test_existing_rows_including_login_walls_are_not_fetched(conn, settings, factory, server):
    for gid in (add(conn, 1, skills=90), add(conn, 2, label="interesting", app="interested")):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, "
            "resolved_at, error) VALUES (?, ?, ?, '[]', 'blocked', ?, ?)",
            (gid, f"https://{host(gid)}/j/1", f"https://{host(gid)}/j/1", NOW.isoformat(),
             applylink.LOGIN_ERROR),
        )  # fmt: skip
    assert apply_pass.select_groups(conn, PROFILE, now=NOW) == []
    result = resolve(conn, settings, factory)
    assert server.requests == [] and result.tried == 0


def test_one_group_failing_does_not_stop_the_others(conn, settings, factory, monkeypatch):
    add(conn, 1, skills=90)
    add(conn, 2, skills=90)
    real = applylink.resolve_group

    def flaky(c, gid, **kw):
        if gid == 1:
            raise RuntimeError("boom")
        return real(c, gid, **kw)

    monkeypatch.setattr(applylink, "resolve_group", flaky)
    result = resolve(conn, settings, factory)
    assert result.errors == 1 and linked(conn) == {2}


def test_parallel_path_uses_own_connections(tmp_path, settings, server, monkeypatch):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    for gid in range(1, 6):
        add(c, gid, skills=90)
    clock = FakeClock()

    def ctx(s, conn, url, **kw):
        assert conn is not c  # each worker thread has its own connection
        kw.update(transport=server.transport, clock=clock, sleep=clock.sleep)
        return fetch_context_for_url(s, conn, url, **kw)

    monkeypatch.setattr(apply_pass, "fetch_context_for_url", ctx)
    result = apply_pass.resolve_likely(c, settings, PROFILE, now=NOW, limit=4, workers=3)
    assert result.tried == 4 and result.errors == 0
    assert len(linked(c)) == 4
    c.close()


# ─── in the run ─────────────────────────────────────────────────────────────


def go(conn, settings, factory, **kw):
    return runner.run_pipeline(
        conn,
        settings,
        [],
        adapters={},
        ctx_factory=lambda src: nullcontext(),
        profile_loader=lambda: PROFILE,
        now=NOW,
        apply_ctx_factory=factory,
        **kw,
    )


def test_run_resolves_after_scoring_and_counts_it(conn, settings, factory):
    gid = add(conn, 1, skills=90)
    report = go(conn, settings, factory)
    assert report.stages[-2:] == ["score", "apply_links"]
    assert linked(conn) == {gid}
    counts = json.loads(conn.execute("SELECT counts FROM run").fetchone()[0])
    assert counts["apply_links"]["tried"] == 1
    # /log shows the stage's counts with the run.
    run = next(e for e in activity_log.events(conn, "run") if e.kind == "run")
    line = next(x for x in run.detail if x.startswith("apply_links:"))
    assert line.startswith("apply_links: tried 1, live ") and "errors 0" in line


def test_stage_flag_skips_the_pass(conn, settings, factory, server):
    add(conn, 1, skills=90)
    report = go(conn, settings, factory, stages=runner.parse_stages(["prefilter,score"]))
    assert "apply_links" not in report.counts and linked(conn) == set()
    assert server.requests == []
    assert runner.parse_stages(["apply_links"]) == ["apply_links"]


def test_per_run_zero_turns_it_off(conn, factory, server, tmp_path):
    add(conn, 1, skills=90)
    off = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "c")}, "apply_links": {"per_run": 0}}
    )
    report = go(conn, off, factory)
    assert report.counts["apply_links"] == {"off": 1} and server.requests == []


def test_pass_failure_never_fails_the_run(conn, settings, factory, monkeypatch):
    add(conn, 1, skills=90)

    def broken(*a, **kw):
        raise sqlite3_error()

    monkeypatch.setattr(apply_pass, "resolve_likely", broken)
    report = go(conn, settings, factory)
    assert report.exit_code == 0
    assert report.counts["apply_links"] == {"failed": 1}
    assert any(m.startswith("apply_links: failed") for m in report.messages)
    row = conn.execute("SELECT exit_status, ended_at FROM run").fetchone()
    assert row["exit_status"] == "ok" and row["ended_at"]


def sqlite3_error() -> Exception:
    import sqlite3

    return sqlite3.OperationalError("database is locked")


def test_backfill_ingest_never_runs_the_pass():
    from jobhunter.pipeline import backfill

    assert "apply_links" not in backfill.INGEST_STAGES
