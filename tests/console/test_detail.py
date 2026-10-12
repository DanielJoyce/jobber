"""Job detail page, apply button states, /apply redirect and the did-you-apply flow. No network."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Settings
from jobhunter.console import apply_bg, detail
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
WD = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Albany-NY/Analyst_R-1234"
DESC = (
    "We run synthetic hosts.\n\nYou will   manage <script>alert(1)</script> Linux servers\n"
    "daily and lead incident response.\n\nBenefits include dental."
)
PREFS = (
    "hard:\n  states_allowed: [CO]\n  remote_ok: true\n"
    "soft:\n  state_ranking: [CO]\nresume_path: resume.md\n"
)
DIMS = {
    "skills": {"score": 88, "why": "skills reason"},
    "seniority": {"score": 80, "why": "seniority reason"},
    "domain": {"score": 80, "why": "domain reason"},
    "raw_skills": 88,
    "recency_weighted_skills": 85,
    "stale_skills": ["VMware"],
}
EVIDENCE = [
    {"claim": "daily Linux work", "quote": "MANAGE <script>alert(1)</script> linux servers daily"},
    {"claim": "invented claim", "quote": "free pizza every friday"},
]


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.db"


def add_job(c, jid, gid, source, url, ts):
    c.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "salary_stated, location_scope, remote, employment_type, first_seen_at, last_seen_at, "
        "job_group_id) VALUES (?, ?, ?, ?, ?, 'Acme', ?, 0, 'single', 'onsite', 'full-time', "
        "?, ?, ?)",
        (jid, source, str(jid), url, f"Analyst {jid}", DESC, ts, ts, gid),
    )


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Connecting Colorado', 'x', 'http', 'https://example.com', 'enabled'), "
        "('nlx', 'A', 'NLx', 'x', 'http', 'https://example.com', 'enabled')"
    )
    ts = NOW.isoformat()
    for gid in range(1, 7):
        c.execute(
            "INSERT INTO job_group (id, member_count, method, created_at) "
            "VALUES (?, 1, 'exact_hash', ?)",
            (gid, ts),
        )
        add_job(c, gid, gid, "co", f"https://example.com/{gid}", ts)
        c.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (gid, gid))
        c.execute(
            "INSERT INTO job_locations (job_id, state, city, is_primary) "
            "VALUES (?, 'CO', 'Denver', 1)",
            (gid,),
        )
        c.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, blockers, missing_info, shape_flags, "
            "tailoring_hints, evidence_unverified, created_at) "
            "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 0, ?, ?, '[\"needs clearance\"]', "
            "'[\"no pay range\"]', '[\"on_call_heavy\"]', '[\"stress incident response\"]', 1, ?)",
            (gid, json.dumps(DIMS), json.dumps(EVIDENCE), ts),
        )
    # a duplicate sibling in group 1
    add_job(c, 99, 1, "nlx", "https://nlx.example/99", ts)
    c.execute("UPDATE job_group SET member_count = 2 WHERE id = 1")
    yield c
    c.close()


def add_link(conn, gid, status, *, verified_hours=1.0, final=WD, ats="workday"):
    chain = [
        {"url": "https://appcast.example/x", "host": "appcast.example", "method": "unwrap"},
        {"url": final, "host": "acme.wd5.myworkdayjobs.com", "method": "3xx", "status": 200},
    ]
    verified = (NOW - timedelta(hours=verified_hours)).isoformat()
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, employer_host, "
        "status, resolved_at, verified_at) VALUES (?, 'https://nlx.example/start', ?, ?, ?, "
        "'acme.wd5.myworkdayjobs.com', ?, ?, ?)",
        (gid, final, json.dumps(chain), ats, status, verified, verified),
    )


class Pages:
    def __init__(self):
        self.responses: dict[str, httpx.Response] = {}
        self.hits: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.hits.append(url)
        return self.responses.get(url, httpx.Response(500))


@pytest.fixture
def pages():
    return Pages()


@pytest.fixture
def client(tmp_path, db_path, conn, pages):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n", encoding="utf-8")
    settings = Settings.model_validate(
        {
            "paths": {
                "profile_dir": str(d),
                "db_path": str(db_path),
                "cache_dir": str(tmp_path / "c"),
            },
            "fetch": {"max_retries": 0},
        }
    )
    app = create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW)
    ctx_conn = db.connect(db_path)

    def factory(url):
        t = {"v": 0.0}

        def sleep(s):
            t["v"] += s

        return fetch_context_for_url(
            settings,
            ctx_conn,
            url,
            transport=httpx.MockTransport(pages.handle),
            clock=lambda: t["v"],
            sleep=sleep,
        )

    app.state.ctx_factory = factory
    yield TestClient(app, follow_redirects=False)
    ctx_conn.close()


def test_page_renders(client):
    r = client.get("/job/1")
    assert r.status_code == 200
    t = r.text
    assert "Analyst 1" in t and "Acme" in t and "Denver, CO" in t
    assert "salary not stated" in t
    assert "bucket" in t and "overall" in t and "verdict" in t
    assert "skills reason" in t and "recency-weighted" in t and "VMware" in t
    assert "needs clearance" in t and "no pay range" in t and "on_call_heavy" in t
    assert "stress incident response" in t
    assert "Also posted (1)" in t and "via NLx" in t
    assert client.get("/job/404").status_code == 404


def test_evidence_highlight_and_escaping(client):
    t = client.get("/job/1").text
    assert '<mark title="daily Linux work">' in t
    assert "manage &lt;script&gt;alert(1)&lt;/script&gt; Linux servers\ndaily</mark>" in t
    assert "<script>alert(1)" not in t
    # unverified quote listed separately, not highlighted
    assert "free pizza every friday" in t and "unverified" in t
    assert 'title="invented claim"' not in t
    assert t.count('<p class="desc">') == 3


def test_highlight_unit():
    h = detail.highlight("Hello <b>World</b>.\n\nSecond  para", [{"claim": "c", "quote": "world"}])
    assert len(h.paragraphs) == 2
    assert "&lt;b&gt;" in h.paragraphs[0]
    assert h.unverified == []
    h = detail.highlight("abc", [{"claim": "c", "quote": "xyz"}])
    assert h.unverified and "<mark" not in "".join(h.paragraphs)


def test_button_no_row_resolves_on_demand(client, conn, pages):
    t = client.get("/job/1").text
    assert "Open posting" in t
    assert 'hx-get="/job/1/apply-button"' in t and 'hx-trigger="load"' in t
    pages.responses["https://example.com/1"] = httpx.Response(302, headers={"Location": WD})
    pages.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    frag = client.get("/job/1/apply-button")
    assert frag.status_code == 200
    assert "Apply on Workday" in frag.text and "hx-get" not in frag.text
    row = conn.execute("SELECT status FROM apply_link WHERE job_group_id = 1").fetchone()
    assert row["status"] == "live"


def test_button_live(client, conn):
    add_link(conn, 2, "live", verified_hours=3)
    t = client.get("/job/2").text
    assert "Apply on Workday ↗" in t
    assert 'href="/apply/2"' in t
    assert "verified 3h ago" in t
    chain = "nlx.example → appcast.example (unwrapped) → Workday · "
    assert chain + "acme.wd5.myworkdayjobs.com" in t
    assert "apply-button" not in t


def test_button_expired(client, conn):
    add_link(conn, 3, "expired")
    t = client.get("/job/3").text
    assert "Posting closed" in t and "disabled" in t and "open anyway" in t
    assert "Apply on" not in t


@pytest.mark.parametrize("status", ["unresolved", "blocked"])
def test_button_open_posting(client, conn, status):
    add_link(conn, 4, status, ats=None)
    t = client.get("/job/4").text
    assert "Open posting" in t and "Apply on" not in t and "apply-button" not in t


def test_apply_redirects_live_and_logs(client, conn, pages):
    add_link(conn, 2, "live", verified_hours=1)
    r = client.get("/apply/2")
    assert r.status_code == 302 and r.headers["location"] == WD
    assert pages.hits == []  # fresh: no network
    click = conn.execute("SELECT * FROM apply_click WHERE job_group_id = 2").fetchone()
    assert click["outcome"] == "redirected" and click["final_url"] == WD


def test_apply_reverifies_when_stale(client, conn, pages):
    add_link(conn, 2, "live", verified_hours=48)
    pages.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    r = client.get("/apply/2")
    assert r.status_code == 302 and pages.hits == [WD]
    row = conn.execute("SELECT verified_at FROM apply_link WHERE job_group_id = 2").fetchone()
    assert row["verified_at"].startswith("2026-10-09T12:00")


def test_apply_stale_but_now_closed(client, conn, pages):
    add_link(conn, 2, "live", verified_hours=48)
    pages.responses[WD] = httpx.Response(404)
    r = client.get("/apply/2")
    assert r.status_code == 200
    assert "posting has closed" in r.text and WD in r.text
    assert conn.execute("SELECT outcome FROM apply_click").fetchone()["outcome"] == "expired"
    status = conn.execute("SELECT status FROM apply_link WHERE job_group_id = 2").fetchone()[0]
    assert status == "expired"


def test_apply_expired_page(client, conn, pages):
    add_link(conn, 3, "expired")
    r = client.get("/apply/3")
    assert r.status_code == 200 and "posting has closed" in r.text
    assert pages.hits == []
    assert conn.execute("SELECT outcome FROM apply_click").fetchone()["outcome"] == "expired"


def test_apply_without_link_goes_to_posting(client, conn):
    r = client.get("/apply/5")
    assert r.status_code == 302 and r.headers["location"] == "https://example.com/5"
    assert conn.execute("SELECT COUNT(*) FROM apply_click").fetchone()[0] == 1


def add_app(conn, gid, status="interested"):
    ts = (NOW - timedelta(days=1)).isoformat()
    cur = conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (gid, status, ts, ts),
    )
    conn.execute(
        "INSERT INTO application_event (application_id, at, status, source) "
        "VALUES (?, ?, ?, 'manual')",
        (cur.lastrowid, ts, status),
    )


def test_click_moves_interested_to_preparing(client, conn):
    add_app(conn, 5)
    client.get("/apply/5")
    assert conn.execute("SELECT status FROM application").fetchone()[0] == "preparing"
    events = conn.execute("SELECT status FROM application_event ORDER BY id").fetchall()
    assert [e[0] for e in events] == ["interested", "preparing"]


def test_click_leaves_later_status_alone(client, conn):
    add_app(conn, 5, "applied")
    client.get("/apply/5")
    assert conn.execute("SELECT status FROM application").fetchone()[0] == "applied"
    assert conn.execute("SELECT COUNT(*) FROM application_event").fetchone()[0] == 1


def test_did_you_apply_banner_logic(client, conn):
    assert "Did you apply?" not in client.get("/job/5").text
    client.get("/apply/5")
    assert "Did you apply?" in client.get("/job/5").text
    client.post("/job/5/applied?choice=not_yet")
    assert "Did you apply?" not in client.get("/job/5").text


def test_banner_hidden_when_event_newer_than_click(client, conn):
    add_app(conn, 5)
    conn.execute(
        "INSERT INTO apply_click (job_group_id, at, final_url, outcome) "
        "VALUES (5, ?, 'u', 'redirected')",
        ((NOW - timedelta(days=2)).isoformat(),),
    )
    assert "Did you apply?" not in client.get("/job/5").text


def test_post_applied_yes(client, conn):
    add_app(conn, 5, "preparing")
    client.get("/apply/5")
    r = client.post("/job/5/applied?choice=yes")
    assert r.status_code == 200 and "Marked as applied" in r.text
    app = conn.execute("SELECT status, applied_at FROM application").fetchone()
    assert app["status"] == "applied" and app["applied_at"]
    last = conn.execute("SELECT status FROM application_event ORDER BY id DESC").fetchone()
    assert last[0] == "applied"
    assert "Did you apply?" not in client.get("/job/5").text


def test_yes_never_moves_an_interview_back_to_applied(client, conn):
    """950d5eb (7): a repeat Apply click on an application at interview asks nothing, and a
    Yes from a stale banner leaves its status, applied date and events alone."""
    add_app(conn, 5, "interview")
    conn.execute("UPDATE application SET applied_at = '2026-09-01T00:00:00+00:00'")
    client.get("/apply/5")
    assert "Did you apply?" not in client.get("/job/5").text
    before = conn.execute("SELECT count(*) FROM application_event").fetchone()[0]
    assert client.post("/job/5/applied?choice=yes").status_code == 200
    app = conn.execute("SELECT status, applied_at FROM application").fetchone()
    assert tuple(app) == ("interview", "2026-09-01T00:00:00+00:00")
    assert conn.execute("SELECT count(*) FROM application_event").fetchone()[0] == before


def test_post_applied_without_application(client, conn):
    client.get("/apply/6")
    client.post("/job/6/applied?choice=yes")
    app = conn.execute("SELECT status, applied_at FROM application WHERE job_group_id = 6")
    row = app.fetchone()
    assert row["status"] == "applied" and row["applied_at"]


def test_post_not_interested(client, conn):
    client.get("/apply/5")
    client.post("/job/5/applied?choice=not_interested")
    label = conn.execute("SELECT label FROM label WHERE job_group_id = 5").fetchone()[0]
    assert label == "not_interesting"
    assert "Did you apply?" not in client.get("/job/5").text


def test_post_bad_choice(client):
    assert client.post("/job/5/applied?choice=maybe").status_code == 422
    assert client.post("/job/404/applied?choice=yes").status_code == 404


def test_keyboard_script_served(client):
    t = client.get("/job/1").text
    assert "detail.js" in t and 'data-posting-url="https://example.com/1"' in t
    js = client.get("/static/detail.js").text
    assert "o:" in js and "A:" in js and "b:" in js


# ─── partial descriptions (specs/012) ──────────────────────────────────────


def test_partial_banner_and_paste_description(client, conn):
    conn.execute("UPDATE job SET description_completeness = 'partial' WHERE id = 2")
    page = client.get("/job/2").text
    assert "partial: open to confirm" in page
    assert 'action="/job/2/description"' in page and "<textarea" in page
    assert "partial: open to confirm" not in client.get("/job/3").text

    r = client.post("/job/2/description", data={"text": "Pasted <b>full</b> posting.\n\nMore."})
    assert r.status_code == 303 and r.headers["location"] == "/job/2"
    job = conn.execute("SELECT * FROM job WHERE id = 2").fetchone()
    assert job["description_completeness"] == "pasted"
    assert job["description_text"].startswith("Pasted <b>full</b> posting.")
    rev = conn.execute("SELECT description_rev FROM job_group WHERE id = 2").fetchone()[0]
    assert rev == 1
    page = client.get("/job/2").text
    assert "pasted description" in page and "partial: open to confirm" not in page


def test_paste_description_rejects_empty_and_unknown(client):
    assert client.post("/job/2/description", data={"text": "  "}).status_code == 422
    assert client.post("/job/999/description", data={"text": "x"}).status_code == 404


def test_decisions_score_shows_probabilities_not_quotes(client, conn):
    report = {
        "verdict": {"choice": "strong", "probabilities": {"strong": 0.97}, "confidence": 0.96},
        "answers": {"avoid.pure_windows": {"type": "noul", "noul": 0.12}},
        "labels": {"avoid.pure_windows": "Pure Windows shops"},
    }
    conn.execute(
        "UPDATE fit_score SET evidence_mode = 'none', evidence = '[]', evidence_unverified = 0, "
        "served_model = 'typesafe/jev-1.13-20260917', decisions = ? WHERE job_group_id = 2",
        (json.dumps(report),),
    )
    t = client.get("/job/2").text
    assert "Jev: fit strong (0.97, confidence 0.96)" in t
    assert "Decision probabilities" in t and "Pure Windows shops" in t and "0.12" in t
    assert "typesafe/jev-1.13-20260917" in t
    assert "unverified evidence" not in t


def test_salary_parsed_from_description_is_labelled_visibly(client, conn):
    conn.execute(
        "UPDATE job SET salary_stated = 1, salary_min = 170000, salary_max = 195000, "
        "salary_period = 'year', salary_source = 'text' WHERE id = 1"
    )
    t = client.get("/job/1").text
    assert "$170k\u2013$195k" in t
    # visible text, not only a hover title: phones have no hover
    assert '<span class="muted">(parsed from the description)</span>' in t
    assert "computed from the salary parsed from the description" in t
    t2 = client.get("/job/2").text
    assert "parsed from the description" not in t2


def test_unscored_job_still_shows_the_employer_rejection_flag(client, conn):
    # Regression: the flag sat inside {% if d.scored %}, so an unscored job at an employer
    # that recently rejected you showed only "Not scored yet." (specs/006).
    from jobhunter.core import rejections

    conn.execute("DELETE FROM fit_score WHERE job_group_id = 2")
    rejections.record(
        conn, received_at=NOW.isoformat(), employer="Acme", title="Analyst 1", source="manual",
        now=NOW,
    )  # fmt: skip
    conn.commit()
    t = client.get("/job/2").text
    assert "Not scored yet." in t
    assert "employer rejected you for Analyst 1" in t


# ─── why this link, Use a different apply link, Reset (bug 334d9eb) ─────────

LOGIN = "https://id.utah.gov/login/?goto=https%3A%2F%2Flogin.dts.utah.gov%2Fsso&realm=/"
MINE = "https://www.example-employer.com/careers/42"


def test_button_shows_why_the_link_was_chosen(client, conn):
    add_link(conn, 2, "live")
    why = "via nlx.example → acme.wd5.myworkdayjobs.com · employer's applicant tracking system"
    conn.execute("UPDATE apply_link SET why = ? WHERE job_group_id = 2", (why,))
    t = client.get("/job/2").text
    assert 'id="apply-why"' in t and "via nlx.example → acme.wd5.myworkdayjobs.com" in t
    assert 'action="/job/2/apply-link"' in t and "Use a different apply link" in t


def test_user_link_set_shown_used_and_reset(client, conn, pages):
    add_link(conn, 2, "live", verified_hours=48)
    r = client.post("/job/2/apply-link", data={"url": MINE})
    assert r.status_code == 303 and r.headers["location"] == "/job/2"
    row = conn.execute("SELECT * FROM apply_link WHERE job_group_id = 2").fetchone()
    assert (row["final_url"], row["chosen_by"], row["why"]) == (MINE, "user", "set by you")
    t = client.get("/job/2").text
    assert "set by you" in t and 'id="apply-link-reset"' in t
    assert "Use a different apply link" not in t
    r = client.get("/apply/2")
    assert r.status_code == 302 and r.headers["location"] == MINE
    assert pages.hits == []  # the user's link is never re-verified
    r = client.post("/job/2/apply-link", data={"reset": "1"})
    assert r.status_code == 303
    assert conn.execute("SELECT 1 FROM apply_link WHERE job_group_id = 2").fetchone() is None
    t = client.get("/job/2").text
    assert 'hx-get="/job/2/apply-button"' in t  # resolves again on demand


@pytest.mark.parametrize("bad", ["", "javascript:alert(1)", "mailto:x@example.com", "x y"])
def test_user_link_rejects_non_http(client, conn, bad):
    r = client.post("/job/2/apply-link", data={"url": bad})
    assert r.status_code == 422
    assert conn.execute("SELECT 1 FROM apply_link WHERE job_group_id = 2").fetchone() is None


def test_user_link_refuses_cross_site_and_unknown_job(client, conn):
    r = client.post(
        "/job/2/apply-link",
        data={"url": MINE},
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert conn.execute("SELECT 1 FROM apply_link WHERE job_group_id = 2").fetchone() is None
    assert client.post("/job/404/apply-link", data={"url": MINE}).status_code == 404


def test_legacy_login_wall_row_resolves_again_on_the_job_page(client, conn, pages, monkeypatch):
    """An older row took the state sign-in page for the posting: the page shows it as pending
    and the fragment resolves the group again, without requesting the login page."""
    add_link(conn, 1, "live", final=LOGIN, ats="unknown")
    t = client.get("/job/1").text
    assert 'hx-get="/job/1/apply-button"' in t and "id.utah.gov" not in t
    # Nothing answers yet: Apply falls back to the start URL, never the login page, and the
    # old row is kept to be tried again.
    r = client.get("/apply/1")
    assert r.headers["location"] == "https://nlx.example/start"
    pages.responses["https://example.com/1"] = httpx.Response(302, headers={"Location": WD})
    pages.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    # A walk that found no answer is not repeated within the day (e0d047b review N6)...
    hits = len(pages.hits)
    frag = client.get("/job/1/apply-button").text
    assert len(pages.hits) == hits and "Apply on Workday" not in frag and "hx-get" not in frag
    # ...and is once the day is over.
    monkeypatch.setattr(apply_bg, "WALK_BACKOFF_S", 0.0)
    frag = client.get("/job/1/apply-button").text
    assert "Apply on Workday" in frag
    assert not any("utah.gov" in h for h in pages.hits)
    row = conn.execute("SELECT final_url, why FROM apply_link WHERE job_group_id = 1").fetchone()
    assert row["final_url"] == WD + "/apply" and row["why"]


def test_open_application_on_a_legacy_login_row_resolves_on_click(client, conn, pages):
    """The reported path: a packet's Open application is /apply/{id}. Right after the upgrade,
    with no visit to the job page and no nightly run, it must reach the employer's ATS."""
    add_link(conn, 1, "live", final=LOGIN, ats="unknown")
    pages.responses["https://example.com/1"] = httpx.Response(302, headers={"Location": WD})
    pages.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    r = client.get("/apply/1")
    assert r.status_code == 302 and r.headers["location"] == WD + "/apply"
    assert not any("utah.gov" in h for h in pages.hits)
    click = conn.execute("SELECT final_url FROM apply_click WHERE job_group_id = 1").fetchone()
    assert click["final_url"] == WD + "/apply"


