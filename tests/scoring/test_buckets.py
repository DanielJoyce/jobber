import json
from datetime import UTC, datetime

import pytest

from jobhunter.core import db
from jobhunter.core.models import Bucket, JobLocation, LocationScope, Verdict
from jobhunter.scoring.buckets import (
    assign_bucket,
    comp_fit,
    compute_row,
    location_fit,
    overall,
    preview,
    verdict_for,
)
from jobhunter.scoring.profile import Profile

W = {"skills": 0.30, "seniority": 0.20, "domain": 0.20, "comp": 0.15, "location": 0.15}


def mk(**hard) -> Profile:
    soft = hard.pop("soft", {})
    return Profile.model_validate({"hard": hard, "soft": soft})


def locs(*states):
    return [JobLocation(state=s) for s in states]


@pytest.mark.parametrize(
    ("args", "hard", "expected"),
    [
        ((100000, 120000, "year", False), {"salary_floor": {"amount": 100000}}, None),
        ((None, None, "year", True), {"salary_floor": {"amount": 100000}}, None),
        ((100000, 120000, "year", True), {"salary_floor": {"amount": 100000}}, 100),
        ((90000, 99000, "year", True), {"salary_floor": {"amount": 100000}}, 0),
        ((100000, 110000, "year", True), {"salary_floor": {"amount": 100000}}, 50),
        (
            (90000, 130000, "year", True),
            {"salary_floor": {"amount": 100000}, "salary_target": {"amount": 150000}},
            60,
        ),
        ((200000, 300000, "year", True), {"salary_floor": {"amount": 100000}}, 100),
        ((50, 55, "hour", True), {"salary_floor": {"amount": 100000}}, 72),  # 114,400/yr
        ((45, 50, "hour", True), {"salary_floor": {"amount": 100000}}, 20),  # 104,000/yr
        ((7000, 8000, "month", True), {"salary_floor": {"amount": 100000}}, 0),
        ((100000, 120000, "year", True), {}, None),
    ],
)
def test_comp_fit(args, hard, expected):
    assert comp_fit(*args, mk(**hard)) == expected


@pytest.mark.parametrize(
    ("hard", "states", "scope", "expected"),
    [
        ({"states_excluded": ["TX"]}, ["TX"], "single", 0),
        ({"states_excluded": ["TX"]}, ["TX", "CO"], "multi_state", 50),
        ({"soft": {"state_ranking": ["CO", "WA", "OR"]}}, ["CO"], "single", 100),
        ({"soft": {"state_ranking": ["CO", "WA", "OR"]}}, ["OR"], "single", 80),
        ({"soft": {"state_ranking": ["CO", "WA", "OR"]}}, ["NV"], "single", 50),
        ({"soft": {"state_ranking": ["CO", "WA", "OR"]}}, ["OR", "WA"], "multi_state", 90),
        ({"states_allowed": ["CO"]}, ["UT"], "single", 0),
        ({"states_allowed": ["CO"]}, ["UT", "CO"], "multi_state", 50),
        ({"remote_ok": True, "soft": {"remote_bonus": 10}}, [], "remote_us", 80),
        ({"remote_ok": True, "soft": {"remote_bonus": 50}}, [], "nationwide", 100),
        ({"remote_ok": True, "soft": {"remote_bonus": 10}}, [], "negotiable", 70),
        ({"remote_ok": False}, [], "remote_us", 30),
        ({"remote_ok": True}, ["TX"], "single", 50),
        ({"remote_ok": True, "states_excluded": ["TX"]}, ["TX"], "remote_us", 70),
        ({}, [], "unknown", 50),
        ({}, [], "overseas", 0),
    ],
)
def test_location_fit(hard, states, scope, expected):
    assert location_fit(locs(*states), LocationScope(scope), mk(**hard)) == expected


def test_overall_renormalizes_without_comp():
    dims = {"skills": 80, "seniority": 80, "domain": 80}
    assert overall(dims, 100, 80, W) == 83
    assert overall(dims, None, 80, W) == 80  # not dragged down by a zero comp
    low = overall(dims, 0, 80, W)
    assert low < overall(dims, None, 80, W)


BASE = dict(
    raw_skills=85,
    recency_skills=85,
    domain=80,
    comp=70,
    overall_score=85,
    direction="match",
    blockers=[],
)


