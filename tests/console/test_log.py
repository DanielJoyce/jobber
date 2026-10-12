"""The /log page and the Today card's last-24-hours numbers (bug be1d232, specs/019 Addendum C)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Settings
from jobhunter.console import activity_log as al
from jobhunter.console.app import NAV, create_app
from jobhunter.core import db
from jobhunter.pipeline.listing import to_iso

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
EMAIL = "sentinel.person@example.net"
KEY = "sk-ant-api03-" + "A1b2C3d4" * 6
ERR = f"auth failed for {EMAIL} using {KEY} at https://api.example.com/v1?token=abc123"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


@pytest.fixture
def client(tmp_path, db_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = Settings(paths=Paths(profile_dir=profile, db_path=db_path))
    return TestClient(create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW))


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    c.execute(
        "INSERT INTO source (key, state, class, name, family, tier, entry, policy, status) "
        "VALUES ('s1', 'US', 'A', 'S1', 'joblink', 'http', 'https://e.invalid/', 'enabled', 'ok')"
    )
    yield c
    c.close()


def ago(**kw):
    return to_iso(NOW - timedelta(**kw))


def add_job(conn, ext, first_seen, group=None):
    cur = conn.execute(
        "INSERT INTO job (source_key, external_id, job_group_id, url, title, first_seen_at, "
        "last_seen_at) VALUES ('s1', ?, ?, 'https://e.example/x', 't', ?, ?)",
        (ext, group, first_seen, first_seen),
    )
    return cur.lastrowid


def add_group(conn, job_id, created):
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, method, created_at) VALUES (?, 'exact_hash', ?)",
        (job_id, created),
    ).lastrowid
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, job_id))
    return gid


def add_score(conn, gid, created, cost=0.01, model="m"):
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, cost_usd, created_at) "
        "VALUES (?, 'screen', ?, 'p', 'v', 'possible', 50, '{}', '[]', ?, ?)",
        (gid, model, cost, created),
    )


def seed_all_kinds(conn):
    conn.execute(
        'INSERT INTO run (started_at, ended_at, stages, exit_status, counts, "trigger") '
        "VALUES (?, ?, 'list,score', 'ok', ?, 'timer')",
        (ago(hours=5), ago(hours=5, minutes=-3), json.dumps({"list": {"stubs": 42}})),
    )
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, scorer, scored, errored, cost_usd, error, "
        "total, started_at, done_at) VALUES (?, 'recent', 'v', 5, 0, 0, 'failed', ?, 0, 5, 0.5, "
        "?, 5, ?, ?)",
        (ago(hours=4), KEY, ERR, ago(hours=4), ago(hours=4, minutes=-1)),
    )
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at, collected_at, counts) "
        "VALUES ('b1', 'screen', 'anthropic:claude-haiku-5-5', 'p', 'v', 30, ?, ?, ?)",
        (ago(hours=8), ago(hours=7), json.dumps({"succeeded": 29, "errored": 1})),
    )
    conn.execute(
        "INSERT INTO deep_request (requested_at, mode, scorer, scoring_version, group_ids, "
        "estimated_usd, max_usd, status, total, done, scored, spent_usd, error, started_at) "
        "VALUES (?, 'next', 'anthropic:claude-opus-5-5', 'v', '[1]', 1, 2, 'failed', 3, 1, 0, "
        "0.2, ?, ?)",
        (ago(hours=3), ERR, ago(hours=3)),
    )
    conn.execute(
        "INSERT INTO mail_message (message_id, family, subject, received_at, entries, "
        "processed_at) VALUES ('m1', 'f', ?, ?, 4, ?)",
        (f"hi {EMAIL}", ago(hours=2), ago(hours=2)),
    )
    conn.execute(
        "INSERT INTO capture_log (action_id, route, captured_at, host, method, outcome) "
        "VALUES ('a1', '/ext/capture', ?, 'boards.example.org', 'jsonld', 'added')",
        (ago(hours=1),),
    )


def seed_draft(conn):
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO draft_run (packet_id, kind, runner, started_at, finished_at, outcome, "
        "error) VALUES (1, 'cover_letter', 'cli', ?, ?, 'failed', ?)",
        (ago(minutes=30), ago(minutes=29), ERR),
    )


def test_every_kind_renders_and_nothing_secret_leaks(client, conn):
    seed_all_kinds(conn)
    seed_draft(conn)
    r = client.get("/log")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    for text in (
        "Pipeline runs",
        "Run (timer): list,score",
        "list: stubs 42",
        "Re-score (recent)",
        "scored 0 of 5, errored 5",
        "Batch submitted (screen) with anthropic:claude-haiku-5-5",
        "Batch collected (screen)",
        "errored 1",
        "Deep pass (next) with anthropic:claude-opus-5-5",
        "Mail fetch",
        "1 message(s), 4 posting(s) found",
        "Captured from boards.example.org",
        "Drafted cover_letter (cli)",
        "$0.50",
        "subscription",
    ):
        assert text in r.text, text
    for secret in (EMAIL, KEY, "abc123", "A1b2C3d4"):
        assert secret not in r.text
    assert "(email)" in r.text
    assert "(hidden value " in r.text  # the key-shaped scorer spec


def test_newest_first(client, conn):
    seed_all_kinds(conn)
    r = client.get("/log")
    assert r.text.index("Captured from boards") < r.text.index("posting(s) found")
    assert r.text.index("posting(s) found") < r.text.index("Deep pass (next)")
    assert r.text.index("Deep pass (next)") < r.text.index("Re-score (recent)")
    assert r.text.index("Re-score (recent)") < r.text.index("Run (timer)")
    assert r.text.index("Run (timer)") < r.text.index("Batch submitted")


def test_filter_by_kind(client, conn):
    seed_all_kinds(conn)
    r = client.get("/log?kind=capture")
    assert "Captured from" in r.text and "Re-score (recent)" not in r.text
    assert "Run (timer)" not in r.text
    # An unknown kind reads as all.
    assert "Re-score (recent)" in client.get("/log?kind=bogus").text


def test_empty_database(client):
    r = client.get("/log")
    assert r.status_code == 200 and "Nothing here yet" in r.text
    assert client.get("/log?kind=run").status_code == 200
    today = client.get("/")
    assert today.status_code in (200, 303, 307)


def test_unfinished_run_is_interrupted_when_the_lock_is_free(client, conn):
    conn.execute("INSERT INTO run (started_at, stages) VALUES (?, 'list')", (ago(hours=1),))
    r = client.get("/log?kind=run")
    assert "interrupted" in r.text and "running" not in r.text


def _hold(db_path, command):
    from jobhunter.core import runlock

    lock = runlock.RunLock(db_path, command)
    lock.acquire()
    return lock


def test_unfinished_run_is_running_only_when_run_holds_the_lock(client, conn, db_path):
    real_now = to_iso(datetime.now(UTC))
    conn.execute("INSERT INTO run (started_at, stages) VALUES (?, 'list')", (ago(hours=9),))
    conn.execute("INSERT INTO run (started_at, stages) VALUES (?, 'list')", (real_now,))
    lock = _hold(db_path, "run")
    try:
        r = client.get("/log?kind=run")
    finally:
        lock.release()
    # Only the newest unfinished run can be the holder.
    assert r.text.count(">running<") == 1 and r.text.count(">interrupted<") == 1


def test_a_dead_run_is_interrupted_while_another_job_holds_the_lock(client, conn, db_path):
    conn.execute("INSERT INTO run (started_at, stages) VALUES (?, 'list')", (ago(hours=1),))
    for command in ("console re-score", "score --submit", "run"):
        lock = _hold(db_path, command)
        try:
            r = client.get("/log?kind=run")
        finally:
            lock.release()
        # Even a "run" holder that started an hour after this row is not this row's process.
        assert ">interrupted<" in r.text and ">running<" not in r.text, command


def test_degraded_run_is_partial_not_failed(client, conn):
    conn.execute(
        "INSERT INTO run (started_at, ended_at, stages, exit_status) "
        "VALUES (?, ?, 'list', 'degraded')",
        (ago(hours=1), ago(minutes=50)),
    )
    r = client.get("/log?kind=run")
    assert ">partial<" in r.text and ">failed<" not in r.text


def test_window_boundary(conn):
    from jobhunter.scoring.profile import Profile

    prof = Profile()
    edge = ago(hours=24)
    for i, t in enumerate((edge, ago(hours=24, seconds=1), ago(hours=23, minutes=59))):
        job = add_job(conn, f"j{i}", t)
        gid = add_group(conn, job, t)
        add_score(conn, gid, t, cost=1.0, model=f"m{i}")
        conn.execute(
            "INSERT INTO prefilter_result VALUES (?, 1, '[]', ?, ?)", (job, prof.filter_version, t)
        )
        conn.execute(
            "INSERT INTO score_failure (job_group_id, tier, model, prompt_version, "
            "scoring_version, reason, created_at) VALUES (?, 'screen', 'm', 'p', 'v', 'x', ?)",
            (gid, t),
        )
    got = al.since_yesterday(conn, NOW, prof, "m0")
    # The row exactly 24 h old and the newer one count; the row a second older does not.
    assert got["jobs"] == got["groups"] == got["passed"] == got["scored"] == 2
    assert got["refused"] == 2
    assert got["spend"] == pytest.approx(2.0)


def test_passed_counts_new_jobs_not_reevaluations(conn):
    from jobhunter.scoring.profile import Profile

    prof = Profile()
    old = add_job(conn, "old", ago(days=5))
    new = add_job(conn, "new", ago(hours=1))
    for job in (old, new):  # a profile change re-evaluated both just now
        conn.execute(
            "INSERT INTO prefilter_result VALUES (?, 1, '[]', ?, ?)",
            (job, prof.filter_version, ago(minutes=1)),
        )
    assert al.since_yesterday(conn, NOW, prof, "m")["passed"] == 1
    assert al.since_yesterday(conn, NOW, None, "m")["passed"] is None


def test_failures_include_rescore_and_batch_errors(conn):
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, errored, done_at) "
        "VALUES (?, 'recent', 'v', 5, 0, 0, 'failed', 5, ?)",
        (ago(hours=3), ago(hours=3)),
    )
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, errored, done_at) "
        "VALUES (?, 'recent', 'v', 5, 0, 0, 'failed', 9, ?)",
        (ago(days=4), ago(days=4)),
    )
    conn.execute(
        "INSERT INTO score_batch (id, tier, model, prompt_version, scoring_version, "
        "request_count, submitted_at, collected_at, counts) "
        "VALUES ('b', 'screen', 'm', 'p', 'v', 9, ?, ?, ?)",
        (ago(hours=5), ago(hours=4), json.dumps({"succeeded": 6, "errored": 2, "expired": 1})),
    )
    got = al.since_yesterday(conn, NOW)
    assert got["rescore_errored"] == 5 and got["rescore_failed"] == 1
    assert got["batch_errored"] == 3 and got["refused"] == 0


def test_backlog_is_the_daily_runs_own_eligibility(conn):
    from jobhunter.scoring import screen
    from jobhunter.scoring.profile import Profile

    prof = Profile()
    t = ago(days=3)
    ids = []
    for i in range(4):
        job = add_job(conn, f"b{i}", t)
        ids.append((job, add_group(conn, job, t)))
        conn.execute(
            "INSERT INTO prefilter_result VALUES (?, ?, '[]', ?, ?)",
            (job, i != 3, prof.filter_version, t),
        )
    add_score(conn, ids[0][1], t)  # scored: not backlog
    conn.execute(
        "INSERT INTO score_failure (job_group_id, tier, model, prompt_version, scoring_version,"
        " reason, created_at) VALUES (?, 'screen', 'm', ?, ?, 'refusal', ?)",
        (ids[1][1], screen.PROMPT_VERSION, prof.scoring_version, t),
    )  # failed under the daily run's key: not backlog
    # ids[2] passed and is unscored; ids[3] did not pass.
    got = al.since_yesterday(conn, NOW, prof, "m")["backlog"]
    assert got == 1
    assert got == len(screen.eligible_groups(conn, prof, scorer="m", limit=100, new_only=True))
    assert al.since_yesterday(conn, NOW, None, "m")["backlog"] is None


def test_today_card_renders_and_links_to_the_log(tmp_path, db_path, conn):
    from jobhunter.scoring.profile import Profile

    conn.execute(
        'INSERT INTO run (started_at, ended_at, stages, exit_status, "trigger") '
        "VALUES (?, ?, 'list', 'ok', 'timer')",
        (ago(hours=2), ago(hours=2, minutes=-5)),
    )
    job = add_job(conn, "c1", ago(hours=1))
    add_group(conn, job, ago(hours=1))
    app = create_app(
        Settings(paths=Paths(profile_dir=tmp_path, db_path=db_path)),
        lambda: db.connect(db_path),
        lambda: Profile(),
        lambda: NOW,
    )
    r = TestClient(app).get("/", follow_redirects=False)
    assert r.status_code == 200
    assert 'id="last-24h"' in r.text and 'href="/log"' in r.text
    assert "5m 00s" in r.text and "Jobs pulled in" in r.text and "Unscored backlog" in r.text


def test_log_is_in_the_nav_and_linked_from_setup(client):
    assert ("/log", "Log") in NAV
    assert 'href="/log"' in client.get("/setup").text
    assert 'href="/log"' in client.get("/log").text


def test_safe_error():
    out = al.safe_error(ERR)
    assert EMAIL not in out and KEY not in out and "abc123" not in out
    assert len(al.safe_error("x " * 500)) <= al.ERROR_MAX
    assert al.safe_error(None) == ""


def test_safe_error_hides_angle_bracket_addresses():
    assert "@" not in al.safe_error("sent to <you>@example.com and Bob <bob@example.org>.")


def test_safe_error_more_shapes():
    # The number is split so the secret scanner does not flag the test file itself.
    out = al.safe_error(
        "call +1 (555) " + "123-" + "4567 or 555-" + "123-" + "4567; "
        "Authorization: Bearer abc123def; "
        "API_KEY=short1 token=xyz at example.com/path?user=bob&x=1 mail bob%40example.org"
    )
    for leaked in ("555", "abc123def", "short1", "xyz", "user=bob", "bob", "example.org"):
        assert leaked not in out, (leaked, out)


def test_draft_with_no_outcome_is_stopped_when_nothing_holds_its_claim(client, conn):
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO draft_run (packet_id, kind, runner, started_at) "
        "VALUES (1, 'resume', 'api', ?)",
        (ago(hours=1),),
    )
    r = client.get("/log?kind=draft")
    assert ">stopped<" in r.text and ">running<" not in r.text