def test_closed_page_does_not_blame_the_employer_for_a_dead_board_link(client, conn):
    dead = "https://www.aplitrak.com/?adid=synthetic"
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, status, "
        "resolved_at) VALUES (3, ?, ?, '[]', 'unknown', 'expired', ?)",
        (dead, dead, NOW.isoformat()),
    )
    t = client.get("/apply/3").text
    assert "This link no longer works" in t and "www.aplitrak.com" in t
    assert "employer's page" not in t and "does not mean the job has closed" in t
    add_link(conn, 4, "expired")  # on the employer's ATS: the posting really closed
    t = client.get("/apply/4").text
    assert "This posting has closed" in t and "employer's page" in t


# ─── bug e0d047b: a busy run or a slow site never holds up a click ─────────────────────


class SlowSites(Pages):
    """Pages whose every non-robots request waits on ``gate``: a site that does not answer
    until the test lets it (or 10 s pass)."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/robots.txt":
            self.gate.wait(10)
        return super().handle(request)


@pytest.fixture
def slow(client, monkeypatch):
    monkeypatch.setattr(apply_bg, "WAIT_S", 0.3)
    monkeypatch.setattr(apply_bg, "REVERIFY_WAIT_S", 0.3)
    monkeypatch.setattr(apply_bg, "CLICK_RETRY_GAP_S", 0.05)
    app = client.app
    settings = app.state.settings
    sites = SlowSites()
    opened = []

    def factory(url):
        c = db.connect(settings.paths.db_path)
        opened.append(c)
        return fetch_context_for_url(
            settings,
            c,
            url,
            transport=httpx.MockTransport(sites.handle),
            clock=lambda: 0.0,
            sleep=lambda s: None,
        )

    app.state.ctx_factory = factory
    yield sites
    sites.gate.set()
    assert app.state.apply_link_worker.wait_idle(15)
    for c in opened:
        c.close()


def timed_get(client, path):
    t0 = time.monotonic()
    r = client.get(path)
    return r, time.monotonic() - t0


def test_apply_redirects_promptly_while_reverify_hangs(client, conn, slow):
    """A stale live link: the click goes to the stored link within the wait budget, and the
    re-verify finishes in the background (the old code held the request for the whole fetch)."""
    add_link(conn, 2, "live", verified_hours=48)
    slow.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    r, took = timed_get(client, "/apply/2")
    assert r.status_code == 302 and r.headers["location"] == WD
    assert took < 3.0
    click = conn.execute("SELECT outcome FROM apply_click WHERE job_group_id = 2").fetchone()
    assert click["outcome"] == "redirected"
    slow.gate.set()
    assert client.app.state.apply_link_worker.wait_idle(10)
    row = conn.execute("SELECT verified_at FROM apply_link WHERE job_group_id = 2").fetchone()
    assert row["verified_at"].startswith("2026-10-09T12:00")


def test_button_fragment_and_click_share_one_walk_and_never_hang(client, conn, slow):
    """A legacy login-wall row, slow sites: the fragment answers pending (and asks again), the
    click goes to the start URL at once, and both share one walk of the group's links."""
    add_link(conn, 1, "live", final=LOGIN, ats="unknown")
    slow.responses["https://example.com/1"] = httpx.Response(302, headers={"Location": WD})
    slow.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    frag, took = timed_get(client, "/job/1/apply-button")
    assert frag.status_code == 200 and took < 3.0
    assert 'hx-get="/job/1/apply-button"' in frag.text and "resolving" in frag.text
    r, took = timed_get(client, "/apply/1")
    assert r.status_code == 302 and r.headers["location"] == "https://nlx.example/start"
    assert took < 3.0
    slow.gate.set()
    assert client.app.state.apply_link_worker.wait_idle(10)
    assert slow.hits.count("https://example.com/1") == 1  # one walk, not one per request
    frag = client.get("/job/1/apply-button").text
    assert "Apply on Workday" in frag and "hx-get" not in frag