@pytest.mark.parametrize(
    ("over", "bucket"),
    [
        ({}, Bucket.A),
        ({"overall_score": 70, "recency_skills": 70, "raw_skills": 70}, Bucket.B),
        ({"direction": "above", "overall_score": 60}, Bucket.C),
        ({"domain": 40, "overall_score": 60}, Bucket.D),
        ({"direction": "below", "comp": 20}, Bucket.E),
        ({"raw_skills": 88, "recency_skills": 41}, Bucket.F),
        ({"recency_skills": 30, "raw_skills": 30}, Bucket.G),
        ({"domain": 20}, Bucket.G),
        # blockers demote one level
        ({"blockers": ["clearance"]}, Bucket.B),
        ({"overall_score": 70, "recency_skills": 70, "blockers": ["x"]}, Bucket.C),
        # unstated comp never triggers E
        ({"direction": "below", "comp": None}, Bucket.A),
        # spec gap fallthrough
        ({"overall_score": 50, "recency_skills": 62, "raw_skills": 62}, Bucket.D),
        ({"overall_score": 30, "recency_skills": 62, "raw_skills": 62}, Bucket.G),
    ],
)
def test_assign_bucket(over, bucket):
    assert assign_bucket(**{**BASE, **over}) is bucket


def test_threshold_override():
    assert assign_bucket(**{**BASE, "overall_score": 70}, thresholds={"b_overall": 75}) is Bucket.D


@pytest.mark.parametrize(
    ("score", "verdict"),
    [
        (90, Verdict.strong),
        (78, Verdict.strong),
        (77, Verdict.possible),
        (60, Verdict.possible),
        (40, Verdict.weak),
        (39, Verdict.mismatch),
    ],
)
def test_verdict(score, verdict):
    assert verdict_for(score, [], None) is verdict


def row(**kw):
    d = {
        "skills": {"score": 85, "why": ""},
        "seniority": {"score": 80, "why": ""},
        "domain": {"score": 80, "why": ""},
        "raw_skills": 85,
        "recency_weighted_skills": 85,
    }
    d.update(kw)
    return {"dimensions": json.dumps(d), "blockers": "[]"}


JOB = {
    "salary_min": 150000,
    "salary_max": 170000,
    "salary_period": "year",
    "salary_stated": 1,
    "location_scope": "single",
}


def test_compute_row_bullseye_and_stale():
    p = mk(salary_floor={"amount": 100000}, soft={"state_ranking": ["CO"]})
    c = compute_row(row(), JOB, locs("CO"), p)
    assert c.bucket is Bucket.A and c.comp == 100 and c.location == 100
    stale = compute_row(row(raw_skills=88, recency_weighted_skills=41), JOB, locs("CO"), p)
    assert stale.bucket is Bucket.F
    assert stale.raw_skills == 88 and stale.recency_weighted_skills == 41


def test_compute_row_tolerant():
    p = mk(salary_floor={"amount": 100000})
    r = {"dimensions": json.dumps({"skills": 70, "seniority": 60, "domain": 60}), "blockers": None}
    c = compute_row(r, {**JOB, "salary_stated": 0}, [], p)
    assert c.comp is None and c.raw_skills == 70 and c.recency_weighted_skills == 70
    c = compute_row(row(seniority_direction="above"), JOB, locs("CO"), p)
    assert c.seniority_direction == "above" and c.bucket is Bucket.C


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


def _add(conn, n, salary_max, dims):
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, salary_min, salary_max, "
        "salary_period, salary_stated, location_scope, first_seen_at, last_seen_at) "
        "VALUES (?, 'wa', ?, 'u', 't', ?, ?, 'year', 1, 'single', ?, ?)",
        (n, str(n), salary_max, salary_max, now, now),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, now),
    )
    conn.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, 'CO', 1)", (n,))
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 80, ?, '[]', ?)",
        (n, json.dumps(dims), now),
    )


def test_preview_counts_change_with_floor(conn):
    dims = {
        "skills": {"score": 85},
        "seniority": {"score": 85},
        "domain": {"score": 85},
        "raw_skills": 85,
        "recency_weighted_skills": 85,
    }
    _add(conn, 1, 120000, dims)
    _add(conn, 2, 200000, dims)
    high = mk(salary_floor={"amount": 140000}, soft={"state_ranking": ["CO"]})
    low = mk(salary_floor={"amount": 100000}, soft={"state_ranking": ["CO"]})
    out = preview(conn, high, low)
    assert out["total"] == 2
    assert out["salary_filtered"] == {"before": 1, "after": 0}
    assert out["before"]["A"] + out["before"]["B"] == 2
    assert out["after"]["A"] >= out["before"]["A"]
    assert sum(out["after"].values()) == 2
    assert all(m["job_id"] == 1 for m in out["moves"])
    assert out["moves"], "job 1 should move bucket when the floor drops"
