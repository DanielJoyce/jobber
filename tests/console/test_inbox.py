"""Inbox: bucket grouping, filters, label writes and the HTML surface. No network."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from stray_letters import stray_letters

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
    assert "Bullseye &mdash; apply, minimal tailoring" in t and "apply, minimal tailoring" in t
    assert ">Bullseye</a>" in t and "&#9873; on_call_heavy" in t
    assert "salary not stated" in t and "partial: open to confirm" in t
    assert "unverified evidence" in t
    assert "recent-skills 90" in t
    assert "2 jobs matched on skills you last used 7+ years ago" in t
    assert "VMware (2)" in t
    assert ">Mismatch</a>" not in t  # bucket G rows hidden
    assert "inbox.js" in t and "<kbd>j</kbd>" in t
    assert "Connecting Colorado" in t


def test_inbox_bucket_and_state_params(client):
    assert ">Mismatch</a>" in client.get("/inbox?bucket=G").text
    assert ">Bullseye</a>" not in client.get("/inbox?state=TX").text


def test_shortlist_toast_links_to_the_pipeline_but_dismiss_does_not(client):
    # The shortlist is otherwise invisible: say where it went.
    r = client.post("/inbox/1/label?label=interesting")
    assert '<a href="/pipeline">see it in Pipeline</a>' in r.text
    assert client.get("/pipeline").status_code == 200
    r = client.post("/inbox/2/label?label=not_interesting")
    assert "Pipeline" not in r.text
    r = client.post("/inbox/bulk", data={"label": "interesting", "group_id": [3, 4]})
    assert r.text.count('<a href="/pipeline">see it in Pipeline</a>') == 2


def test_label_and_undo_routes(client):
    r = client.post("/inbox/1/label?label=interesting")
    assert r.status_code == 200
    assert 'id="row-1"' in r.text and "Shortlisted: Bullseye" in r.text
    assert "/inbox/1/undo" in r.text
    assert ">Bullseye</a>" not in client.get("/inbox").text
    r = client.post("/inbox/1/undo")
    assert r.status_code == 200
    assert 'id="row-1"' in r.text and ">Bullseye</a>" in r.text and "shortlist" in r.text
    assert ">Bullseye</a>" in client.get("/inbox").text
    assert client.post("/inbox/1/label?label=applied").status_code == 422
    assert client.post("/inbox/999/label?label=interesting").status_code == 404


def test_set_labels_bulk_and_undo(seeded):
    inbox.set_labels(seeded, [1, 2, 3], "interesting")
    assert seeded.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 3
    assert seeded.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 3
    inbox.set_labels(seeded, [2, 3], "not_interesting")  # relabel keeps the applications
    assert seeded.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 3
    inbox.undo_labels(seeded, [1, 2, 3])
    for table in ("label", "application", "application_event"):
        assert seeded.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_set_labels_is_atomic(seeded):
    # group 999 has no job_group row: the FK on label fails after 1 and 2 were written
    with pytest.raises(Exception):  # noqa: B017 - any DB error must roll back everything
        inbox.set_labels(seeded, [1, 2, 999], "interesting")
    assert not seeded.in_transaction
    for table in ("label", "application", "application_event"):
        assert seeded.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(ValueError):
        inbox.set_labels(seeded, [1], "applied")


def test_bulk_routes(client):
    r = client.post("/inbox/bulk", data={"label": "not_interesting", "group_id": [1, 2, 2]})
    assert r.status_code == 200
    assert r.text.count('hx-swap-oob="true"') == 2  # two rows (deduped)
    # The toast goes into the page's standing role=status node. A replacement node (outerHTML)
    # is not announced by screen readers.
    assert r.text.count('hx-swap-oob="innerHTML:#bulk-toast"') == 1
    assert 'id="bulk-toast"' not in r.text and 'role="status"' not in r.text
    assert "Dismissed: Bullseye" in r.text and "Dismissed: Strong" in r.text
    assert 'name="group_id" value="1"' in r.text and "/inbox/bulk/undo" in r.text
    page = client.get("/inbox").text
    assert ">Bullseye</a>" not in page and ">Strong</a>" not in page
    r = client.post("/inbox/bulk/undo", data={"group_id": [1, 2]})
    assert r.status_code == 200
    assert 'id="row-1"' in r.text and ">Bullseye</a>" in r.text and ">Strong</a>" in r.text
    assert "Undone: 2 jobs restored." in r.text
    assert r.text.count('hx-swap-oob="innerHTML:#bulk-toast"') == 1
    assert 'id="bulk-toast"' not in r.text and 'role="status"' not in r.text
    assert ">Bullseye</a>" in client.get("/inbox").text


def test_bulk_shortlist_creates_applications(client, tmp_path):
    client.post("/inbox/bulk", data={"label": "interesting", "group_id": [1, 2]})
    c = db.connect(tmp_path / "t.db")
    assert c.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 2
    client.post("/inbox/bulk/undo", data={"group_id": [1, 2]})
    assert c.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 0


def test_bulk_validation(client, tmp_path):
    ok = {"label": "interesting"}
    assert (
        client.post("/inbox/bulk", data={**ok, "group_id": [1], "label": "applied"}).status_code
        == 422
    )
    assert client.post("/inbox/bulk", data=ok).status_code == 422  # empty selection
    assert client.post("/inbox/bulk", data={**ok, "group_id": ["x"]}).status_code == 422
    over = list(range(1, inbox.BULK_MAX + 2))
    assert client.post("/inbox/bulk", data={**ok, "group_id": over}).status_code == 422
    # unknown id: 404 and nothing applied, even for the valid ids in the request
    assert client.post("/inbox/bulk", data={**ok, "group_id": [1, 999]}).status_code == 404
    c = db.connect(tmp_path / "t.db")
    assert c.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0
    assert client.post("/inbox/bulk/undo", data={}).status_code == 422
    assert client.post("/inbox/bulk/undo", data={"group_id": [999]}).status_code == 404


def test_page_has_bulk_controls(client):
    t = client.get("/inbox").text
    assert t.count('id="bulk-toast"') == 1  # the one standing live region the toasts swap into
    assert 'id="bulk-toast" class="bulk-toast" role="status"' in t
    assert 'id="select-all"' in t and 'id="bulk-bar"' in t and "Clear selection" in t
    assert 'type="checkbox" class="sel" name="group_id" value="1"' in t
    assert "Select Bullseye" in t


def test_no_profile_banner(tmp_path, seeded):
    settings = Settings(paths=Paths(profile_dir=tmp_path / "missing", db_path=tmp_path / "t.db"))
    c = TestClient(create_app(settings, lambda: db.connect(tmp_path / "t.db")))
    r = c.get("/inbox")
    assert r.status_code == 200
    assert "preferences.yaml" in r.text
    assert ">Bullseye</a>" not in r.text


# ─── Employer rejections ────────────────────────────────────────────────────


def _reject(conn, title, gid=None, days_ago=5):
    from jobhunter.core import rejections

    rejections.record(
        conn,
        received_at=(NOW - timedelta(days=days_ago)).isoformat(),
        employer="Acme Inc",
        title=title,
        source="email",
        now=NOW,
        job_group_id=gid,
        evidence={"sender": "Acme <no-reply@ashbyhq.com>", "subject": "Your application"},
    )


def test_triaged_param_lists_shortlisted_and_dismissed_groups_with_a_badge(client):
    client.post("/inbox/1/label?label=interesting")
    client.post("/inbox/2/label?label=not_interesting")
    plain = client.get("/inbox?bucket=A,B").text
    assert "badge-triaged" not in plain  # untriaged lists show no triage badges
    t = client.get("/inbox?bucket=A,B&triaged=1").text
    shortlisted = r'<article class="row readonly" id="row-1".*?badge-triaged"[^>]*>shortlisted<'
    dismissed = r'<article class="row readonly" id="row-2".*?badge-triaged"[^>]*>dismissed<'
    assert re.search(shortlisted, t, re.S)
    assert re.search(dismissed, t, re.S)
    assert "badge-triaged" in t and t.count("badge-triaged") == 2


def _row_html(page: str, gid: int) -> str:
    return re.search(rf'<article[^>]*id="row-{gid}".*?</article>', page, re.S).group(0)


def test_triaged_rows_are_read_only_but_untriaged_rows_keep_their_controls(client):
    client.post("/inbox/1/label?label=interesting")
    client.post("/inbox/2/label?label=not_interesting")
    t = client.get("/inbox?bucket=A,B&triaged=1").text
    for gid in (1, 2):
        row = _row_html(t, gid)
        assert 'class="sel"' not in row and "/label?label=" not in row
        assert "shortlist (s)" not in row and "dismiss (x)" not in row
        assert "posting (o)" in row
    # an untriaged row in the same kind of list still has them
    other = client.get("/inbox?bucket=F&triaged=1").text
    assert re.findall(r'<article class="row" id="row-(\d+)"', other)
    plain = _row_html(other, int(re.findall(r'<article class="row" id="row-(\d+)"', other)[0]))
    assert 'class="sel"' in plain and "shortlist (s)" in plain


def test_relabel_and_bulk_relabel_refuse_triaged_groups_and_keep_the_application(client, seeded):
    client.post("/inbox/1/label?label=interesting")
    r = client.post("/inbox/1/label?label=not_interesting")
    assert r.status_code == 409
    r = client.post("/inbox/bulk", data={"label": "not_interesting", "group_id": [1, 2]})
    assert r.status_code == 409
    assert seeded.execute("SELECT label FROM label WHERE job_group_id = 1").fetchone()[0] == (
        "interesting"
    )
    assert seeded.execute("SELECT COUNT(*) FROM label WHERE job_group_id = 2").fetchone()[0] == 0


def test_undo_keeps_an_application_the_shortlist_press_did_not_open(client, seeded):
    seeded.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (1, 'interested', 'x', 'x')"
    )
    seeded.execute(
        "INSERT INTO application_event (application_id, at, status, note, source) "
        "SELECT id, 'x', 'interested', 'added from job page', 'manual' FROM application"
    )
    assert client.post("/inbox/1/label?label=interesting").status_code == 200
    assert client.post("/inbox/1/undo").status_code == 200
    assert seeded.execute("SELECT COUNT(*) FROM application WHERE job_group_id = 1").fetchone()[0]
    assert seeded.execute("SELECT COUNT(*) FROM label WHERE job_group_id = 1").fetchone()[0] == 0


def test_undo_without_a_label_keeps_the_application(client, seeded):
    client.post("/inbox/1/label?label=interesting")
    seeded.execute("DELETE FROM label WHERE job_group_id = 1")  # e.g. a stale second undo
    assert client.post("/inbox/1/undo").status_code == 200
    assert client.post("/inbox/bulk/undo", data={"group_id": [1]}).status_code == 200
    apps = seeded.execute("SELECT status FROM application WHERE job_group_id = 1").fetchall()
    assert [a[0] for a in apps] == ["interested"]


def test_triaged_param_still_hides_rejected_postings_you_never_acted_on(client, seeded):
    _reject(seeded, "Bullseye", gid=1)
    _reject(seeded, "Strong", gid=2)
    client.post("/inbox/2/label?label=interesting")
    t = client.get("/inbox?bucket=A,B&triaged=1").text
    assert 'id="row-1"' not in t  # rejected, never triaged: left out as in the plain inbox
    assert 'id="row-2"' in t  # rejected but shortlisted: you are tracking it


def test_a_capped_bucket_says_how_many_rows_it_is_showing(client, seeded):
    assert "Showing the first" not in client.get("/inbox").text
    for n in range(100, 100 + inbox.BULK_MAX + 1):
        add(seeded, n, dims(90), title=f"Extra {n}")
    seeded.commit()
    t = client.get("/inbox?bucket=A").text
    assert f"Showing the first {inbox.BULK_MAX} of {inbox.BULK_MAX + 1 + 1}." in t


def test_rejected_posting_left_out_and_other_roles_flagged(seeded, profile):
    _reject(seeded, "Bullseye", gid=1)
    data = inbox.inbox_items(seeded, profile)
    shown = [i.group_id for items in data.buckets.values() for i in items]
    assert 1 not in shown
    strong = data.buckets["B"][0]
    day = (NOW - timedelta(days=5)).date().isoformat()
    assert strong.employer_rejection == f"employer rejected you for Bullseye on {day}"
    # Outside the window: no flag.
    data = inbox.inbox_items(seeded, profile, rejection_days=1)
    assert data.buckets["B"][0].employer_rejection is None


def test_unmatched_rejection_excludes_by_employer_and_title(seeded, profile):
    _reject(seeded, "Stale one")  # no job group id: matched on Acme + title
    data = inbox.inbox_items(seeded, profile)
    assert "Stale one" not in [i.title for i in data.buckets["F"]]


def test_rejections_page_lists_and_adds_manual(client, seeded):
    _reject(seeded, "Bullseye", gid=1)
    seeded.commit()
    r = client.get("/rejections")
    assert r.status_code == 200
    assert "Employer rejections (1)" in r.text
    assert 'href="/job/1"' in r.text and "Acme Inc" in r.text
    assert 'href="/rejections"' in r.text  # in the nav
    r = client.post(
        "/rejections",
        data={"employer": "Globex", "title": "Analyst", "date": "2026-09-30", "group_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = seeded.execute("SELECT * FROM rejection WHERE employer = 'Globex'").fetchone()
    assert (row["source"], row["title"], row["received_at"][:10]) == (
        "manual",
        "Analyst",
        "2026-09-30",
    )
    assert row["gmail_message_id"] is None and row["employer_norm"] == "globex"
    assert "no matching job in jobhunter" in client.get("/rejections").text


def test_rejections_form_validation_and_delete(client, seeded):
    assert client.post("/rejections", data={"employer": ""}).status_code == 422
    assert client.post("/rejections", data={"employer": "X", "date": "nope"}).status_code == 422
    assert client.post("/rejections", data={"employer": "X", "group_id": "999"}).status_code == 422
    r = client.post(
        "/rejections",
        data={"employer": "Acme", "title": "Bullseye", "group_id": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    (rid,) = seeded.execute("SELECT id FROM rejection").fetchone()
    assert 'id="row-1"' not in client.get("/inbox").text
    assert "rejected you for this posting" in client.get("/job/1").text
    assert "employer rejected you for Bullseye" in client.get("/job/2").text
    assert client.post(f"/rejections/{rid}/delete", follow_redirects=False).status_code == 303
    assert client.post(f"/rejections/{rid}/delete").status_code == 404
    assert 'id="row-1"' in client.get("/inbox").text


def test_rejected_page_is_still_the_prefilter_page(client):
    # /rejected (jobs we filtered out) is unchanged and distinct from /rejections.
    assert client.get("/rejected").status_code == 200
    assert "Employer rejections" not in client.get("/rejected").text.split("<main")[-1]


# ─── multi-bucket filter (?bucket=A,B) ─────────────────────────────────────


def test_inbox_items_accepts_several_buckets(seeded, profile):
    data = inbox.inbox_items(seeded, profile, bucket="A,B")
    assert data.shown == ["A", "B"]
    assert [i.title for i in data.buckets["A"]] == ["Bullseye"]
    assert [i.title for i in data.buckets["B"]] == ["Strong"]
    assert data.buckets["F"] == []
    assert inbox.inbox_items(seeded, profile, bucket="b,zz").shown == ["B"]
    assert "G" not in inbox.inbox_items(seeded, profile, bucket="nonsense").shown


def test_inbox_page_multi_bucket_chips(client):
    t = client.get("/inbox?bucket=A,B").text
    assert 'id="bucket-A"' in t and 'id="bucket-B"' in t and 'id="bucket-F"' not in t
    assert 'class="chip on" data-bucket="A" href="#bucket-A"' in t
    assert 'class="chip on" data-bucket="B" href="#bucket-B"' in t
    # Buckets not on the page link to that bucket (a dead #anchor would do nothing).
    assert 'data-bucket="F" href="/inbox?bucket=F"' in t


def _chips(html: str) -> dict[str, str]:
    """data-bucket letter -> the chip's visible text."""
    found = re.findall(r'<a class="chip[^"]*" data-bucket="([A-G])"[^>]*>(.*?)</a>', html, re.S)
    return {
        b: " ".join(re.sub(r"<[^>]+>", " ", text).replace("&middot;", "·").split())
        for b, text in found
    }