def test_apply_click_survives_a_run_holding_the_write_lock(client, conn, db_path, slow):
    """Another process (a nightly run) holds the write lock: the click still redirects at once
    and its log row lands once the lock is free (the old code failed with 'database is
    locked' after busy_timeout)."""
    add_link(conn, 2, "live", verified_hours=1)
    run = db.connect(db_path)
    run.execute("BEGIN IMMEDIATE")
    try:
        r, took = timed_get(client, "/apply/2")
        assert r.status_code == 302 and r.headers["location"] == WD
        assert took < 3.0
    finally:
        run.execute("COMMIT")
        run.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if conn.execute("SELECT COUNT(*) FROM apply_click").fetchone()[0]:
            break
        time.sleep(0.05)
    click = conn.execute("SELECT outcome, final_url FROM apply_click").fetchone()
    assert click["outcome"] == "redirected" and click["final_url"] == WD


def test_click_reverify_has_its_own_longer_budget(client, conn, slow, monkeypatch):
    """A re-verify slower than a walk's budget (the first request to a host also fetches
    robots.txt and waits a rate interval) still decides this click (review F2b)."""
    monkeypatch.setattr(apply_bg, "REVERIFY_WAIT_S", 7.0)
    add_link(conn, 2, "live", verified_hours=48)
    slow.responses[WD] = httpx.Response(404)
    timer = threading.Timer(0.8, slow.gate.set)
    timer.start()
    r, took = timed_get(client, "/apply/2")
    timer.join()
    assert r.status_code == 200 and "posting has closed" in r.text
    assert 0.8 <= took < 5.0


