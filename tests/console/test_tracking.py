"""Application tracking: events, derived status, pipeline, follow-ups, contacts, attachments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Settings
from jobhunter.console import tracking as t
from jobhunter.console.app import create_app
from jobhunter.core import db

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
HX = {"HX-Request": "true"}


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "t.db"
    c = db.connect(p)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Connecting Colorado', 'x', 'http', 'https://example.com', 'enabled')"
    )
    c.commit()
    c.close()
    return p


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


def make_app(conn, n, status="interested", at=None):
    """Job n, its group, and an application whose first event is ``status`` at ``at``."""
    ts = NOW.isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, location_scope, "
        "first_seen_at, last_seen_at) VALUES (?, 'co', ?, ?, ?, 'Acme', 'single', ?, ?)",
        (n, str(n), f"https://example.com/{n}", f"Job {n}", ts, ts),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, 'CO', 'Denver', 1)",
        (n,),
    )
    cur = conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, 'interested', ?, ?)",
        (n, ts, ts),
    )
    app_id = cur.lastrowid
    conn.commit()
    t.add_event(conn, app_id, "interested", "shortlisted", at or ago(40))
    if status != "interested":
        t.add_event(conn, app_id, status, None, at or ago(40))
    return app_id


@pytest.fixture
def client(db_path):
    return TestClient(create_app(Settings(), lambda: db.connect(db_path), clock=lambda: NOW))


def status_of(conn, app_id):
    return conn.execute("SELECT status FROM application WHERE id = ?", (app_id,)).fetchone()[0]


# --- events -----------------------------------------------------------------------------


def test_event_append_and_derived_status(conn):
    a = make_app(conn, 1)
    t.add_event(conn, a, "applied", "via board", ago(10))
    t.add_event(conn, a, "acknowledged", None, ago(5))
    assert status_of(conn, a) == "acknowledged"
    row = conn.execute("SELECT applied_at FROM application WHERE id = ?", (a,)).fetchone()
    assert row[0] == ago(10).isoformat()
    assert [e["status"] for e in t.events(conn, a)] == ["acknowledged", "applied", "interested"]


def test_status_follows_latest_event_by_time_and_rebuilds(conn):
    a = make_app(conn, 1)
    t.add_event(conn, a, "applied", None, ago(10))
    t.add_event(conn, a, "preparing", "backfilled", ago(20))  # older than 'applied'
    assert status_of(conn, a) == "applied"
    conn.execute("UPDATE application SET status = 'closed'")
    assert t.rebuild_status(conn, a) == "applied"
    assert status_of(conn, a) == "applied"


def test_past_events_never_mutated(conn):
    a = make_app(conn, 1)
    t.add_event(conn, a, "applied", "first", ago(10))
    before = [tuple(r) for r in conn.execute("SELECT * FROM application_event ORDER BY id")]
    t.add_event(conn, a, "rejected", "no thanks", ago(1))
    t.add_event(conn, a, "applied", "re-opened", ago(0.5))
    after = [tuple(r) for r in conn.execute("SELECT * FROM application_event ORDER BY id")]
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2


def test_add_event_rejects_bad_input(conn):
    a = make_app(conn, 1)
    with pytest.raises(t.TrackingError):
        t.add_event(conn, a, "bogus")
    with pytest.raises(t.TrackingError):
        t.add_event(conn, 999, "applied")
    with pytest.raises(t.TrackingError):
        t.add_event(conn, a, "applied", at="not a date")


# --- staleness and pipeline ----------------------------------------------------------------


def test_days_in_status_counts_from_start_of_run(conn):
    a = make_app(conn, 1)
    t.add_event(conn, a, "applied", None, ago(30))
    t.add_event(conn, a, "applied", "note only", ago(2))
    assert t.days_in_status(conn, a, NOW) == 30


@pytest.mark.parametrize(
    ("status", "days", "stale"),
    [
        ("applied", 20, False),
        ("applied", 21, True),
        ("acknowledged", 14, True),
        ("screening", 9, False),
        ("interview", 7, True),
        ("offer", 5, True),
        ("interested", 400, False),
        ("rejected", 400, False),
    ],
)
def test_staleness_thresholds(status, days, stale):
    assert t.is_stale(status, days) is stale


def test_pipeline_grouping_and_closed_lane(conn):
    ids = {
        s: make_app(conn, i + 1, s, ago(3))
        for i, s in enumerate(
            ["interested", "applied", "interview", "rejected", "withdrawn", "no_response", "closed"]
        )
    }
    board = t.pipeline(conn, NOW)
    assert list(board.columns) == list(t.BOARD_STATUSES)
    assert [c.app_id for c in board.columns["applied"]] == [ids["applied"]]
    assert board.columns["preparing"] == []
    assert sorted(c.status for c in board.closed) == [
        "closed",
        "no_response",
        "rejected",
        "withdrawn",
    ]
    c = board.columns["interview"][0]
    assert (c.title, c.employer, c.location, c.days, c.stale) == (
        "Job 3",
        "Acme",
        "Denver, CO",
        3,
        False,
    )


def test_card_amber_when_stale(conn):
    make_app(conn, 1, "applied", ago(25))
    make_app(conn, 2, "applied", ago(5))
    cards = {c.title: c for c in t.pipeline(conn, NOW).columns["applied"]}
    assert cards["Job 1"].stale and not cards["Job 2"].stale


# --- follow-ups ----------------------------------------------------------------------------


def test_followups_due_overdue_and_nudge(conn):
    overdue = make_app(conn, 1, "applied", ago(5))
    today = make_app(conn, 2, "applied", ago(5))
    future = make_app(conn, 3, "applied", ago(5))
    nudge = make_app(conn, 4, "applied", ago(22))
    fresh = make_app(conn, 5, "applied", ago(20))
    closed = make_app(conn, 6, "rejected", ago(5))
    t.set_next_action(conn, overdue, "email recruiter", "2026-10-01")
    t.set_next_action(conn, today, "call", "2026-10-09")
    t.set_next_action(conn, future, "later", "2026-10-10")
    t.set_next_action(conn, closed, "ignored", "2026-10-01")
    items = t.followups(conn, NOW)
    got = [(i.app_id, i.kind) for i in items]
    assert got == [(overdue, "action"), (nudge, "nudge"), (today, "action")]
    assert fresh not in [i.app_id for i in items]
    assert items[0].overdue_days == 8 and items[2].overdue_days == 0
    assert "no_response" in items[1].text


def test_nudge_clears_after_a_later_event(conn):
    a = make_app(conn, 1, "applied", ago(30))
    assert [i.kind for i in t.followups(conn, NOW)] == ["nudge"]
    t.add_event(conn, a, "acknowledged", None, ago(1))
    assert t.followups(conn, NOW) == []


def test_done_and_snooze(conn):
    a = make_app(conn, 1, "applied", ago(2))
    t.set_next_action(conn, a, "ping", "2026-10-01")
    t.snooze(conn, a, NOW)
    row = conn.execute("SELECT next_action, next_action_at FROM application").fetchone()
    assert tuple(row) == ("ping", "2026-10-12")
    assert t.followups(conn, NOW) == []
    t.done(conn, a)
    row = conn.execute("SELECT next_action, next_action_at FROM application").fetchone()
    assert tuple(row) == (None, None)


def test_bad_next_action_date(conn):
    a = make_app(conn, 1)
    with pytest.raises(t.TrackingError):
        t.set_next_action(conn, a, "x", "soon")


# --- contacts ------------------------------------------------------------------------------


def test_contacts_crud(conn):
    a = make_app(conn, 1)
    cid = t.add_contact(conn, a, name="Pat Doe", role="recruiter", email="<you>@example.com")
    assert [r["name"] for r in t.contacts(conn, a)] == ["Pat Doe"]
    t.update_contact(conn, cid, name="Pat Roe", role=" ", email=None, phone="555-0100", note="")
    row = t.contacts(conn, a)[0]
    assert (row["name"], row["role"], row["email"], row["phone"], row["note"]) == (
        "Pat Roe",
        None,
        None,
        "555-0100",
        None,
    )
    with pytest.raises(t.TrackingError):
        t.add_contact(conn, a, name=" ")
    with pytest.raises(t.TrackingError):
        t.update_contact(conn, 999, name="x")
    t.delete_contact(conn, cid)
    assert t.contacts(conn, a) == []


# --- attachments ---------------------------------------------------------------------------


def test_attachment_stores_path_only(conn, tmp_path):
    a = make_app(conn, 1)
    f = tmp_path / "resume.pdf"
    f.write_bytes(b"%PDF fake")
    aid = t.add_attachment(conn, a, "resume", str(f), NOW)
    row = t.attachments(conn, a)[0]
    assert (row["id"], row["kind"], row["path"]) == (aid, "resume", str(f.resolve()))
    assert [p.name for p in tmp_path.glob("*.pdf")] == ["resume.pdf"]  # nothing copied
    t.delete_attachment(conn, aid)
    assert t.attachments(conn, a) == [] and f.exists()


def test_attachment_nonexistent_rejected(conn, tmp_path):
    a = make_app(conn, 1)
    with pytest.raises(t.TrackingError, match="no such file"):
        t.add_attachment(conn, a, "other", str(tmp_path / "missing.pdf"))
    with pytest.raises(t.TrackingError):
        t.add_attachment(conn, a, "other", str(tmp_path))  # directory
    with pytest.raises(t.TrackingError):
        t.add_attachment(conn, a, "bogus-kind", str(tmp_path))


def test_attachment_repo_tracked_path_rejected(conn):
    a = make_app(conn, 1)
    tracked = t.REPO_ROOT / "pyproject.toml"
    assert tracked.is_file()
    with pytest.raises(t.TrackingError, match="tracked tree"):
        t.add_attachment(conn, a, "other", str(tracked))
    assert t.attachments(conn, a) == []


def test_attachment_untracked_file_inside_repo_allowed(conn, tmp_path):
    import subprocess

    a = make_app(conn, 1)
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    (tmp_path / "repo" / "ignored.txt").write_text("x")
    # untracked inside a repo is fine; a tracked one is not
    t.add_attachment(
        conn, a, "other", str(tmp_path / "repo" / "ignored.txt"), repo_root=tmp_path / "repo"
    )
    subprocess.run(["git", "-C", str(tmp_path / "repo"), "add", "ignored.txt"], check=True)
    with pytest.raises(t.TrackingError):
        t.add_attachment(
            conn, a, "other", str(tmp_path / "repo" / "ignored.txt"), repo_root=tmp_path / "repo"
        )


# --- routes --------------------------------------------------------------------------------


def test_pipeline_page_renders(client, conn):
    make_app(conn, 1, "applied", ago(25))
    make_app(conn, 2, "rejected", ago(3))
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "Job 1" in r.text and "Job 2" in r.text
    assert 'class="pcard stale"' in r.text
    assert 'id="cards-closed"' in r.text
    assert "tracking.js" in r.text and "htmx-2.0.4.min.js" in r.text


def test_move_endpoint_appends_event_and_returns_card(client, conn):
    a = make_app(conn, 1, "applied", ago(2))
    r = client.post(f"/pipeline/{a}/move?to=acknowledged", headers=HX)
    assert r.status_code == 200
    assert f'id="card-{a}"' in r.text
    assert "beforeend:#cards-acknowledged" in r.text
    assert 'hx-swap-oob="delete"' in r.text
    assert status_of(conn, a) == "acknowledged"
    evs = t.events(conn, a)
    assert evs[0]["status"] == "acknowledged" and len(evs) == 3
    # closed statuses land in the closed lane
    r = client.post(f"/pipeline/{a}/move?to=rejected", headers=HX)
    assert "beforeend:#cards-closed" in r.text
    assert client.post(f"/pipeline/{a}/move?to=nope").status_code == 422
    assert client.post("/pipeline/999/move?to=applied").status_code == 404


def test_move_without_htmx_redirects(client, conn):
    a = make_app(conn, 1)
    r = client.post(f"/pipeline/{a}/move?to=preparing", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pipeline"
    assert status_of(conn, a) == "preparing"


def test_drawer_and_edits(client, conn, tmp_path):
    a = make_app(conn, 1, "applied", ago(3))
    r = client.get(f"/pipeline/{a}", headers=HX)
    assert r.status_code == 200 and "History" in r.text and "shortlisted" in r.text
    assert "<html" in client.get(f"/pipeline/{a}").text  # full page when not htmx
    assert client.get("/pipeline/999").status_code == 404

    def post(path, body):
        return client.post(
            f"/pipeline/{a}/{path}",
            content=body,
            headers={**HX, "content-type": "application/x-www-form-urlencoded"},
        )

    assert (
        post("next-action", "next_action=email+them&next_action_at=2026-10-20").status_code == 200
    )
    assert post("details", "resume_version=v3&external_ref=REQ-9").status_code == 200
    row = conn.execute("SELECT * FROM application WHERE id = ?", (a,)).fetchone()
    assert (row["next_action"], row["next_action_at"]) == ("email them", "2026-10-20")
    assert (row["resume_version"], row["external_ref"]) == ("v3", "REQ-9")

    r = post("event", "status=acknowledged&note=got+an+email")
    assert "got an email" in r.text and status_of(conn, a) == "acknowledged"

    assert post("contacts", "name=Pat&role=recruiter").status_code == 200
    cid = t.contacts(conn, a)[0]["id"]
    assert post(f"contacts/{cid}/update", "name=Pat+R").status_code == 200
    assert t.contacts(conn, a)[0]["name"] == "Pat R"
    post(f"contacts/{cid}/delete", "")
    assert t.contacts(conn, a) == []

    f = tmp_path / "cl.txt"
    f.write_text("hi")
    post("attachments", f"kind=cover_letter&path={f}")
    assert len(t.attachments(conn, a)) == 1
    r = post("attachments", f"kind=other&path={tmp_path / 'nope'}")
    assert "no such file" in r.text and len(t.attachments(conn, a)) == 1
    post(f"attachments/{t.attachments(conn, a)[0]['id']}/delete", "")
    assert t.attachments(conn, a) == []


def test_followups_page_and_actions(client, conn):
    a = make_app(conn, 1, "applied", ago(5))
    b = make_app(conn, 2, "applied", ago(30))
    t.set_next_action(conn, a, "email recruiter", "2026-10-01")
    r = client.get("/followups")
    assert r.status_code == 200
    assert "email recruiter" in r.text and "10d overdue" not in r.text and "8d overdue" in r.text
    assert "follow up or mark no_response" in r.text
    assert r.text.index("Job 2") < r.text.index("Job 1")  # date sorted

    assert client.post(f"/followups/{a}/snooze", headers=HX).status_code == 200
    assert "email recruiter" not in client.get("/followups").text
    assert client.post(f"/followups/{a}/done", headers=HX).text == ""
    assert (
        conn.execute("SELECT next_action FROM application WHERE id = ?", (a,)).fetchone()[0] is None
    )
    assert client.post(f"/followups/{b}/no-response", headers=HX).status_code == 200
    assert status_of(conn, b) == "no_response"
    assert "Job 2" not in client.get("/followups").text
    assert client.post("/followups/999/done").status_code == 404
