"""mailalerts adapter end to end against a fake Gmail service and httpx.MockTransport.

Never touches a real mailbox, the OS keyring, or the network.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Settings
from jobhunter.console import detail
from jobhunter.core import db
from jobhunter.core.fetch import FetchContext, reset_shared_state
from jobhunter.core.models import Bucket, Query
from jobhunter.mail import auth, sync
from jobhunter.pipeline import runner
from jobhunter.scoring import buckets, screen
from jobhunter.scoring.profile import load_profile
from jobhunter.sources.adapters.mailalerts import (
    SOURCE_KEY,
    MailAlertsAdapter,
    external_id_for,
    extract_description,
)
from jobhunter.sources.registry import load_registry, sync_sources_table

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mail"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
LABEL = "jobhunter/alerts"
LABEL_ID = "Label_77"
VOS = "vos/employflorida_virtual_recruiter.eml"
JOBLINK = "joblink/illinoisjoblink_saved_search.eml"
NLX = "nlx/usnlx_job_alert.eml"
PA = "generic/pacareerlink_alert.eml"
JOBLINK_HOST = "illinoisjoblink.illinois.gov"
JOBLINK_ROBOTS = "User-agent: *\nDisallow: /search/jobs\nDisallow: /search/resumes\n"
DETAIL_HTML = (
    "<html><body><nav>menu</nav><div class='job-description'><h2>About the role</h2>"
    "<p>Build and operate the container platform that runs our claims systems. You will own "
    "Kubernetes clusters, Terraform modules and the CI pipelines that ship to them, and share "
    "a light on-call rotation with five colleagues.</p><ul><li>5+ years Linux</li>"
    "<li>Kubernetes in production</li></ul></div></body></html>"
)


# ─── fake Gmail ─────────────────────────────────────────────────────────────


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def gmail_full(mid: str, raw: bytes, history_id: int) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        data = base64.urlsafe_b64encode(part.get_content().encode()).decode()
        parts.append({"mimeType": part.get_content_type(), "body": {"data": data}})
    return {
        "id": mid,
        "historyId": str(history_id),
        "labelIds": [LABEL_ID],
        "payload": {
            "mimeType": msg.get_content_type(),
            "headers": [{"name": k, "value": str(v)} for k, v in msg.items()],
            "parts": parts,
        },
    }


class FakeGmail:
    """users().labels/messages/history/getProfile, read-only, with call recording."""

    def __init__(self) -> None:
        self.raw: dict[str, bytes] = {}
        self.order: list[str] = []  # oldest first
        self.events: list[tuple[int, str]] = []
        self.history_id = 100
        self.calls: list[tuple[str, dict]] = []
        self.history_expired = False

    def add(self, mid: str, rel: str | None = None, raw: bytes | None = None) -> None:
        self.history_id += 1
        self.raw[mid] = raw if raw is not None else (FIX / rel).read_bytes()
        self.order.append(mid)
        self.events.append((self.history_id, mid))

    # resource chain
    def users(self):
        return self

    def labels(self):
        return self

    def messages(self):
        return _Messages(self)

    def history(self):
        return _History(self)

    def getProfile(self, userId):
        self.calls.append(("getProfile", {}))
        return _Call(lambda: {"emailAddress": "some.one@example.com", "historyId": self.history_id})

    def list(self, userId):  # labels().list
        return _Call(lambda: {"labels": [{"id": LABEL_ID, "name": LABEL}, {"id": "INBOX"}]})


class _Messages:
    def __init__(self, g: FakeGmail) -> None:
        self.g = g

    def list(self, userId, labelIds, maxResults=100, pageToken=None):
        self.g.calls.append(("messages.list", {"labelIds": labelIds, "pageToken": pageToken}))
        newest_first = list(reversed(self.g.order))
        start = int(pageToken or 0)
        page = newest_first[start : start + maxResults]
        out: dict = {"messages": [{"id": m} for m in page]}
        if start + maxResults < len(newest_first):
            out["nextPageToken"] = str(start + maxResults)
        return _Call(lambda: out)

    def get(self, userId, id, format):
        self.g.calls.append(("messages.get", {"id": id, "format": format}))
        hid = next(h for h, m in self.g.events if m == id)
        if format == "raw":
            raw = base64.urlsafe_b64encode(self.g.raw[id]).decode()
            return _Call(lambda: {"id": id, "raw": raw})
        return _Call(lambda: gmail_full(id, self.g.raw[id], hid))


class _HttpError(Exception):
    class resp:  # mimics googleapiclient.errors.HttpError.resp
        status = 404


class _History:
    def __init__(self, g: FakeGmail) -> None:
        self.g = g

    def list(self, userId, startHistoryId, labelId, historyTypes, pageToken=None):
        self.g.calls.append(("history.list", {"start": startHistoryId}))

        def run():
            if self.g.history_expired:
                raise _HttpError("history too old")
            recs = [
                {"id": str(h), "messagesAdded": [{"message": {"id": m, "labelIds": [labelId]}}]}
                for h, m in self.g.events
                if h > int(startHistoryId)
            ]
            return {"history": recs, "historyId": str(self.g.history_id)}

        return _Call(run)


# ─── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_keyring_no_state(monkeypatch):
    reset_shared_state()
    monkeypatch.setattr(auth, "load_token", lambda: None)
    yield
    reset_shared_state()


@pytest.fixture(scope="module")
def rows():
    return load_registry()


@pytest.fixture
def src(rows):
    return next(r for r in rows if r.key == SOURCE_KEY)


@pytest.fixture
def conn(rows):
    c = db.connect(":memory:")
    db.migrate(c)
    sync_sources_table(c, rows)
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache")}, "fetch": {"max_retries": 0}}
    )


class Web:
    """The only web: JobLink's robots + one detail page. Every request is recorded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == JOBLINK_HOST:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=JOBLINK_ROBOTS)
            if request.url.path.startswith("/jobs/"):
                return httpx.Response(
                    200, text=DETAIL_HTML, headers={"content-type": "text/html; charset=utf-8"}
                )
        return httpx.Response(404, text="not here")

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}