@pytest.mark.parametrize("path", ["/job/2", "/job/2/apply-button"])
def test_showing_a_stale_link_reverifies_it_in_the_background(client, conn, slow, path):
    """The job page (or its fragment) starts a re-verify without waiting, so the next click
    finds the link fresh (review F2c)."""
    add_link(conn, 2, "live", verified_hours=48)
    slow.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    r, took = timed_get(client, path)
    assert r.status_code == 200 and "Apply on Workday" in r.text and took < 1.0
    assert client.app.state.apply_link_worker.in_flight() == 1
    slow.gate.set()
    assert client.app.state.apply_link_worker.wait_idle(10)
    assert slow.hits == [WD]
    row = conn.execute("SELECT verified_at FROM apply_link WHERE job_group_id = 2").fetchone()
    assert row["verified_at"].startswith("2026-10-09T12:00")


def test_background_save_never_replaces_the_users_link(client, conn, slow):
    """A re-verify read the row, then the user set a link: the re-verify's save must not
    overwrite it (review N1)."""
    add_link(conn, 2, "live", verified_hours=48)
    slow.responses[WD] = httpx.Response(200, html="<html><body>Analyst</body></html>")
    client.get("/job/2")  # starts the re-verify, which blocks on the site
    assert client.post("/job/2/apply-link", data={"url": MINE}).status_code == 303
    slow.gate.set()
    assert client.app.state.apply_link_worker.wait_idle(10)
    assert slow.hits == [WD]  # the re-verify did run and save
    row = conn.execute("SELECT final_url, chosen_by FROM apply_link WHERE job_group_id = 2")
    row = row.fetchone()
    assert row["final_url"] == MINE and row["chosen_by"] == "user"


