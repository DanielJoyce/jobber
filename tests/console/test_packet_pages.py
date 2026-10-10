"""Assisted apply phase 1a in the console (specs/017): Prepare, New packet, packet page, Score now.

No network: Fetch posting text goes through a FetchContext on an httpx.MockTransport, and the
scorer is a fake injected into ``app.state``. Nothing here can reach a paid API.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Scoring, Settings
from jobhunter.console import dashboard as dash
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state
from jobhunter.scoring.profile import load_profile
from jobhunter.scoring.scorers import ScoreResult

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
SPEC = "test:model"
QUOTE = "Write Terraform modules and Go tooling."
TEXT = f"Own the Linux fleet for a synthetic company.\n\n{QUOTE}\n\nBenefits include dental."
GH = "https://boards.greenhouse.io/acme/jobs/4012345"
PREFS = """\
resume_path: resume.md
target_titles: [Systems Engineer]
hard:
  states_allowed: [CO]
  remote_ok: true
soft:
  state_ranking: [CO]
narrative:
  want: Synthetic systems work.
"""
DIMS = {k: {"score": 90, "why": "match: ok"} for k in ("skills", "seniority", "domain")} | {
    "raw_skills": 90,
    "recency_weighted_skills": 90,
    "stale_skills": [],
}
BODY = json.dumps(
    {
        "verdict": "strong",
        "dimensions": {k: DIMS[k] for k in ("skills", "seniority", "domain")},
        "raw_skills": 90,
        "recency_weighted_skills": 90,
        "stale_skills": [],
        "current_focus_overlap": 50,
        "done_with_hits": [],
        "evidence": [{"claim": "tooling", "quote": QUOTE}],
        "blockers": [],
        "missing_info": [],
        "shape_flags": [],
        "tailoring_hints": [],
    }
)


class FakeScorer:
    name = SPEC

    def __init__(self, fail: bool = False):
        self.calls: list[list[str]] = []
        self.fail = fail

    def submit(self, requests):
        self.calls.append([r.custom_id for r in requests])
        if self.fail:
            return [ScoreResult(custom_id=r.custom_id, status="errored") for r in requests]
        return [ScoreResult(custom_id=r.custom_id, status="succeeded", text=BODY) for r in requests]

    def cost(self, usage, batch):
        return 0.001


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


def add_group(c, n, *, title, url, text=TEXT, scored=False, state="CO"):
    c.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "location_scope, employment_type, stage, first_seen_at, last_seen_at) "
        "VALUES (?, 'co', ?, ?, ?, 'Acme', ?, 'single', 'full_time', 'grouped', ?, ?)",
        (n, str(n), url, title, text, ISO, ISO),
    )
    c.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ISO),
    )
    c.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    c.execute("INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (n, state))
    if scored:
        c.execute(
            "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
            "verdict, overall, dimensions, evidence, created_at) "
            "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 80, ?, '[]', ?)",
            (n, json.dumps(DIMS), ISO),
        )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    add_group(c, 1, title="Ingested Analyst", url="https://example.com/1", scored=True)
    add_group(c, 2, title="Unscored Analyst", url="https://example.com/2")
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


class Pages:
    def __init__(self):
        self.responses: dict[str, httpx.Response] = {}
        self.robots = "User-agent: *\nAllow: /\n"
        self.hits: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        url = str(request.url)
        self.hits.append(url)
        return self.responses.get(url, httpx.Response(404))


@pytest.fixture
def pages():
    return Pages()


def make_client(pdir, db_path, pages, **scoring) -> TestClient:
    scoring.setdefault("screen_scorer", SPEC)
    settings = Settings(
        paths=Paths(profile_dir=pdir, db_path=db_path, cache_dir=pdir.parent / "cache"),
        scoring=Scoring(**scoring),
    )
    app = create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW)
    ctx_conn = db.connect(db_path)

    def factory(url):
        return fetch_context_for_url(
            settings,
            ctx_conn,
            url,
            transport=httpx.MockTransport(pages.handle),
            clock=lambda: 0.0,
            sleep=lambda s: None,
        )

    app.state.ctx_factory = factory
    app.state.packet_scorer_factory = lambda spec: pytest.fail("no scorer was expected")
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(pdir, db_path, pages):
    return make_client(pdir, db_path, pages)


def new_packet(client, **fields):
    r = client.post("/apply/new", data=fields)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def packet_group(conn, pid):
    return conn.execute(
        "SELECT a.job_group_id FROM application_packet p "
        "JOIN application a ON a.id = p.application_id WHERE p.id = ?",
        (pid,),
    ).fetchone()[0]


def job_of(conn, gid):
    return conn.execute(
        "SELECT j.* FROM job_group g JOIN job j ON j.id = g.canonical_job_id WHERE g.id = ?",
        (gid,),
    ).fetchone()


# ─── Prepare ────────────────────────────────────────────────────────────────


def test_prepare_opens_application_at_preparing_and_a_packet(client, conn):
    r = client.post("/job/2/prepare")
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    app = conn.execute("SELECT * FROM application WHERE job_group_id = 2").fetchone()
    assert app["status"] == "preparing"
    events = conn.execute(
        "SELECT status, note FROM application_event WHERE application_id = ?", (app["id"],)
    ).fetchall()
    assert [tuple(e) for e in events] == [("preparing", "packet started")]
    assert conn.execute("SELECT label FROM label WHERE job_group_id = 2").fetchone()[0] == (
        "interesting"
    )
    # Pressing it again reuses the same packet.
    again = client.post("/job/2/prepare")
    assert again.headers["location"] == f"/packet/{pid}"
    assert conn.execute("SELECT COUNT(*) FROM application_packet").fetchone()[0] == 1
    page = client.get(f"/packet/{pid}")
    assert page.status_code == 200
    assert "Unscored Analyst" in page.text and "packet draft" in page.text
    assert "Coming in 1b" in page.text


def test_prepare_moves_a_shortlisted_application_forward_and_keeps_later_ones(client, conn):
    client.post("/inbox/1/label?label=interesting")
    # The inbox stamps the shortlist with the wall clock; put it before this test's fixed NOW.
    conn.execute("UPDATE application_event SET at = '2026-10-01T00:00:00+00:00'")
    client.post("/job/1/prepare")
    assert conn.execute("SELECT status FROM application WHERE job_group_id = 1").fetchone()[0] == (
        "preparing"
    )
    conn.execute("UPDATE application SET status = 'applied' WHERE job_group_id = 1")
    client.post("/job/1/prepare")
    assert conn.execute("SELECT status FROM application WHERE job_group_id = 1").fetchone()[0] == (
        "applied"
    )


def test_prepare_unknown_group_is_404(client):
    assert client.post("/job/999/prepare").status_code == 404
    assert client.get("/packet/999").status_code == 404


def test_job_page_has_prepare_button_then_open_packet_link(client):
    t = client.get("/job/2").text
    assert 'action="/job/2/prepare"' in t and "Prepare packet (p)" in t
    pid = int(client.post("/job/2/prepare").headers["location"].rsplit("/", 1)[1])
    t = client.get("/job/2").text
    assert f'href="/packet/{pid}"' in t and 'action="/job/2/prepare"' not in t


def test_inbox_has_new_packet_and_prepare_keys(client):
    t = client.get("/inbox").text
    assert 'href="/apply/new"' in t and "<kbd>p</kbd> prepare packet" in t
    js = client.get("/static/inbox.js").text
    assert '"/prepare"' in js and '"/apply/new"' in js


def test_pipeline_drawer_links_the_packet(client):
    pid = int(client.post("/job/2/prepare").headers["location"].rsplit("/", 1)[1])
    t = client.get("/pipeline").text
    app_id = re.search(r'data-app="(\d+)"', t)
    assert app_id
    drawer = client.get(f"/pipeline/{app_id.group(1)}").text
    assert f'href="/packet/{pid}"' in drawer


# ─── New packet ─────────────────────────────────────────────────────────────


def test_new_packet_text_only(client, conn):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    gid = packet_group(conn, pid)
    job = job_of(conn, gid)
    assert job["source_key"] == "paste-manual"
    assert job["stage"] == "normalized"
    assert job["url"] == f"paste:{pid}"
    assert QUOTE in job["description_text"]
    assert (
        conn.execute("SELECT description_rev FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT method FROM job_group WHERE id = ?", (gid,)).fetchone()[0] == (
        "manual"
    )
    assert (
        conn.execute("SELECT 1 FROM apply_link WHERE job_group_id = ?", (gid,)).fetchone() is None
    )
    assert (
        conn.execute("SELECT status FROM application WHERE job_group_id = ?", (gid,)).fetchone()[0]
        == "preparing"
    )
    # No URL: no Open application, and /apply/{id} never opens the placeholder.
    page = client.get(f"/packet/{pid}").text
    assert 'id="open-application"' not in page and "No posting URL was given" in page
    assert client.get(f"/apply/{gid}").status_code == 404
    job_page = client.get(f"/job/{gid}").text
    assert 'id="apply-link"' not in job_page and "paste:" not in job_page


def test_new_packet_url_only_writes_apply_link_and_open_application(client, conn):
    embed = "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345"
    pid = new_packet(client, url=embed, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    job = job_of(conn, gid)
    assert job["url"] == GH  # the embed URL rewritten to the direct board URL
    assert job["description_text"] in (None, "")
    link = conn.execute("SELECT * FROM apply_link WHERE job_group_id = ?", (gid,)).fetchone()
    assert (link["start_url"], link["status"], link["ats"]) == (GH, "unresolved", "greenhouse")
    page = client.get(f"/packet/{pid}").text
    assert f'href="/apply/{gid}"' in page
    assert 'id="needs-text"' in page  # asks for the posting text
    r = client.get(f"/apply/{gid}")
    assert r.status_code == 302 and r.headers["location"] == GH


def test_new_packet_needs_url_or_text_and_employer_and_title(client, conn):
    r = client.post("/apply/new", data={"employer": "Acme", "title": "Engineer"})
    assert r.status_code == 422 and "URL or paste its text" in r.text
    r = client.post("/apply/new", data={"text": TEXT, "employer": "Acme"})
    assert r.status_code == 422 and "both required" in r.text
    r = client.post("/apply/new", data={"url": "ftp://x", "employer": "A", "title": "B"})
    assert r.status_code == 422
    assert conn.execute("SELECT COUNT(*) FROM application_packet").fetchone()[0] == 0


def test_new_packet_dedup_by_url_offers_the_existing_group(client, conn):
    conn.execute("UPDATE job SET apply_url = ? WHERE id = 1", (GH,))
    r = client.post(
        "/apply/new", data={"url": GH + "#app", "employer": "Other", "title": "Other role"}
    )
    assert r.status_code == 409
    assert 'href="/job/1"' in r.text and "same URL" in r.text
    assert 'action="/job/1/prepare"' in r.text
    assert 'id="force-btn"' not in r.text  # same URL: use that posting
    r = client.post(
        "/apply/new", data={"url": GH, "employer": "Other", "title": "Other role", "force": "1"}
    )
    assert r.status_code == 409
    assert (
        conn.execute("SELECT COUNT(*) FROM job WHERE source_key = 'paste-manual'").fetchone()[0]
        == 0
    )


def test_new_packet_dedup_by_employer_and_title_can_be_overridden(client, conn):
    data = {"text": TEXT, "employer": "ACME", "title": "Ingested  Analyst"}
    r = client.post("/apply/new", data=data)
    assert r.status_code == 409 and "same employer and title" in r.text
    assert 'id="force-btn"' in r.text
    pid = new_packet(client, **data, force="1")
    assert job_of(conn, packet_group(conn, pid))["source_key"] == "paste-manual"


def test_dup_with_a_packet_links_to_that_packet(client, conn):
    pid = int(client.post("/job/1/prepare").headers["location"].rsplit("/", 1)[1])
    r = client.post(
        "/apply/new", data={"text": TEXT, "employer": "Acme", "title": "Ingested Analyst"}
    )
    assert f'href="/packet/{pid}"' in r.text


def test_fetch_posting_text_fills_the_form(client, pages):
    html = (
        "<html><head><title>Job Application for Systems Engineer at Acme</title></head>"
        f"<body><nav>menu</nav><main><p>{TEXT}</p><p>{'More detail. ' * 30}</p></main>"
        "</body></html>"
    )
    pages.responses[GH] = httpx.Response(200, text=html, headers={"content-type": "text/html"})
    r = client.post("/apply/new/fetch", data={"url": GH})
    assert r.status_code == 200, r.text
    assert "Fetched." in r.text
    assert QUOTE in r.text and 'value="Systems Engineer"' in r.text and 'value="Acme"' in r.text
    assert "menu" not in r.text.split('id="new-text"')[1].split("</textarea>")[0]
    assert pages.hits == [GH]


def test_fetch_refused_by_robots_asks_to_paste(client, pages):
    pages.robots = "User-agent: *\nDisallow: /\n"
    r = client.post("/apply/new/fetch", data={"url": GH, "employer": "Acme"})
    assert r.status_code == 200
    assert "robots.txt does not allow" in r.text and "paste the posting text" in r.text
    assert pages.hits == []  # only robots.txt was read
    assert 'value="Acme"' in r.text  # what was typed is kept


def test_fetch_of_an_empty_javascript_page_asks_to_paste(client, pages):
    url = "https://jobs.ashbyhq.com/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    pages.responses[url] = httpx.Response(200, text="<html><body><div id=root></div></body></html>")
    r = client.post("/apply/new/fetch", data={"url": url})
    assert "returned no posting text" in r.text


def test_cross_site_posts_are_refused(client, conn):
    evil = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}
    for path, data in (
        ("/apply/new", {"text": TEXT, "employer": "A", "title": "B"}),
        ("/apply/new/fetch", {"url": GH}),
        ("/job/2/prepare", {}),
        ("/packet/1/score", {"token": "x"}),
        ("/packet/1/posting", {"text": "x"}),
    ):
        assert client.post(path, data=data, headers=evil).status_code == 403, path
    assert conn.execute("SELECT COUNT(*) FROM application").fetchone()[0] == 0


def test_packet_pages_are_not_cached(client):
    pid = int(client.post("/job/2/prepare").headers["location"].rsplit("/", 1)[1])
    for path in ("/apply/new", f"/packet/{pid}"):
        assert client.get(path).headers["cache-control"] == "no-store"


def test_posting_text_paste_on_packet_page_does_not_queue_a_rescore(client, conn):
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    r = client.post(f"/packet/{pid}/posting", data={"text": TEXT})
    assert r.status_code == 303
    job = job_of(conn, gid)
    assert QUOTE in job["description_text"] and job["stage"] == "normalized"
    assert (
        conn.execute("SELECT description_rev FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
        == 0
    )
    assert 'id="needs-text"' not in client.get(f"/packet/{pid}").text


# ─── Score this group now ──────────────────────────────────────────────────


def token_of(page: str) -> str:
    m = re.search(r'name="token" value="([0-9a-f]+)"', page)
    assert m, "no Score this group now form"
    return m.group(1)


def test_score_now_shows_estimate_and_scores_one_group(client, conn, pdir):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    gid = packet_group(conn, pid)
    page = client.get(f"/packet/{pid}").text
    assert 'id="score-estimate"' in page and "This spends credits." in page
    assert "$0.0010" in page or "$0.00" in page
    scorer = FakeScorer()
    client.app.state.packet_scorer_factory = lambda spec: scorer
    r = client.post(
        f"/packet/{pid}/score", data={"token": token_of(page)}, headers={"hx-request": "true"}
    )
    assert r.status_code == 200
    assert scorer.calls == [[f"g{gid}"]]  # exactly this group, once
    assert 'id="score-result"' in r.text
    pre = conn.execute(
        "SELECT passed, reasons, filter_version FROM prefilter_result WHERE job_id = ?",
        (job_of(conn, gid)["id"],),
    ).fetchone()
    assert (pre[0], json.loads(pre[1])) == (1, ["user-requested"])
    assert pre[2] == load_profile(pdir).filter_version
    assert job_of(conn, gid)["stage"] == "scored"
    assert (
        conn.execute("SELECT COUNT(*) FROM fit_score WHERE job_group_id = ?", (gid,)).fetchone()[0]
        == 1
    )
    # Scored: the panel shows the result, not the button.
    assert 'id="score-now"' not in client.get(f"/packet/{pid}").text


def test_score_now_needs_the_confirmed_estimate(client, conn):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    r = client.post(f"/packet/{pid}/score", data={"token": "stale"}, headers={"hx-request": "true"})
    assert "estimate changed" in r.text
    assert conn.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 0


def test_score_now_over_the_cap_is_refused_and_writes_nothing(pdir, db_path, pages, conn):
    c = make_client(pdir, db_path, pages, daily_cap_usd=0.0)
    pid = new_packet(c, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    page = c.get(f"/packet/{pid}").text
    assert 'id="score-refusal"' in page and "over the remaining spend cap" in page
    assert re.search(r'id="score-now"\s+disabled', page)
    r = c.post(
        f"/packet/{pid}/score", data={"token": token_of(page)}, headers={"hx-request": "true"}
    )
    assert "over the remaining spend cap" in r.text
    assert conn.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fit_score").fetchone()[0] == 1  # the seeded one


def test_failed_score_restores_prefilter_and_stage(client, conn):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    gid = packet_group(conn, pid)
    token = token_of(client.get(f"/packet/{pid}").text)
    client.app.state.packet_scorer_factory = lambda spec: FakeScorer(fail=True)
    r = client.post(f"/packet/{pid}/score", data={"token": token}, headers={"hx-request": "true"})
    assert "nothing is left waiting to be scored" in r.text
    assert conn.execute("SELECT COUNT(*) FROM prefilter_result").fetchone()[0] == 0
    assert job_of(conn, gid)["stage"] == "normalized"


def test_score_now_needs_posting_text(client, conn):
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    page = client.get(f"/packet/{pid}").text
    assert "paste it first" in page and re.search(r'id="score-now"\s+disabled', page)


def test_scored_group_page_has_no_score_button(client):
    pid = int(client.post("/job/1/prepare").headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/packet/{pid}").text
    assert 'id="score-result"' in page and 'id="score-now"' not in page


# ─── Sankey ─────────────────────────────────────────────────────────────────


def nodes(conn, pdir):
    s = dash.sankey(conn, load_profile(pdir))
    return (
        {n["id"]: n["value"] for n in s["nodes"]},
        {(link["source"], link["target"]): link["value"] for link in s["links"]},
        s["total"],
    )


def test_pasted_node_only_once_scored(client, conn, pdir):
    pid = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    gid = packet_group(conn, pid)
    values, links, total = nodes(conn, pdir)
    assert "pasted" not in values  # unscored: only in the pipeline
    before = total
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 80, ?, '[]', ?)",
        (gid, json.dumps(DIMS), ISO),
    )
    values, links, total = nodes(conn, pdir)
    assert values["pasted"] == 1 and total == before + 1
    (bucket,) = [b for (a, b) in links if a == "pasted"]
    assert bucket.startswith("bucket_")
    assert links[(bucket, "shortlisted")] >= 1  # Prepare shortlisted it
    # The pasted group never enters through Fetched: only the scored ingested group passes.
    assert links[("fetched", "passed")] == 1 and values["fetched"] == 2


# ─── links ──────────────────────────────────────────────────────────────────


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []
        self.actions: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href", "").startswith("/"):
            self.hrefs.append(a["href"])
        if tag in ("form", "button") and (a.get("action") or a.get("formaction") or "").startswith(
            "/"
        ):
            self.actions.append(a.get("action") or a["formaction"])


def test_every_link_on_the_packet_pages_resolves(client, conn):
    pasted = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    ingested = int(client.post("/job/2/prepare").headers["location"].rsplit("/", 1)[1])
    seen: set[str] = set()
    for page in ("/apply/new", f"/packet/{pasted}", f"/packet/{ingested}", "/job/2", "/inbox"):
        p = _Links()
        p.feed(client.get(page).text)
        for href in p.hrefs:
            if href.startswith("/static/") or href in seen or href.startswith("/apply/"):
                continue
            seen.add(href)
            r = client.get(href)
            assert r.status_code == 200, f"{page} -> {href}: {r.status_code}"
        # every form posts to a route that exists (405/404 would mean a typo)
        for action in p.actions:
            assert client.post(action, data={}).status_code not in (404, 405), f"{page}: {action}"
    gid = packet_group(conn, pasted)
    assert client.get(f"/apply/{gid}").status_code == 302  # Open application