@pytest.fixture
def web():
    return Web()


def ctx_for(src, settings, conn, web):
    t = {"v": 0.0}

    def sleep(s):
        t["v"] += s

    return FetchContext(
        src,
        settings,
        conn,
        transport=httpx.MockTransport(web.handle),
        clock=lambda: t["v"],
        sleep=sleep,
    )


def search(adapter, src, settings, conn, web):
    with ctx_for(src, settings, conn, web) as ctx:
        return list(adapter.search(src, Query(), NOW, ctx))


def adapter_for(gmail, rows, **kw):
    return MailAlertsAdapter(service=gmail, rows=rows, clock=lambda: NOW, **kw)


# ─── watermark ──────────────────────────────────────────────────────────────


def test_first_run_lists_label_then_history_watermark(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", VOS)
    adapter = adapter_for(gmail, rows)
    stubs = search(adapter, src, settings, conn, web)
    assert len(stubs) == 3
    assert [c for c, _ in gmail.calls].count("messages.list") == 1
    assert sync.stored_watermark(conn, LABEL) == str(gmail.history_id)

    # second run: only messages added since the watermark, via history.list
    gmail.add("m2", JOBLINK)
    gmail.calls.clear()
    stubs = search(adapter_for(gmail, rows), src, settings, conn, web)
    kinds = [c for c, _ in gmail.calls]
    assert "messages.list" not in kinds and "history.list" in kinds
    assert [k for k in kinds if k == "messages.get"] == ["messages.get"]
    assert len(stubs) == 3 and all("illinoisjoblink" in s.url for s in stubs)
    assert sync.stored_watermark(conn, LABEL) == str(gmail.history_id)

    # third run: nothing new, nothing fetched
    gmail.calls.clear()
    assert search(adapter_for(gmail, rows), src, settings, conn, web) == []
    assert "messages.get" not in [c for c, _ in gmail.calls]


def test_expired_history_falls_back_to_listing_and_skips_processed(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", VOS)
    search(adapter_for(gmail, rows), src, settings, conn, web)
    gmail.add("m2", NLX)
    gmail.history_expired = True
    gmail.calls.clear()
    stubs = search(adapter_for(gmail, rows), src, settings, conn, web)
    gets = [a["id"] for c, a in gmail.calls if c == "messages.get"]
    assert gets == ["m2"]  # m1 was already processed
    assert len(stubs) == 2


def test_limit_truncates_and_keeps_old_watermark(rows, src, settings, conn, web):
    gmail = FakeGmail()
    for i, rel in enumerate((VOS, JOBLINK, NLX)):
        gmail.add(f"m{i}", rel)
    search(adapter_for(gmail, rows, limit=2), src, settings, conn, web)
    assert sync.stored_watermark(conn, LABEL) is None  # one message still waits
    n = conn.execute("SELECT COUNT(*) FROM mail_message").fetchone()[0]
    assert n == 2
    search(adapter_for(gmail, rows, limit=2), src, settings, conn, web)
    assert conn.execute("SELECT COUNT(*) FROM mail_message").fetchone()[0] == 3
    assert sync.stored_watermark(conn, LABEL) == str(gmail.history_id)


def test_missing_label_is_a_clear_error(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.list = lambda userId: _Call(lambda: {"labels": []})  # type: ignore[method-assign]
    with pytest.raises(sync.MailSyncError, match="jobhunter mail setup"):
        search(adapter_for(gmail, rows), src, settings, conn, web)


# ─── stubs, family state, resolve policy ────────────────────────────────────


def test_vos_stubs_are_partial_unresolvable_and_carry_state(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", VOS)
    stubs = search(adapter_for(gmail, rows), src, settings, conn, web)
    for s in stubs:
        assert s.source_key == SOURCE_KEY
        assert s.description_completeness.value == "partial"
        assert s.needs_resolve is False
        assert s.locations and s.locations[0].state == "FL"
        assert s.extra["origin_source_key"] == "fl-employflorida"
    assert stubs[0].external_id == external_id_for(stubs[0].url)
    fam = conn.execute("SELECT family, origin_source_key FROM mail_message").fetchone()
    assert tuple(fam) == ("vos", "fl-employflorida")


def test_origin_state_used_when_location_has_none(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", PA)
    stubs = search(adapter_for(gmail, rows), src, settings, conn, web)
    pitt = next(s for s in stubs if s.title == "Systems Engineer")
    assert (pitt.locations[0].state, pitt.locations[0].city) == ("PA", "Pittsburgh")
    assert all(not s.needs_resolve for s in stubs)  # PA CareerLink is Disallow: /


def run_pipeline(conn, settings, src, gmail, rows, web, stages=("list", "resolve", "normalize")):
    adapter = adapter_for(gmail, rows)
    return runner.run_pipeline(
        conn,
        settings,
        [src],
        adapters={"mailalerts": lambda: adapter},
        ctx_factory=lambda s: ctx_for(s, settings, conn, web),
        profile_loader=lambda: None,
        stages=list(stages),
        now=NOW,
    )


def test_vos_entry_stays_partial_with_zero_requests_to_vos_host(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", VOS)
    gmail.add("m2", PA)
    report = run_pipeline(conn, settings, src, gmail, rows, web)
    assert report.exit_code == 0
    assert web.requests == []  # not even robots.txt
    jobs = conn.execute("SELECT * FROM job").fetchall()
    assert len(jobs) == 5
    assert {j["description_completeness"] for j in jobs} == {"partial"}
    assert all(j["stage"] == "normalized" for j in jobs)


def test_joblink_entry_resolved_where_robots_allows_detail(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", JOBLINK)
    report = run_pipeline(conn, settings, src, gmail, rows, web)
    assert report.results[SOURCE_KEY].resolved == 3
    paths = [r.url.path for r in web.requests]
    assert paths[0] == "/robots.txt" and paths.count("/robots.txt") == 1
    assert web.hosts() == {JOBLINK_HOST}
    assert not any(p.startswith("/search/") for p in paths)
    rows_ = conn.execute("SELECT * FROM job ORDER BY id").fetchall()
    assert {r["description_completeness"] for r in rows_} == {"full"}
    assert "Kubernetes in production" in rows_[0]["description_text"]


def test_robots_disallowed_detail_keeps_partial_without_fetching(rows, src, settings, conn, web):
    def handle(request):
        web.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /jobs/\n")
        return httpx.Response(200, text=DETAIL_HTML)

    web.handle = handle  # type: ignore[method-assign]
    gmail = FakeGmail()
    gmail.add("m1", JOBLINK)
    report = run_pipeline(conn, settings, src, gmail, rows, web)
    assert report.exit_code == 0
    assert [r.url.path for r in web.requests] == ["/robots.txt"]
    jobs = conn.execute("SELECT description_completeness, needs_resolve FROM job").fetchall()
    assert {tuple(j) for j in jobs} == {("partial", 0)}


def test_extract_description_prefers_jsonld():
    ld = {"@type": "JobPosting", "description": "<p>From JSON-LD</p>"}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>' + DETAIL_HTML
    assert extract_description(html) == "<p>From JSON-LD</p>"
    assert "container platform" in extract_description(DETAIL_HTML)
    assert extract_description("<p>tiny</p>") is None


# ─── dedupe first ───────────────────────────────────────────────────────────


def add_full_job(conn, *, url, title, employer, state, apply_url=None):
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, apply_url, title, employer, "
        "description_raw, description_text, description_completeness, stage, first_seen_at, "
        "last_seen_at) VALUES ('us-nlx', ?, ?, ?, ?, ?, '<p>full</p>', 'full', 'full', "
        "'grouped', 'x', 'x')",
        (url, url, apply_url, title, employer),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_locations (job_id, state, is_primary) VALUES (?, ?, 1)", (jid, state)
    )
    return jid


def test_dedupe_first_attaches_sighting_to_full_record(rows, src, settings, conn, web):
    by_url = add_full_job(
        conn,
        url="https://usnlx.com/olympia-wa/data-engineer/0123456789ABCDEF0123456789ABCDEF/job",
        title="Data Engineer",
        employer="Fictional Data Cooperative",
        state="WA",
    )
    by_name = add_full_job(
        conn,
        url="https://example.org/postings/abc",
        title="Senior Data Engineer",
        employer="Example Mountain Logistics, Inc.",
        state="ID",
    )
    gmail = FakeGmail()
    gmail.add("m1", NLX)
    before = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    stubs = search(adapter_for(gmail, rows), src, settings, conn, web)
    assert stubs == []  # nothing new: both entries were already held in full
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == before
    sightings = conn.execute(
        "SELECT matched_job_id, match_method FROM mail_entry WHERE outcome = 'sighting' "
        "ORDER BY idx"
    ).fetchall()
    assert [tuple(s) for s in sightings] == [
        (by_url, "apply_url"),
        (by_name, "title_employer_state"),
    ]
    assert conn.execute("SELECT last_seen_at FROM job WHERE id = ?", (by_url,)).fetchone()[0]


def test_partial_job_is_not_a_dedupe_target(rows, src, settings, conn, web):
    gmail = FakeGmail()
    gmail.add("m1", VOS)
    search(adapter_for(gmail, rows), src, settings, conn, web)
    gmail.add("m2", raw=(FIX / VOS).read_bytes())  # the same alert again
    stubs = search(adapter_for(gmail, rows), src, settings, conn, web)
    assert len(stubs) == 3  # re-yielded (the list stage upserts them in place), no sightings
    assert (
        conn.execute("SELECT COUNT(*) FROM mail_entry WHERE outcome='sighting'").fetchone()[0] == 0
    )


# ─── scoring: partial cap, paste, rescore ───────────────────────────────────

DIMS_A = {
    "skills": {"score": 95, "why": "x"},
    "seniority": {"score": 95, "why": "match: x"},
    "domain": {"score": 95, "why": "x"},
    "raw_skills": 95,
    "recency_weighted_skills": 95,
}


@pytest.fixture
def profile(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(
        "hard:\n  states_allowed: all\n  remote_ok: true\nsoft:\n  state_ranking: [FL]\n"
        "resume_path: resume.md\n",
        encoding="utf-8",
    )
    (d / "resume.md").write_text("Synthetic Person\n", encoding="utf-8")
    return load_profile(d)


def scored_partial_group(conn, profile) -> int:
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_raw, "
        "description_text, description_completeness, location_scope, stage, first_seen_at, "
        "last_seen_at) VALUES (?, 'm1', 'https://www.employflorida.com/vosnet/x', "
        "'Systems Administrator II', 'Gulfside Widget Cooperative', '<p>snippet</p>', "
        "'snippet', 'partial', 'single', 'scored', 'x', 'x')",
        (SOURCE_KEY,),
    ).lastrowid
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, method, created_at) "
        "VALUES (?, 'exact_hash', 'x')",
        (jid,),
    ).lastrowid
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (gid, jid))
    conn.execute(
        "INSERT INTO job_locations (job_id, state, city, is_primary) "
        "VALUES (?, 'FL', 'Tallahassee', 1)",
        (jid,),
    )
    conn.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[]', ?, 'x')",
        (jid, profile.filter_version),
    )
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, blockers, created_at) "
        "VALUES (?, 'screen', 'test:model', ?, ?, 'strong', 0, ?, '[]', '[]', 'x')",
        (gid, screen.PROMPT_VERSION, profile.scoring_version, json.dumps(DIMS_A)),
    )
    return gid