def test_save_keeps_a_user_link_but_the_user_can_replace_it(conn):
    from jobhunter.pipeline import applylink

    applylink.set_user_link(conn, 2, MINE, NOW)
    auto = applylink.get_apply_link(conn, 2).model_copy(
        update={"final_url": WD, "chosen_by": None, "why": "ranked"}
    )
    applylink._save(conn, auto)
    assert applylink.get_apply_link(conn, 2).final_url == MINE
    applylink.set_user_link(conn, 2, MINE + "/v2", NOW)
    assert applylink.get_apply_link(conn, 2).final_url == MINE + "/v2"


def test_walks_in_flight_are_capped(client, conn, slow, monkeypatch):
    """Past the cap a request starts nothing and answers with the stored link (review N2)."""
    monkeypatch.setattr(apply_bg, "MAX_IN_FLIGHT", 3)
    for gid in range(1, 7):
        add_link(conn, gid, "live", final=LOGIN, ats="unknown")
    frags = {}
    for gid in range(1, 7):
        r, took = timed_get(client, f"/job/{gid}/apply-button")
        assert r.status_code == 200 and took < 2.0
        frags[gid] = r.text
    worker = client.app.state.apply_link_worker
    assert worker.in_flight() == 3
    for gid in (1, 2, 3):
        assert "resolving" in frags[gid]  # still walking: the fragment asks again
    for gid in (4, 5, 6):
        assert "resolving" not in frags[gid] and "hx-get" not in frags[gid]
    r, took = timed_get(client, "/apply/5")
    assert r.status_code == 302 and r.headers["location"] == "https://nlx.example/start"
    assert took < 1.0 and worker.in_flight() == 3


def test_a_thread_that_cannot_start_leaves_nothing_behind(monkeypatch):
    """No thread could start: nothing is left in flight and a later request runs (review N3)."""

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    worker = apply_bg.ApplyLinkWorker(lambda: db.connect(":memory:"), lambda c: None)
    ran = []
    real = apply_bg.threading
    fake = type("T", (), {"Thread": NoThread, "Event": real.Event, "Lock": real.Lock})
    monkeypatch.setattr(apply_bg, "threading", fake)
    assert worker.run(7, lambda c, cf: ran.append(1), wait_s=1) == "skipped"
    assert worker.in_flight() == 0
    monkeypatch.setattr(apply_bg, "threading", real)
    assert worker.run(7, lambda c, cf: ran.append(1), wait_s=5) == "done"
    assert ran == [1]
