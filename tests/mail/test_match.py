"""Gmail application matching: fake Gmail service, synthetic mail, no network."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from jobhunter.cli import app as cli_app
from jobhunter.config import Settings
from jobhunter.console import proposals as props
from jobhunter.console import tracking as t
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.mail import auth, match

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SETTINGS = Settings()


# --- fake Gmail ----------------------------------------------------------------------------


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _Messages:
    def __init__(self, g):
        self.g = g

    def list(self, userId, q, maxResults=100, pageToken=None):
        self.g.queries.append(q)
        refs = [{"id": m["id"], "threadId": m["threadId"]} for m in self.g.messages]
        return _Call(lambda: {"messages": refs})

    def get(self, userId, id, format):
        self.g.gets.append(id)
        return _Call(lambda: next(m for m in self.g.messages if m["id"] == id))


class _Labels:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return _Call(lambda: {"labels": [{"id": "Label_9", "name": SETTINGS.mail.label}]})


class _Users:
    def __init__(self, g):
        self.g = g

    def messages(self):
        return _Messages(self.g)

    def labels(self):
        return _Labels(self.g)


class FakeGmail:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.queries: list[str] = []
        self.gets: list[str] = []

    def users(self):
        return _Users(self)


def raw(mid, sender, subject, body, days_ago=1, labels=()):
    when = NOW - timedelta(days=days_ago)
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": mid,
        "threadId": f"th-{mid}",
        "internalDate": str(int(when.timestamp() * 1000)),
        "snippet": body[:300],
        "labelIds": list(labels),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "parts": [{"mimeType": "text/plain", "body": {"data": data}}],
        },
    }


# --- database ------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Test', 'x', 'http', 'https://example.test', 'enabled')"
    )
    c.commit()
    yield c
    c.close()


def add_job(conn, n, employer, title, host=None, applied=None):
    ts = (NOW - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, first_seen_at, "
        "last_seen_at) VALUES (?, 'co', ?, ?, ?, ?, ?, ?)",
        (n, str(n), f"https://example.test/{n}", title, employer, ts, ts),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ts),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    if host:
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, "
            "employer_host, status, resolved_at) VALUES (?, ?, ?, '[]', 'workday', ?, 'live', ?)",
            (n, f"https://{host}/job/{n}", f"https://{host}/job/{n}", host, ts),
        )
    app_id = None
    if applied:
        cur = conn.execute(
            "INSERT INTO application (job_group_id, status, created_at, updated_at) "
            "VALUES (?, 'interested', ?, ?)",
            (n, ts, ts),
        )
        app_id = cur.lastrowid
        conn.commit()
        t.add_event(conn, app_id, "interested", None, NOW - timedelta(days=20))
        if applied != "interested":
            t.add_event(conn, app_id, applied, None, NOW - timedelta(days=10))
    conn.commit()
    return app_id


NORTHWIND_HOST = "northwind.wd5.myworkdayjobs.com"
CONFIRM_BODY = (
    "Hello Alex, thank you for applying to the Senior Data Engineer position at Northwind "
    "Analytics. We have received your application. View it at "
    f"https://{NORTHWIND_HOST}/en-US/careers/job/123"
)


def msg_confirm(mid="m1", days_ago=1):
    return raw(
        mid,
        "Northwind Analytics <noreply@myworkday.com>",
        "Thank you for applying to Northwind Analytics",
        CONFIRM_BODY,
        days_ago,
    )


def msg_of(sender, subject, body=""):
    return match.Message("x", "t", NOW, sender, subject, body[:200], body)


# --- classification ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "subject", "body", "kind"),
    [
        ("A <no-reply@myworkday.com>", "Thank you for applying", "", "confirmation"),
        ("A <jobs@acme.example.test>", "Application received", "", "confirmation"),
        (
            "A <jobs@acme.example.test>",
            "Update",
            "We have received your application.",
            "confirmation",
        ),
        (
            "A <no-reply@greenhouse.io>",
            "Your application",
            "It is under review by the team.",
            "acknowledgement",
        ),
        (
            "A <x@example.test>",
            "Your application",
            "Unfortunately we are not moving forward.",
            "rejection",
        ),
        (
            "A <no-reply@lever.co>",
            "Thanks for applying",
            "Unfortunately, we decided to pursue other candidates.",
            "rejection",
        ),
        ("A <x@icims.com>", "Interview invitation", "", "interview"),
        (
            "A <x@example.test>",
            "Next steps",
            "We would like to schedule an interview with you.",
            "interview",
        ),
        ("A <x@example.test>", "Offer", "We are pleased to offer you the role.", "offer"),
        ("A <x@example.test>", "Lunch on Friday?", "Want to grab food?", "other"),
        # Loose phrases only count from a known ATS sender.
        ("A <x@example.test>", "Hi", "Thank you for your interest in our newsletter.", "other"),
        ("A <x@smartrecruiters.com>", "Hi", "Thank you for your interest in Acme.", "confirmation"),
    ],
)
def test_classification_table(sender, subject, body, kind):
    assert match.classify(msg_of(sender, subject, body)).kind == kind


@pytest.mark.parametrize(
    "domain",
    [
        "myworkday.com",
        "mail.greenhouse.io",
        "hire.lever.co",
        "us.icims.com",
        "jobs.ashbyhq.com",
        "smartrecruiters.com",
        "usastaffing.gov",
        "usajobs.gov",
        "governmentjobs.com",
        "taleo.net",
    ],
)
def test_known_ats_sender_domains(domain):
    assert match.is_ats_sender(domain)
    assert not match.is_ats_sender("notgreenhouse.io.example.test")


def test_llm_hook_only_sees_other():
    seen = []

    def hook(m):
        seen.append(m.subject)
        return match.Classification("offer", False, False, source="llm")

    assert match.classify(msg_of("A <x@example.test>", "Lunch?", ""), hook).source == "llm"
    assert match.classify(msg_of("A <x@icims.com>", "Application received", ""), hook).kind == (
        "confirmation"
    )
    assert seen == ["Lunch?"]


# --- matching ------------------------------------------------------------------------------


def test_match_by_ats_host(conn):
    add_job(conn, 1, "Unrelated Name", "Quite Different Title", host=NORTHWIND_HOST)
    m = match.parse_message(msg_confirm())
    mt = match.match_groups(m, match.load_groups(conn))
    assert mt is not None
    assert mt.group.group_id == 1
    assert mt.matched == ["ats_host"]


def test_match_by_employer_fuzzy_and_title(conn):
    add_job(conn, 1, "Northwind Analytics, Inc.", "Senior Data Engineer")
    add_job(conn, 2, "Fabrikam Robotics", "Senior Data Engineer")
    m = match.parse_message(msg_confirm())
    mt = match.match_groups(m, match.load_groups(conn))
    assert mt and mt.group.group_id == 1
    assert set(mt.matched) == {"employer", "title"}
    # A misspelt employer still matches fuzzily.
    body = CONFIRM_BODY.replace("Northwind", "Northwynd")
    m2 = match.parse_message(raw("m2", "x@example.test", "Thank you for applying", body))
    mt2 = match.match_groups(m2, match.load_groups(conn))
    assert mt2 and mt2.group.group_id == 1


def test_employer_alone_is_not_enough(conn):
    add_job(conn, 1, "Northwind Analytics", "Completely Different Role")
    m = match.parse_message(
        raw("m", "x@example.test", "Application received", "Northwind Analytics")
    )
    assert match.match_groups(m, match.load_groups(conn)) is None


def test_ambiguous_titles_pick_none(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer")
    add_job(conn, 2, "Northwind Analytics", "Senior Data Engineer")
    m = match.parse_message(msg_confirm())
    assert match.match_groups(m, match.load_groups(conn)) is None


# --- proposals -----------------------------------------------------------------------------


def run_scan(conn, messages, **kw):
    svc = FakeGmail(messages)
    return svc, match.scan(conn, svc, SETTINGS, now=NOW, **kw)


def rows(conn):
    return conn.execute("SELECT * FROM mail_proposal ORDER BY id").fetchall()


def test_confirmation_without_application_proposes_create(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", host=NORTHWIND_HOST)
    _, res = run_scan(conn, [msg_confirm()])
    assert res.stored == 1
    (r,) = rows(conn)
    assert (r["proposed_action"], r["proposed_status"], r["job_group_id"]) == (
        "create_application",
        "applied",
        1,
    )
    assert r["application_id"] is None
    assert r["state"] == "pending"
    assert r["confidence"] >= 0.9
    ev = json.loads(r["evidence"])
    assert set(ev["matched"]) == {"ats_host", "employer", "title"}
    assert conn.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 0


def test_confirmation_with_no_matching_job_proposes_manual_record(conn):
    add_job(conn, 1, "Fabrikam Robotics", "Welder")
    _, _res = run_scan(conn, [msg_confirm()])
    (r,) = rows(conn)
    assert r["proposed_action"] == "create_application"
    assert r["job_group_id"] is None
    ev = json.loads(r["evidence"])
    assert ev["parsed"] == {"employer": "Northwind Analytics", "title": "Senior Data Engineer"}
    assert r["confidence"] < 0.5


def test_rejection_for_existing_application_proposes_event(conn):
    app_id = add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", applied="applied")
    rej = raw(
        "r1",
        "Northwind Analytics <no-reply@greenhouse.io>",
        "Your application to Northwind Analytics",
        "Unfortunately we will not be moving forward with your application for the "
        "Senior Data Engineer role.",
    )
    run_scan(conn, [rej])
    (r,) = rows(conn)
    assert (r["proposed_action"], r["proposed_status"], r["application_id"]) == (
        "add_event",
        "rejected",
        app_id,
    )
    # Nothing applied yet.
    assert t.events(conn, app_id)[0]["status"] == "applied"


def test_event_that_adds_nothing_is_not_proposed(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", applied="interview")
    ack = raw(
        "a1",
        "Northwind Analytics <no-reply@lever.co>",
        "Application status",
        "Your Senior Data Engineer application at Northwind Analytics is under review.",
    )
    run_scan(conn, [ack, msg_confirm("m9")])
    assert rows(conn) == []


def test_other_mail_and_alerts_label_are_ignored(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer")
    lunch = raw("o1", "Pat <pat@example.test>", "Lunch?", "Friday?")
    alert = msg_confirm("al1")
    alert["labelIds"] = ["Label_9"]
    run_scan(conn, [lunch, alert])
    assert rows(conn) == []


def test_query_excludes_alerts_label_and_is_windowed(conn):
    svc, _ = run_scan(conn, [], days=7)
    q = svc.queries[0]
    assert "newer_than:7d" in q
    assert "-label:jobhunter-alerts" in q


def test_rescan_is_idempotent_and_skips_known_messages(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", host=NORTHWIND_HOST)
    _svc, first = run_scan(conn, [msg_confirm()])
    assert first.stored == 1
    svc2, second = run_scan(conn, [msg_confirm()])
    assert second.stored == 0
    assert len(rows(conn)) == 1
    assert svc2.gets == []  # a known message is not even fetched again


def test_dry_run_writes_nothing(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", host=NORTHWIND_HOST)
    _, res = run_scan(conn, [msg_confirm()], dry_run=True)
    assert len(res.proposals) == 1
    assert res.stored == 0
    assert rows(conn) == []


def test_snippet_capped_and_body_not_stored(conn):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", host=NORTHWIND_HOST)
    secret = "SECRET-TAIL-" + "x" * 50
    body = CONFIRM_BODY + " " + "filler " * 200 + secret
    long = raw("big", "Northwind Analytics <noreply@myworkday.com>", "Thank you for applying", body)
    long["snippet"] = body[:1000]
    run_scan(conn, [long])
    (r,) = rows(conn)
    ev = json.loads(r["evidence"])
    assert 0 < len(ev["snippet"]) <= match.SNIPPET_MAX
    assert "SECRET-TAIL" not in json.dumps(dict(r))


# --- accept / dismiss ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, conn):
    path = tmp_path / "t.db"
    return TestClient(
        create_app(Settings(), lambda: db.connect(path), clock=lambda: NOW), follow_redirects=False
    )


def test_accept_event_applies_via_add_event(conn, client):
    app_id = add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", applied="applied")
    rej = raw(
        "r1",
        "Northwind Analytics <no-reply@greenhouse.io>",
        "Your application to Northwind Analytics",
        "Unfortunately we will not be moving forward. Senior Data Engineer.",
    )
    run_scan(conn, [rej])
    page = client.get("/proposals")
    assert page.status_code == 200
    assert "Unfortunately" in page.text
    assert "Proposals (1)" in client.get("/pipeline").text
    pid = rows(conn)[0]["id"]
    assert client.post(f"/proposals/{pid}/accept").status_code == 303
    ev = t.events(conn, app_id)[0]
    assert (ev["status"], ev["source"]) == ("rejected", "email")
    r = rows(conn)[0]
    assert r["state"] == "accepted"
    assert r["decided_at"]
    # Deciding twice is refused.
    assert client.post(f"/proposals/{pid}/accept").status_code == 409


def test_accept_create_for_known_job(conn, client):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", host=NORTHWIND_HOST)
    run_scan(conn, [msg_confirm(days_ago=2)])
    pid = rows(conn)[0]["id"]
    client.post(f"/proposals/{pid}/accept", headers={"HX-Request": "true"})
    a = conn.execute("SELECT * FROM application WHERE job_group_id = 1").fetchone()
    assert a["status"] == "applied"
    assert a["applied_at"].startswith("2026-10-07")
    assert t.events(conn, a["id"])[0]["source"] == "email"


def test_accept_manual_record_creates_job_and_application(conn, client):
    add_job(conn, 1, "Fabrikam Robotics", "Welder")
    run_scan(conn, [msg_confirm()])
    pid = rows(conn)[0]["id"]
    client.post(f"/proposals/{pid}/accept", content="employer=Edited+Co&title=Analyst")
    row = conn.execute(
        "SELECT a.status, j.employer, j.title FROM application a "
        "JOIN job_group g ON g.id = a.job_group_id JOIN job j ON j.id = g.canonical_job_id "
        "WHERE j.source_key = ?",
        (props.MANUAL_SOURCE,),
    ).fetchone()
    assert tuple(row) == ("applied", "Edited Co", "Analyst")


def test_dismiss_changes_nothing(conn, client):
    add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", host=NORTHWIND_HOST)
    run_scan(conn, [msg_confirm()])
    pid = rows(conn)[0]["id"]
    assert client.post(f"/proposals/{pid}/dismiss").status_code == 303
    assert rows(conn)[0]["state"] == "dismissed"
    assert conn.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 0
    assert "Proposals (0)" in client.get("/proposals").text
    # A dismissed message is not re-proposed by a rescan.
    run_scan(conn, [msg_confirm()])
    assert len(rows(conn)) == 1


# --- CLI -----------------------------------------------------------------------------------


def test_cli_skips_without_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "load_token", lambda: None)
    monkeypatch.setattr(
        auth, "build_service", lambda s: pytest.fail("must not build a service without a token")
    )
    res = CliRunner().invoke(cli_app, ["mail", "match"])
    assert res.exit_code == 0
    assert "skipping" in res.output


def test_cli_dry_run_prints_and_writes_nothing(monkeypatch, tmp_path):
    path = tmp_path / "cli.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    settings = Settings.model_validate({"paths": {"db_path": str(path)}})
    monkeypatch.setattr("jobhunter.config.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr(auth, "load_token", lambda: "tok")
    monkeypatch.setattr(auth, "build_service", lambda s: FakeGmail([msg_confirm()]))
    res = CliRunner().invoke(cli_app, ["mail", "match", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "create_application" in res.output
    assert "would store 1" in res.output
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM mail_proposal").fetchone()[0] == 0
    c.close()
    res = CliRunner().invoke(cli_app, ["mail", "match"])
    assert "stored 1" in res.output


# --- employer rejections (rejection table) -------------------------------------------------


def rejections_rows(conn):
    return conn.execute("SELECT * FROM rejection ORDER BY id").fetchall()


SEEQ_REJECT = (
    "Thank you for your interest in the Senior Platform Engineer position at Seeq. "
    "Unfortunately, we have decided to move forward with other candidates."
)


def msg_seeq_reject(mid="rj1", thread=None, days_ago=1):
    m = raw(mid, "Seeq <no-reply@ashbyhq.com>", "Your application to Seeq", SEEQ_REJECT, days_ago)
    if thread:
        m["threadId"] = thread
    return m


def test_unmatched_rejection_is_stored_not_dropped(conn):
    add_job(conn, 1, "Fabrikam Robotics", "Welder")
    _, res = run_scan(conn, [msg_seeq_reject()])
    assert rows(conn) == []  # no proposal: nothing to change in the pipeline
    (r,) = rejections_rows(conn)
    assert (r["employer"], r["employer_norm"], r["title"]) == (
        "Seeq",
        "seeq",
        "Senior Platform Engineer",
    )
    assert r["job_group_id"] is None and r["application_id"] is None
    assert (r["source"], r["gmail_message_id"], r["thread_id"]) == ("email", "rj1", "th-rj1")
    ev = json.loads(r["evidence"])
    assert set(ev) >= {"sender", "subject", "snippet", "phrase"}
    assert len(ev["snippet"]) <= match.SNIPPET_MAX
    assert res.rejections_stored == 1


def test_matched_rejection_stores_row_and_still_proposes_event(conn):
    app_id = add_job(conn, 1, "Northwind Analytics", "Senior Data Engineer", applied="applied")
    rej = raw(
        "r1",
        "Northwind Analytics <no-reply@greenhouse.io>",
        "Your application to Northwind Analytics",
        "Unfortunately we will not be moving forward with your application for the "
        "Senior Data Engineer role.",
    )
    run_scan(conn, [rej])
    (p,) = rows(conn)
    assert (p["proposed_action"], p["application_id"]) == ("add_event", app_id)
    (r,) = rejections_rows(conn)
    assert (r["job_group_id"], r["application_id"]) == (1, app_id)
    assert (r["employer"], r["title"]) == ("Northwind Analytics", "Senior Data Engineer")
    # The fact is recorded, but the application's status is only changed on accept.
    assert t.events(conn, app_id)[0]["status"] == "applied"


def test_rejection_rescan_is_idempotent_and_not_refetched(conn):
    run_scan(conn, [msg_seeq_reject()])
    svc, res = run_scan(conn, [msg_seeq_reject()])
    assert len(rejections_rows(conn)) == 1
    assert res.rejections_stored == 0
    assert svc.gets == []


def test_rejection_dry_run_lists_and_writes_nothing(conn):
    _, res = run_scan(conn, [msg_seeq_reject()], dry_run=True)
    assert [r.employer for r in res.rejections] == ["Seeq"]
    assert rejections_rows(conn) == []


def test_one_rejection_per_thread(conn):
    run_scan(conn, [msg_seeq_reject("a", "T1", 3), msg_seeq_reject("b", "T1", 1)])
    assert [r["gmail_message_id"] for r in rejections_rows(conn)] == ["a"]


def test_repeated_confirmations_in_one_thread_make_one_proposal(conn):
    msgs = []
    for i in range(5):
        m = raw(
            f"s{i}",
            "Seeq <no-reply@ashbyhq.com>",
            "Thanks for applying to Seeq",
            "Thanks for applying to the Platform Engineer role at Seeq.",
            days_ago=5 - i,
        )
        m["threadId"] = "T-seeq"
        msgs.append(m)
    _, res = run_scan(conn, msgs)
    (p,) = rows(conn)
    assert p["gmail_message_id"] == "s0"  # the earliest
    assert res.thread_duplicates == 4
    # A later rescan with a new message in the same thread adds nothing either.
    extra = dict(msgs[0], id="s9")
    _, res2 = run_scan(conn, [extra])
    assert len(rows(conn)) == 1 and res2.thread_duplicates == 1


# --- Gmail rate limits ---------------------------------------------------------------------


def http_error(status, reason="rateLimitExceeded", retry_after=None):
    import httplib2
    from googleapiclient.errors import HttpError

    headers = {"status": status}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    body = json.dumps(
        {"error": {"code": status, "errors": [{"reason": reason}], "message": "Units per minute"}}
    ).encode()
    return HttpError(httplib2.Response(headers), body)


class FlakyGmail(FakeGmail):
    """messages.get fails with the given errors first, then succeeds."""

    def __init__(self, messages, errors):
        super().__init__(messages)
        self.errors = list(errors)

    def users(self):
        users = super().users()
        msgs = users.messages()
        flaky = self

        class M:
            def list(self, **kw):
                return msgs.list(**kw)

            def get(self, **kw):
                call = msgs.get(**kw)

                def run():
                    if flaky.errors:
                        raise flaky.errors.pop(0)
                    return call.execute()

                return _Call(run)

        users.messages = lambda: M()
        return users


def test_rate_limit_backs_off_then_succeeds(conn):
    sleeps: list[float] = []
    svc = FlakyGmail(
        [msg_seeq_reject()],
        [http_error(429), http_error(403, "userRateLimitExceeded"), http_error(429, retry_after=7)],
    )
    res = match.scan(conn, svc, SETTINGS, now=NOW, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 7.0]  # exponential, then Retry-After honoured
    assert res.rejections_stored == 1


def test_rate_limit_exhausted_raises_clean_error(conn):
    from jobhunter.mail.gmail_api import MAX_RETRIES, GmailRateLimited

    sleeps: list[float] = []
    svc = FlakyGmail([msg_seeq_reject()], [http_error(429)] * (MAX_RETRIES + 1))
    with pytest.raises(GmailRateLimited, match="rate limit"):
        match.scan(conn, svc, SETTINGS, now=NOW, sleep=sleeps.append)
    assert len(sleeps) == MAX_RETRIES


def test_other_http_errors_are_not_retried(conn):
    from googleapiclient.errors import HttpError

    sleeps: list[float] = []
    svc = FlakyGmail([msg_seeq_reject()], [http_error(403, "insufficientPermissions")])
    with pytest.raises(HttpError):
        match.scan(conn, svc, SETTINGS, now=NOW, sleep=sleeps.append)
    assert sleeps == []


def test_cli_rate_limit_prints_message_not_traceback(monkeypatch, tmp_path):
    from jobhunter.mail.gmail_api import GmailRateLimited

    path = tmp_path / "cli.db"
    settings = Settings.model_validate({"paths": {"db_path": str(path)}})
    monkeypatch.setattr("jobhunter.config.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr(auth, "load_token", lambda: "tok")
    monkeypatch.setattr(auth, "build_service", lambda s: FakeGmail([]))

    def boom(*a, **k):
        raise GmailRateLimited("Gmail rate limit: still refused after 6 retries (HTTP 429).")

    monkeypatch.setattr(match, "scan", boom)
    res = CliRunner().invoke(cli_app, ["mail", "match", "--days", "60"])
    assert res.exit_code == 1
    assert "Gmail rate limit" in res.output
    assert "Traceback" not in res.output


def test_cli_dry_run_lists_employer_rejections(monkeypatch, tmp_path):
    path = tmp_path / "cli.db"
    settings = Settings.model_validate({"paths": {"db_path": str(path)}})
    monkeypatch.setattr("jobhunter.config.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr(auth, "load_token", lambda: "tok")
    monkeypatch.setattr(auth, "build_service", lambda s: FakeGmail([msg_seeq_reject()]))
    res = CliRunner().invoke(cli_app, ["mail", "match", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "employer rejection: Seeq | Senior Platform Engineer" in res.output
    assert "would store 0 proposals and 1 employer rejections" in res.output
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM rejection").fetchone()[0] == 0
    c.close()