def test_inbox_chips_read_bucket_names_with_counts(client):
    # The chips, not the job titles in the rows below (seeded as "Bullseye" and "Mismatch").
    assert _chips(client.get("/inbox").text) == {
        "A": "Bullseye 1",
        "B": "Strong 1",
        "C": "Stretch Up 0",
        "D": "Lateral 0",
        "E": "Downlevel 0",
        "F": "Stale Match 2",
        "G": "Mismatch ·hidden· 1",
    }
    picked = _chips(client.get("/inbox?bucket=A,B").text)  # chips for sections off the page too
    assert picked["A"] == "Bullseye 1" and picked["F"] == "Stale Match 2"


def test_inbox_chips_keep_the_state_filter(client):
    t = client.get("/inbox?state=CO&bucket=A,B").text
    assert 'data-bucket="D" href="/inbox?state=CO&amp;bucket=D"' in t
    assert 'data-bucket="G" href="/inbox?state=CO&amp;bucket=G"' in t
    assert 'href="#bucket-D"' not in t and 'href="#bucket-G"' not in t
    plain = client.get("/inbox").text  # nothing picked: every non-hidden section is on the page
    assert 'href="#bucket-D"' in plain
    assert 'data-bucket="G" href="/inbox?bucket=G"' in plain
    assert "toggle Stale Match" in plain


