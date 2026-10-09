"""Inbox: bucket grouping, filters, label writes and the HTML surface. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Settings
from jobhunter.console import inbox
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile

PREFS = """\
hard:
  states_allowed: [CO, WA]
  remote_ok: true
soft:
  state_ranking: [CO, WA]
resume_path: resume.md
"""


def dims(skills=90, raw=None, rec=None, seniority=90, domain=90, direction="match", stale=()):
    rec = skills if rec is None else rec
    return {
        "skills": {"score": skills, "why": "skills reason"},
        "seniority": {"score": seniority, "why": "seniority reason"},
        "domain": {"score": domain, "why": "domain reason"},
        "raw_skills": skills if raw is None else raw,
        "recency_weighted_skills": rec,
        "stale_skills": list(stale),
        "seniority_direction": direction,
    }


@pytest.fixture
def profile_dir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


@pytest.fixture
def profile(profile_dir):
    return load_profile(profile_dir)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Connecting Colorado', 'x', 'http', 'https://example.com', 'enabled'), "
        "('fed', 'C', 'USAJOBS', 'usajobs', 'api', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


NOW = datetime.now(UTC)


def add(
    conn,
    n,
    dimensions,
    *,
    states=("CO",),
    scope="single",
    source="co",
    flags=(),
    unverified=0,
    salary=None,
    tier="screen",
    posted_days=2,
    completeness="full",
    title=None,
):
    ts = NOW.isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, salary_min, "
        "salary_max, salary_period, salary_stated, location_scope, remote, employment_type, "
        "posted_at, description_completeness, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, 'Acme', ?, ?, 'year', ?, ?, 'onsite', 'full-time', ?, ?, ?, ?)",
        (
            n,
            source,
            str(n),
            f"https://example.com/{n}",
            title or f"Job {n}",
            salary[0] if salary else None,
            salary[1] if salary else None,
            1 if salary else 0,
            scope,
            (NOW - timedelta(days=posted_days)).isoformat(),
            completeness,
            ts,
            ts,
        ),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    for i, st in enumerate(states):
        conn.execute(
            "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, ?, ?, ?)",
            (n, st, "Town" if st else None, int(i == 0)),
        )
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, shape_flags, evidence_unverified, "
        "created_at) VALUES (?, ?, 'm', 'p', 's', 'strong', 0, ?, '[]', '[]', ?, ?, ?)",
        (n, tier, json.dumps(dimensions), json.dumps(list(flags)), unverified, ts),
    )


@pytest.fixture
def seeded(conn):
    add(conn, 1, dims(90), flags=["on_call_heavy"], salary=(118000, 142000), title="Bullseye")
    add(conn, 2, dims(70, seniority=70, domain=70), title="Strong", completeness="partial")
    add(conn, 3, dims(40, raw=85, stale=["VMware", "Oracle"]), title="Stale one", unverified=1)
    add(conn, 4, dims(45, raw=80, stale=["VMware"]), title="Stale two")
    add(conn, 5, dims(20), title="Mismatch")
    return conn


def test_buckets_and_fields(seeded, profile):
    data = inbox.inbox_items(seeded, profile)
    assert data.counts == {"A": 1, "B": 1, "C": 0, "D": 0, "E": 0, "F": 2, "G": 1}
    a = data.buckets["A"][0]
    assert a.title == "Bullseye"
    assert a.shape_flags == ["on_call_heavy"]
    assert a.salary == "$118k\u2013$142k"
    assert a.source_name == "Connecting Colorado"
    assert a.source_badge == "state"
    assert a.recency_skills == 90 and a.raw_skills == 90
    assert a.posted == "2d"
    b = data.buckets["B"][0]
    assert b.partial and b.salary == "salary not stated" and not b.salary_stated
    assert any(i.evidence_unverified for i in data.buckets["F"])


def test_posting_link_prefers_the_human_apply_url(seeded, profile):
    # Workday stubs keep the JSON detail endpoint in `url` and the human page in `apply_url`.
    api = "https://tenant.example.gov/wday/cxs/t/Site/job/x"
    seeded.execute("UPDATE job SET url = ? WHERE id = 1", (api,))
    plain = inbox.inbox_items(seeded, profile).buckets["A"][0]
    assert plain.url == api
    seeded.execute(
        "UPDATE job SET apply_url = ? WHERE id = 1", ("https://tenant.example.gov/Site/job/x",)
    )
    item = inbox.inbox_items(seeded, profile).buckets["A"][0]
    assert item.url == "https://tenant.example.gov/Site/job/x"


def test_f_summary_and_g_hidden(seeded, profile):
    data = inbox.inbox_items(seeded, profile)
    assert data.stale_summary == [("VMware", 2), ("Oracle", 1)]
    assert data.buckets["G"] == []
    assert "G" not in data.shown
    shown_g = inbox.inbox_items(seeded, profile, bucket="G")
    assert [i.title for i in shown_g.buckets["G"]] == ["Mismatch"]
    assert shown_g.buckets["A"] == []


def test_deep_tier_preferred(seeded, profile):
    seeded.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (5, 'deep', 'm', 'p', 's', 'strong', 0, ?, '[]', ?)",
        (json.dumps(dims(95)), NOW.isoformat()),
    )
    data = inbox.inbox_items(seeded, profile)
    assert {i.title for i in data.buckets["A"]} == {"Bullseye", "Mismatch"}


def test_federal_badge_and_multi_location(conn, profile):
    add(conn, 1, dims(90), states=("CO", "WA", "TX"), scope="multi_state", source="fed")
    item = inbox.inbox_items(conn, profile).buckets["A"][0]
    assert item.source_badge == "federal"
    assert item.location == "Town, CO +2 more"


def test_state_filter_multi_state_and_remote(conn, profile):
    add(conn, 1, dims(90), states=("CO",), title="co only")
    add(conn, 2, dims(90), states=("WA", "CO"), scope="multi_state", title="wa+co")
    add(conn, 3, dims(90), states=("TX",), title="tx only")
    add(conn, 4, dims(90), states=(None,), scope="remote_us", title="remote")

    def titles(**kw):
        data = inbox.inbox_items(conn, profile, **kw)
        return {i.title for items in data.buckets.values() for i in items}

    assert titles(state="WA") == {"wa+co", "remote"}
    assert titles(state="CO") == {"co only", "wa+co", "remote"}
    # TX is outside states_allowed: its own job only, remote jobs do not follow
    assert titles(state="TX") == {"tx only"}


def test_label_interesting_creates_application(seeded, profile):
    inbox.set_label(seeded, 1, "interesting")
    label = seeded.execute("SELECT label FROM label WHERE job_group_id = 1").fetchone()[0]
    assert label == "interesting"
    app = seeded.execute("SELECT * FROM application WHERE job_group_id = 1").fetchone()
    assert app["status"] == "interested"
    events = seeded.execute(
        "SELECT status FROM application_event WHERE application_id = ?", (app["id"],)
    ).fetchall()
    assert [e["status"] for e in events] == ["interested"]
    # triaged items are hidden by default, available on request
    assert inbox.inbox_items(seeded, profile).counts["A"] == 0
    assert inbox.inbox_items(seeded, profile, include_triaged=True).counts["A"] == 1


def test_label_upsert_and_dismiss(seeded):
    inbox.set_label(seeded, 2, "not_interesting")
    inbox.set_label(seeded, 2, "interesting")
    inbox.set_label(seeded, 2, "interesting")
    assert seeded.execute("SELECT COUNT(*) FROM label WHERE job_group_id = 2").fetchone()[0] == 1
    assert seeded.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 1
    inbox.set_label(seeded, 3, "not_interesting")
    assert seeded.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 1


def test_undo_removes_label_and_application(seeded, profile):
    inbox.set_label(seeded, 1, "interesting")
    inbox.undo_label(seeded, 1)
    for table in ("label", "application", "application_event"):
        assert seeded.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert inbox.inbox_items(seeded, profile).counts["A"] == 1


def test_undo_keeps_progressed_application(seeded):
    inbox.set_label(seeded, 1, "interesting")
    app_id = seeded.execute("SELECT id FROM application").fetchone()[0]
    seeded.execute(
        "INSERT INTO application_event (application_id, at, status) VALUES (?, ?, 'preparing')",
        (app_id, NOW.isoformat()),
    )
    inbox.undo_label(seeded, 1)
    assert seeded.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0
    assert seeded.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 1


# ─── HTTP ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, profile_dir, seeded):
    settings = Settings(paths=Paths(profile_dir=profile_dir, db_path=tmp_path / "t.db"))
    return TestClient(create_app(settings, lambda: db.connect(tmp_path / "t.db")))


def test_inbox_page_markup(client):
    r = client.get("/inbox")
    assert r.status_code == 200
    t = r.text
    assert "A &middot; BULLSEYE" in t and "apply, minimal tailoring" in t
    assert "Bullseye" in t and "&#9873; on_call_heavy" in t
    assert "salary not stated" in t and "partial: open to confirm" in t
    assert "unverified evidence" in t
    assert "recent-skills 90" in t
    assert "2 jobs matched on skills you last used 7+ years ago" in t
    assert "VMware (2)" in t
    assert "Mismatch" not in t  # bucket G rows hidden
    assert "inbox.js" in t and "<kbd>j</kbd>" in t
    assert "Connecting Colorado" in t


def test_inbox_bucket_and_state_params(client):
    assert "Mismatch" in client.get("/inbox?bucket=G").text
    assert "Bullseye" not in client.get("/inbox?state=TX").text


def test_label_and_undo_routes(client):
    r = client.post("/inbox/1/label?label=interesting")
    assert r.status_code == 200
    assert 'id="row-1"' in r.text and "Shortlisted: Bullseye" in r.text
    assert "/inbox/1/undo" in r.text
    assert "Bullseye" not in client.get("/inbox").text
    r = client.post("/inbox/1/undo")
    assert r.status_code == 200
    assert 'id="row-1"' in r.text and "Bullseye" in r.text and "shortlist" in r.text
    assert "Bullseye" in client.get("/inbox").text
    assert client.post("/inbox/1/label?label=applied").status_code == 422
    assert client.post("/inbox/999/label?label=interesting").status_code == 404


def test_no_profile_banner(tmp_path, seeded):
    settings = Settings(paths=Paths(profile_dir=tmp_path / "missing", db_path=tmp_path / "t.db"))
    c = TestClient(create_app(settings, lambda: db.connect(tmp_path / "t.db")))
    r = c.get("/inbox")
    assert r.status_code == 200
    assert "preferences.yaml" in r.text
    assert "Bullseye" not in r.text