def fit_for(conn, profile, gid):
    job = detail.group_job(conn, gid)
    fit = conn.execute(
        "SELECT * FROM fit_score WHERE job_group_id = ? ORDER BY id DESC LIMIT 1", (gid,)
    ).fetchone()
    return buckets.compute_row(fit, job, [], profile)


def test_partial_capped_at_b(conn, profile):
    gid = scored_partial_group(conn, profile)
    fit = fit_for(conn, profile, gid)
    assert fit.partial and fit.bucket is Bucket.B
    conn.execute("UPDATE job SET description_completeness = 'full'")
    assert fit_for(conn, profile, gid).bucket is Bucket.A
    assert buckets.cap_partial(Bucket.C, True) is Bucket.C


def test_partial_posting_text_says_so(conn, profile):
    gid = scored_partial_group(conn, profile)
    job = dict(detail.group_job(conn, gid))
    req = screen.build_score_request(job, "Tallahassee, FL", profile)
    assert "description below is partial" in req.posting


def test_paste_description_makes_group_eligible_again(conn, profile):
    gid = scored_partial_group(conn, profile)
    assert screen.eligible_groups(conn, profile, scorer="test:model", limit=10) == []
    detail.paste_description(
        conn, gid, "Run Linux servers.\n\nRequires\nKubernetes & Terraform <3 years>", NOW
    )
    job = detail.group_job(conn, gid)
    assert job["description_completeness"] == "pasted"
    assert "Kubernetes & Terraform <3 years>" in job["description_text"]
    assert "&lt;3 years&gt;" in job["description_raw"]  # stored escaped
    eligible = screen.eligible_groups(conn, profile, scorer="test:model", limit=10)
    assert [g["group_id"] for g in eligible] == [gid] and eligible[0]["input_rev"] == 1
    assert fit_for(conn, profile, gid).bucket is Bucket.A  # the cap is lifted
    # nothing deleted: the partial-era score is still there
    assert conn.execute("SELECT COUNT(*) FROM fit_score").fetchone()[0] == 1

    class Scorer:
        name = "test:model"

        def submit(self, requests):
            from jobhunter.scoring.scorers import ScoreResult

            body = {
                "verdict": "strong",
                "dimensions": {
                    k: {"score": 90, "why": "match: ok"} for k in ("skills", "seniority", "domain")
                },
                "raw_skills": 90,
                "recency_weighted_skills": 90,
                "stale_skills": [],
                "current_focus_overlap": 50,
                "done_with_hits": [],
                "evidence": [{"claim": "linux", "quote": "Run Linux servers."}],
                "blockers": [],
                "missing_info": [],
                "shape_flags": [],
                "tailoring_hints": [],
            }
            return [
                ScoreResult(custom_id=r.custom_id, status="succeeded", text=json.dumps(body))
                for r in requests
            ]

        def cost(self, usage, batch):
            return 0.0

    out = screen.score_sync(conn, Scorer(), profile, limit=10, now=NOW)
    assert out.written == 1
    revs = [r[0] for r in conn.execute("SELECT input_rev FROM fit_score ORDER BY id")]
    assert revs == [0, 1]
    assert screen.eligible_groups(conn, profile, scorer="test:model", limit=10) == []


