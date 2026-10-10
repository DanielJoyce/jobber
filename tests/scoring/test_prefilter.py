from datetime import UTC, datetime, timedelta

import pytest

from jobhunter.core import db
from jobhunter.core.models import JobLocation
from jobhunter.scoring.prefilter import evaluate, rejected_summary, run_prefilter
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
YEAR = {"salary_stated": 1, "salary_period": "year"}
HOUR = {"salary_stated": 1, "salary_period": "hour"}


def prof(**hard):
    return Profile.model_validate({"hard": hard})


def job(**kw):
    base = {
        "title": "Systems Engineer",
        "location_scope": "single",
        "remote": "unknown",
        "employment_type": "unknown",
        "salary_stated": 0,
        "salary_min": None,
        "salary_max": None,
        "salary_period": None,
        "closes_at": None,
        "description_text": "",
    }
    return base | kw


def locs(*states):
    return [JobLocation(state=s) for s in states]


def desc(text):
    return {"description_text": text}


def lack(*keys):
    return {"requires_i_lack": list(keys)}


FLOOR = {"salary_floor": {"amount": 100000}}
CO = {"states_allowed": ["CO"]}

# (id, job overrides, location states, hard fields, expected reasons)
CASES = [
    ("state_reject", {}, ["TX"], CO, ["state_not_allowed"]),
    ("state_pass", {}, ["CO"], CO, []),
    ("territory_excluded", {}, ["PR"], {"states_excluded": ["PR"]}, ["state_not_allowed"]),
    ("territory_included", {}, ["PR"], {"states_excluded": ["GU"]}, []),
    ("territory_default", {}, ["PR"], {}, []),
    ("state_multi_any", {"location_scope": "multi_state"}, ["TX", "CO"], CO, []),
    ("state_excluded", {}, ["TX"], {"states_excluded": ["TX"]}, ["state_not_allowed"]),
    ("state_remote_scope", {"location_scope": "remote_us"}, ["TX"], CO, []),
    ("state_nationwide", {"location_scope": "nationwide"}, [], CO, []),
    ("state_none_enumerated", {"location_scope": "unknown"}, [], CO, []),
    ("remote_only_reject", {"remote": "onsite"}, ["CO"], {"remote_only": True},
     ["remote_only_violation"]),
    ("remote_only_unknown", {"remote": "unknown"}, ["CO"], {"remote_only": True}, []),
    ("remote_only_off", {"remote": "onsite"}, ["CO"], {}, []),
    ("salary_reject", YEAR | {"salary_min": 50000, "salary_max": 60000}, ["CO"], FLOOR,
     ["salary_below_floor"]),
    ("salary_pass_max_above", YEAR | {"salary_min": 50000, "salary_max": 120000}, ["CO"],
     FLOOR, []),
    ("salary_unstated_pass", {}, ["CO"], FLOOR, []),
    ("salary_hourly_reject", HOUR | {"salary_min": 20, "salary_max": 30}, ["CO"], FLOOR,
     ["salary_below_floor"]),
    ("salary_hourly_pass", HOUR | {"salary_min": 40, "salary_max": 60}, ["CO"], FLOOR, []),
    ("salary_floor_monthly", YEAR | {"salary_min": 90000, "salary_max": 95000}, ["CO"],
     {"salary_floor": {"amount": 8000, "period": "month"}}, ["salary_below_floor"]),
    ("salary_hide_unstated", {}, ["CO"], {"hide_unstated_salary": True}, ["salary_unstated"]),
    ("etype_reject", {"employment_type": "seasonal"}, ["CO"],
     {"employment_types_excluded": ["seasonal"]}, ["employment_type_excluded"]),
    ("etype_unknown_pass", {}, ["CO"], {"employment_types_excluded": ["seasonal"]}, []),
    ("etype_other_pass", {"employment_type": "full_time"}, ["CO"],
     {"employment_types_excluded": ["seasonal"]}, []),
    ("title_reject", {"title": "Summer INTERN, IT"}, ["CO"], {"title_exclusions": ["intern"]},
     ["title_excluded"]),
    ("title_word_boundary", {"title": "Internal Systems Engineer"}, ["CO"],
     {"title_exclusions": ["intern"]}, []),
    ("closed_reject", {"closes_at": (NOW - timedelta(days=1)).isoformat()}, ["CO"], {},
     ["closed"]),
    ("closed_date_only", {"closes_at": "2026-09-30"}, ["CO"], {}, ["closed"]),
    ("closed_future_pass", {"closes_at": (NOW + timedelta(days=1)).isoformat()}, ["CO"], {}, []),
    ("overseas_reject", {"location_scope": "overseas"}, [], {}, ["overseas"]),
    ("clearance_required", desc("An active Secret clearance required."), ["CO"],
     lack("active_security_clearance"), ["credential_required"]),
    ("clearance_must", desc("Must possess an active TS/SCI clearance."), ["CO"],
     lack("active_security_clearance"), ["credential_required"]),
    ("clearance_preferred", desc("Secret clearance preferred."), ["CO"],
     lack("active_security_clearance"), []),
    ("clearance_obtain", desc("Ability to obtain a clearance."), ["CO"],
     lack("active_security_clearance"), []),
    ("cdl_required", desc("Valid CDL required."), ["CO"], lack("cdl"), ["credential_required"]),
    ("cdl_preferred", desc("CDL preferred."), ["CO"], lack("cdl"), []),
    ("rn_required", desc("Current RN license required"), ["CO"], lack("rn_license"),
     ["credential_required"]),
    ("pe_required", desc("Must hold a valid PE license."), ["CO"], lack("pe_license"),
     ["credential_required"]),
    ("cred_not_lacked", desc("Valid CDL required."), ["CO"], {}, []),
    ("cred_unknown_key", desc("forklift required"), ["CO"], lack("forklift"), []),
    ("multiple_reasons", {"title": "Intern", "location_scope": "overseas"}, [],
     {"title_exclusions": ["intern"]}, ["title_excluded", "overseas"]),
]  # fmt: skip


