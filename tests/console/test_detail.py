"""Job detail page, apply button states, /apply redirect and the did-you-apply flow. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Settings
from jobhunter.console import detail
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
    assert "hx-get" not in t


def test_button_expired(client, conn):
    add_link(conn, 3, "expired")
    t = client.get("/job/3").text
    assert "Posting closed" in t and "disabled" in t and "open anyway" in t
    assert "Apply on" not in t


@pytest.mark.parametrize("status", ["unresolved", "blocked"])
def test_button_open_posting(client, conn, status):
    add_link(conn, 4, status, ats=None)
    t = client.get("/job/4").text
    assert "Open posting" in t and "Apply on" not in t and "hx-get" not in t


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