def test_paste_rejects_empty(conn, profile):
    gid = scored_partial_group(conn, profile)
    with pytest.raises(ValueError):
        detail.paste_description(conn, gid, "   ", NOW)


# ─── CLI ────────────────────────────────────────────────────────────────────

cli = CliRunner()


def _config(tmp_path, monkeypatch, extra: str = "") -> Path:
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[paths]\ndb_path = "{db_path}"\n{extra}')
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    return db_path


def test_sync_skips_cleanly_without_token(tmp_path, monkeypatch):
    db_path = _config(tmp_path, monkeypatch)
    result = cli.invoke(app, ["mail", "sync"])
    assert result.exit_code == 0
    assert "skipped: no Gmail token; run: jobhunter mail auth" in result.output
    assert not db_path.exists()


def test_runner_skips_mailalerts_without_token(rows, src, settings, conn):
    report = runner.run_pipeline(
        conn, settings, [src], profile_loader=lambda: None, stages=["list"], now=NOW
    )
    assert report.exit_code == 0
    assert any("us-mailalerts skipped (no Gmail token" in m for m in report.messages)


def test_mail_sync_command_runs_adapter(tmp_path, monkeypatch):
    db_path = _config(
        tmp_path, monkeypatch, f'[mail]\nclient_secrets_path = "{tmp_path}/cs.json"\n'
    )
    (tmp_path / "cs.json").write_text("{}")
    monkeypatch.setattr(auth, "load_token", lambda: "fake-token")
    gmail = FakeGmail()
    gmail.add("m1", VOS)
    monkeypatch.setattr(auth, "build_service", lambda settings: gmail)
    result = cli.invoke(app, ["mail", "sync", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "messages 1 (vos 1); entries 3: 3 new, 0 sightings" in result.output
    c = db.connect(db_path)
    try:
        assert c.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 3
    finally:
        c.close()


def test_sample_saves_scrubbed_fixtures(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, '[mail]\nalerts_address = "some.one+jobs@example.com"\n')
    raw = (
        (FIX / JOBLINK)
        .read_bytes()
        .replace(
            b"To: you+jobs@example.com",
            b"Delivered-To: some.one+jobs@example.com\n"
            b"Received: from mx for <some.one@example.com>\n"
            b"To: Some One <some.one+jobs@example.com>",
        )
        .replace(b"Manage alerts</a>", b"Manage alerts for some.one+jobs@example.com</a>")
    )
    gmail = FakeGmail()
    gmail.add("m1", raw=raw)
    gmail.add("m2", NLX)
    monkeypatch.setattr(auth, "build_service", lambda settings: gmail)
    out = tmp_path / "samples"
    result = cli.invoke(app, ["mail", "sample", "--save", str(out), "-n", "1"])
    assert result.exit_code == 0, result.output
    saved = sorted(out.glob("*.eml"))
    assert [p.name for p in saved] == ["m2.eml"]  # newest first, n=1
    result = cli.invoke(app, ["mail", "sample", "--save", str(out), "-n", "2"])
    data = (out / "m1.eml").read_bytes()
    assert b"some.one" not in data.lower()
    assert b"Delivered-To" not in data and b"Received" not in data
    assert b"you+jobs@example.com" in data
    # the scrubbed copy still parses like the original fixture
    from jobhunter.mail.message import load_eml
    from jobhunter.mail.parsers import parse_message

    det, found = parse_message(load_eml(out / "m1.eml"), load_registry())
    assert det.family == "joblink" and len(found) == 3
