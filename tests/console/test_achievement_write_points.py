"""Achievements write points (specs/021 "Data" table, ticket A1).

Every ``application_event`` insert sets ``recorded_at``; follow-ups keep a ``followup_log``
(planned only on a new date, done only when a dated action existed, no-response in one
transaction); contacts get ``created_at``; confirming a rejection sets ``confirmed_at``; the
nightly merge and the shortlist undo handle ``followup_log``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunter.apply import packets
from jobhunter.config import Settings
from jobhunter.console import detail, inbox
from jobhunter.console import tracking as t
from jobhunter.console.app import create_app
from jobhunter.core import db, rejections
from jobhunter.pipeline.dedupe import _refresh_group
from jobhunter.pipeline.dedupe_url import merge_by_apply_url

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
HX = {"HX-Request": "true"}
SRC = Path(__file__).resolve().parents[2] / "src" / "jobhunter"
GH = "https://boards.greenhouse.io/acme/jobs/4012345"


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "t.db"
    c = db.connect(p)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Test', 'x', 'http', 'https://example.com', 'enabled')"
    )
    c.close()
    return p


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def client(db_path):
    return TestClient(
        create_app(Settings(), lambda: db.connect(db_path), clock=lambda: NOW),
        follow_redirects=False,
    )


def group(conn, n, *, apply_url=None, created=ISO):
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, apply_url, "
        "location_scope, first_seen_at, last_seen_at) "
        "VALUES (?, 'co', ?, ?, ?, 'Acme', ?, 'single', ?, ?)",
        (n, str(n), f"https://example.com/{n}", f"Job {n}", apply_url, ISO, ISO),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, created),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) VALUES (?, 'CO', 'Denver', 1)",
        (n,),
    )
    _refresh_group(conn, n)
    return n


def application(conn, n, status="applied", **kw):
    group(conn, n, **kw)
    cur = conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (n, status, ISO, ISO),
    )
    app_id = int(cur.lastrowid or 0)
    t.add_event(conn, app_id, status, None, NOW - timedelta(days=30))
    return app_id


def log(conn, app_id=None):
    sql = "SELECT application_id, kind, due_at, backfill FROM followup_log"
    args: tuple = ()
    if app_id is not None:
        sql += " WHERE application_id = ?"
        args = (app_id,)
    return [tuple(r) for r in conn.execute(sql + " ORDER BY id", args)]


# --- recorded_at at every application_event insert ----------------------------------------

_INSERT_RE = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+application_event\b", re.I)


def _sites_missing_recorded_at(root: Path) -> tuple[int, list[str]]:
    """Scan every ``.py`` and ``.sql`` under ``root`` for ``INSERT INTO application_event``;
    returns (sites found, ``file:line`` of those whose column list lacks ``recorded_at``)."""
    found, missing = 0, []
    for path in sorted([*root.rglob("*.py"), *root.rglob("*.sql")]):
        text = path.read_text(encoding="utf-8")
        for m in _INSERT_RE.finditer(text):
            found += 1
            tail = text[m.end() : m.end() + 400]
            # The statement is built from adjacent string literals: the column list runs to
            # VALUES (or SELECT, for an INSERT ... SELECT).
            end = re.search(r"\b(VALUES|SELECT)\b", tail, re.I)
            if "recorded_at" not in (tail[: end.start()] if end else tail):
                missing.append(f"{path.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}")
    return found, missing


def test_every_application_event_insert_sets_recorded_at():
    found, missing = _sites_missing_recorded_at(SRC)
    # The six sites the spec names (tracking, detail x2, packets, inbox, dedupe merge): if this
    # drops, the scan stopped finding them, not the code stopped inserting.
    assert found >= 6
    assert missing == [], f"application_event insert without recorded_at: {missing}"


def test_the_scan_catches_a_site_without_recorded_at(tmp_path):
    (tmp_path / "x.py").write_text(
        'conn.execute(\n    "INSERT INTO application_event (application_id, at, status) "\n'
        '    "VALUES (?, ?, ?)", (1, 2, 3))\n'
        'conn.execute("insert or ignore into application_event (application_id, at) "\n'
        '    "SELECT id, at FROM application")\n'
    )
    assert _sites_missing_recorded_at(tmp_path) == (2, ["x.py:2", "x.py:4"])


def test_recorded_at_is_set_by_each_writer(conn):
    # tracking.add_event, backdated: recorded_at is when it was written, not the event's date.
    a = application(conn, 1)
    before = datetime.now(UTC)
    t.add_event(conn, a, "screening", "call", NOW - timedelta(days=20))
    row = conn.execute(
        "SELECT at, recorded_at FROM application_event WHERE status = 'screening'"
    ).fetchone()
    assert row["at"] == (NOW - timedelta(days=20)).isoformat()
    assert datetime.fromisoformat(row["recorded_at"]) >= before - timedelta(seconds=1)

    # inbox shortlist, apply click, "Did you apply?", packet start.
    group(conn, 2)
    inbox.set_label(conn, 2, "interesting")
    detail.log_click(conn, 2, "https://example.com/apply", "redirected", NOW)
    group(conn, 3)
    detail.answer_prompt(conn, 3, "not_yet", NOW)
    group(conn, 4)
    packets.prepare(conn, 4, NOW)
    notes = {
        r["note"]: (r["at"], r["recorded_at"])
        for r in conn.execute("SELECT note, at, recorded_at FROM application_event")
    }
    # These are stamped at the press (the shortlist reads its own clock; the others take now).
    for note in (t.APPLY_CLICK_NOTE, "not applied yet", t.PREPARE_NOTE):
        assert notes[note] == (ISO, ISO), note
    at, recorded = notes[t.SHORTLIST_NOTE]
    assert recorded is not None and recorded == at
    assert (
        conn.execute("SELECT COUNT(*) FROM application_event WHERE recorded_at IS NULL").fetchone()[
            0
        ]
        == 0
    )


def test_merge_event_has_recorded_at_and_followup_log_moves(conn):
    a1 = application(conn, 1, "interested", apply_url=GH, created="2026-09-01T00:00:00+00:00")
    a2 = application(conn, 2, "applied", apply_url=GH, created="2026-09-02T00:00:00+00:00")
    t.set_next_action(conn, a1, "email", "2026-10-14", now=NOW)
    t.done(conn, a1, NOW)
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 1
    # a2 is further along and wins; a1's follow-up history follows it.
    assert [r[0] for r in conn.execute("SELECT id FROM application")] == [a2]
    assert log(conn) == [(a2, "planned", "2026-10-14", 0), (a2, "done", "2026-10-14", 0)]
    merged = conn.execute(
        "SELECT recorded_at FROM application_event WHERE note LIKE 'Merged duplicate%'"
    ).fetchone()
    assert merged["recorded_at"] == ISO
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- follow-ups ----------------------------------------------------------------------------


def test_planned_only_when_the_date_changes_to_a_new_value(client, conn):
    a = application(conn, 1)
    post = lambda action, at: client.post(  # noqa: E731  (the drawer form re-posts both)
        f"/pipeline/{a}/next-action", data={"next_action": action, "next_action_at": at}, headers=HX
    )
    assert post("email recruiter", "2026-10-14").status_code == 200
    assert post("email the recruiter", "2026-10-14").status_code == 200  # text only
    assert post("email the recruiter", "2026-10-14").status_code == 200  # same date re-saved
    assert log(conn, a) == [(a, "planned", "2026-10-14", 0)]
    post("email the recruiter", "2026-10-16")
    post("no date now", "")  # cleared: nothing planned
    assert log(conn, a) == [(a, "planned", "2026-10-14", 0), (a, "planned", "2026-10-16", 0)]
    at = conn.execute("SELECT at FROM followup_log ORDER BY id DESC").fetchone()[0]
    assert at == ISO


def test_snooze_writes_nothing(client, conn):
    a = application(conn, 1)
    t.set_next_action(conn, a, "call", (NOW - timedelta(days=1)).date().isoformat(), now=NOW)
    n = len(log(conn, a))
    assert client.post(f"/followups/{a}/snooze", headers=HX).status_code == 200
    assert (
        conn.execute("SELECT next_action_at FROM application WHERE id = ?", (a,)).fetchone()[0]
        == (NOW + timedelta(days=3)).date().isoformat()
    )
    assert len(log(conn, a)) == n
    # Snoozing a nudge (no next action) sets a date you did not choose: still nothing.
    b = application(conn, 2)
    t.snooze(conn, b, NOW)
    assert log(conn, b) == []


def test_done_records_the_dated_action_it_closes(client, conn):
    a = application(conn, 1)
    t.set_next_action(conn, a, "call", "2026-10-08", now=NOW)
    assert client.post(f"/followups/{a}/done", headers=HX).status_code == 200
    assert log(conn, a) == [(a, "planned", "2026-10-08", 0), (a, "done", "2026-10-08", 0)]
    row = conn.execute("SELECT next_action, next_action_at FROM application").fetchone()
    assert tuple(row) == (None, None)
    # Done again (now with no next action, e.g. on a nudge) writes nothing.
    assert client.post(f"/followups/{a}/done", headers=HX).status_code == 200
    assert len(log(conn, a)) == 2


def test_set_done_set_same_date_done_is_one_pair(conn):
    a = application(conn, 1)
    for _ in range(2):
        t.set_next_action(conn, a, "call", "2026-10-14", now=NOW)
        t.done(conn, a, NOW)
    pairs = conn.execute(
        "SELECT DISTINCT application_id, due_at FROM followup_log WHERE kind = 'done'"
    ).fetchall()
    assert [tuple(p) for p in pairs] == [(a, "2026-10-14")]
    assert [k for _, k, _, _ in log(conn, a)] == ["planned", "done", "planned", "done"]


def test_no_response_writes_event_log_and_clear_together(client, conn):
    a = application(conn, 1)
    t.set_next_action(conn, a, "chase", "2026-10-01", now=NOW)
    assert client.post(f"/followups/{a}/no-response", headers=HX).status_code == 200
    assert t.events(conn, a)[0]["status"] == "no_response"
    assert log(conn, a)[-1] == (a, "no_response", "2026-10-01", 0)
    row = conn.execute("SELECT status, next_action_at FROM application").fetchone()
    assert tuple(row) == ("no_response", None)
    # A nudge has no next action: the record carries no date.
    b = application(conn, 2)
    t.no_response(conn, b, NOW)
    assert log(conn, b) == [(b, "no_response", None, 0)]


def test_no_response_is_one_transaction(conn, monkeypatch):
    a = application(conn, 1)
    t.set_next_action(conn, a, "chase", "2026-10-01", now=NOW)
    events_before = len(t.events(conn, a))
    log_before = log(conn, a)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(t, "set_next_action", boom)  # fails after the event and the log row
    with pytest.raises(RuntimeError):
        t.no_response(conn, a, NOW)
    assert not conn.in_transaction
    assert len(t.events(conn, a)) == events_before
    assert log(conn, a) == log_before
    assert conn.execute("SELECT status FROM application").fetchone()[0] == "applied"


def test_add_event_inside_a_callers_transaction_leaves_the_commit_to_it(conn):
    a = application(conn, 1)
    conn.execute("BEGIN IMMEDIATE")
    t.add_event(conn, a, "screening", None, NOW)
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert [e["status"] for e in t.events(conn, a)] == ["applied"]


# --- contacts and rejections ---------------------------------------------------------------


def test_contact_created_at(client, conn):
    a = application(conn, 1)
    before = datetime.now(UTC)
    r = client.post(f"/pipeline/{a}/contacts", data={"name": "Pat Recruiter"}, headers=HX)
    assert r.status_code == 200
    created = conn.execute("SELECT created_at FROM contact").fetchone()[0]
    assert datetime.fromisoformat(created) >= before - timedelta(seconds=1)


def test_confirming_a_pending_rejection_sets_confirmed_at_once(client, conn):
    rid = rejections.record(
        conn, received_at=ISO, employer="Acme", title=None, source="email", now=NOW,
        gmail_message_id="m1", state="pending",
    )  # fmt: skip
    assert conn.execute("SELECT confirmed_at FROM rejection").fetchone()[0] is None
    assert client.post(f"/rejections/{rid}/confirm").status_code == 303
    assert conn.execute("SELECT state, confirmed_at FROM rejection").fetchone()[:] == (
        "confirmed",
        ISO,
    )
    rejections.confirm(conn, rid, NOW + timedelta(days=1))  # a second confirm keeps the first
    assert conn.execute("SELECT confirmed_at FROM rejection").fetchone()[0] == ISO


# --- shortlist undo ------------------------------------------------------------------------


def test_undo_keeps_a_shortlisted_application_with_follow_up_history(conn):
    group(conn, 1)
    inbox.set_label(conn, 1, "interesting")
    app_id = conn.execute("SELECT id FROM application").fetchone()[0]
    t.set_next_action(conn, app_id, "research the team", "2026-10-12", now=NOW)
    inbox.undo_label(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 1
    assert log(conn, app_id) == [(app_id, "planned", "2026-10-12", 0)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_undo_without_follow_up_history_still_removes_the_application(conn):
    group(conn, 1)
    inbox.set_label(conn, 1, "interesting")
    inbox.undo_label(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 0