@pytest.mark.parametrize(
    ("overrides", "states", "hard", "expected"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_rules(overrides, states, hard, expected):
    passed, reasons = evaluate(job(**overrides), locs(*states), prof(**hard), NOW)
    assert reasons == expected
    assert passed == (not expected)


# ─── run_prefilter ──────────────────────────────────────────────────────────


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


def add(conn, ext, *, state="CO", title="Engineer", stage="grouped", canonical=True):
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, stage, location_scope, "
        "first_seen_at, last_seen_at) VALUES ('wa', ?, ?, ?, ?, 'single', ?, ?)",
        (ext, f"https://example.com/{ext}", title, stage, NOW.isoformat(), NOW.isoformat()),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (jid, state)
    )
    if canonical:
        g = conn.execute(
            "INSERT INTO job_group (canonical_job_id, method, created_at) "
            "VALUES (?, 'exact_hash', ?)",
            (jid, NOW.isoformat()),
        ).lastrowid
        conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (g, jid))
    return jid


def stage_of(conn, jid):
    return conn.execute("SELECT stage FROM job WHERE id = ?", (jid,)).fetchone()[0]


def test_run_counts_stage_and_idempotent(conn):
    ok = add(conn, "1", state="CO")
    bad = add(conn, "2", state="TX")
    add(conn, "3", state="TX", canonical=False)  # non-canonical member: untouched
    early = add(conn, "4", state="TX", stage="normalized")
    p = prof(states_allowed=["CO"])
    assert run_prefilter(conn, p, now=NOW) == {"evaluated": 2, "passed": 1, "rejected": 1}
    assert stage_of(conn, ok) == stage_of(conn, bad) == "prefiltered"
    assert stage_of(conn, early) == "normalized"
    assert conn.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 2
    assert run_prefilter(conn, p, now=NOW) == {"evaluated": 0, "passed": 0, "rejected": 0}
    assert run_prefilter(conn, p, now=NOW, force=True)["evaluated"] == 2


def test_limit(conn):
    for i in range(3):
        add(conn, str(i))
    assert run_prefilter(conn, prof(), now=NOW, limit=2)["evaluated"] == 2


def test_filter_version_change_reevaluates(conn):
    jid = add(conn, "1", state="TX")
    p1 = prof(states_allowed=["CO"])
    p2 = prof(states_allowed=["CO", "TX"])
    assert p1.filter_version != p2.filter_version
    run_prefilter(conn, p1, now=NOW)
    row = conn.execute("SELECT * FROM prefilter_result WHERE job_id = ?", (jid,)).fetchone()
    assert (row["passed"], row["filter_version"]) == (0, p1.filter_version)
    assert run_prefilter(conn, p2, now=NOW)["passed"] == 1
    row = conn.execute("SELECT * FROM prefilter_result WHERE job_id = ?", (jid,)).fetchone()
    assert (row["passed"], row["filter_version"], row["reasons"]) == (1, p2.filter_version, "[]")
    assert stage_of(conn, jid) == "prefiltered"


def test_rejected_summary(conn):
    add(conn, "1", state="TX")
    add(conn, "2", state="TX", title="Intern")
    add(conn, "3", state="CO")
    p = prof(states_allowed=["CO"], title_exclusions=["intern"])
    run_prefilter(conn, p, now=NOW)
    assert rejected_summary(conn, p.filter_version) == {"state_not_allowed": 2, "title_excluded": 1}
    assert rejected_summary(conn, "other") == {}