# ─── stray bucket letters ──────────────────────────────────────────────────

_PAGES = [
    "/",
    "/dash/state-table?buckets=A,B",
    "/dash/kpis",
    "/inbox",
    "/inbox?bucket=A,B",
    "/inbox?bucket=G",
    "/job/1",
    "/job/3",
    "/search?bucket_from=A&bucket_to=C",
    "/prefs",
    "/pipeline",
    "/costs",
    "/rejections",
    "/proposals",
    "/alerts",
]


def test_stray_scanner_catches_known_bad_patterns():
    assert "letters joined with +" in stray_letters("<span>New A+B</span>")
    assert "letters joined with +" in stray_letters("<td>A + B</td>")
    assert "'bucket X'" in stray_letters("<span>bucket B</span>")
    assert "letter arrow letter" in stray_letters('<span class="muted">B → A</span>')
    assert "letter arrow letter" in stray_letters("<li>D &rarr; A</li>")
    assert stray_letters('<span title="Bucket B">Strong</span>') == []
    assert "letter then count" in stray_letters(
        '<a class="chip"><b>B 12</b></a>'.replace("<b>", ">")
    )
    assert "bare letter element" in stray_letters("<td>C</td>")
    assert stray_letters('<span class="bucket-badge" title="Bucket B">B</span> Strong') == []
    assert stray_letters("<script>var x = 'A+B';</script>") == []


@pytest.mark.parametrize("path", _PAGES)
def test_pages_show_bucket_names_not_letters(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    assert stray_letters(r.text) == [], path


def test_text_salary_hint_is_announced_to_screen_readers(client, seeded):
    seeded.execute("UPDATE job SET salary_source = 'text' WHERE id = 1")
    t = client.get("/inbox").text
    assert '<span aria-hidden="true">~</span>' in t
    assert '<span class="sr-only">(parsed from the description)</span>' in t
