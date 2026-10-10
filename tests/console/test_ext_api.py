"""The extension API, /ext/v1/ (specs/017 phase 1e): contract, pairing, capture, Score it.

FastAPI TestClient on 127.0.0.1:8808 (the Host the extension uses). No network: Fetch goes
to an injected fetcher, scoring to injected fake scorers or a fake batch client. Every fake
that should not be called fails the test when it is.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobhunter.apply import ext_pairing, paste
from jobhunter.apply.capture_models import MAX_BODY_BYTES, schema_json
from jobhunter.config import Console, Paths, Scoring, Settings
from jobhunter.console import ext_routes
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring.scorers import ScorerError, ScoreResult

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
SPEC = "test:model"
ORIGIN = ext_pairing.EXTENSION_ORIGIN
BASE = "http://127.0.0.1:8808"
LONG = " ".join(["Operate the synthetic Linux fleet and write Terraform modules."] * 6)
PAGE = "https://careers.acme.example/jobs/123"
GH = "https://boards.greenhouse.io/acme/jobs/4012345"
REPO = Path(__file__).resolve().parents[2]
PREFS = """\
resume_path: resume.md
hard:
  states_allowed: [CO]
  remote_ok: true
soft:
  state_ranking: [CO]
"""
DIMS = {k: {"score": 90, "why": "match: ok"} for k in ("skills", "seniority", "domain")}
BODY = json.dumps(
    {
        "verdict": "strong",
        "dimensions": DIMS,
        "raw_skills": 90,
        "recency_weighted_skills": 90,
        "stale_skills": [],
        "current_focus_overlap": 50,
        "done_with_hits": [],
        "evidence": [{"claim": "fleet", "quote": "Operate the synthetic Linux fleet"}],
        "blockers": [],
        "missing_info": [],
        "shape_flags": [],
        "tailoring_hints": [],
    }
)


def aid() -> str:
    return str(uuid.uuid4())


def posting(**kw):
    base = {
        "@type": "JobPosting",
        "title": "Platform Engineer",
        "hiringOrganization": {"name": "Acme Synthetic"},
        "jobLocation": {"address": {"addressLocality": "Denver", "addressRegion": "CO"}},
        "description": f"<p>{LONG}</p>",
        "url": PAGE,
    }
    base.update(kw)
    return base


def facts(*postings, url=PAGE, **kw):
    return {"url": url, "jsonld": [json.dumps(p) for p in postings], **kw}


class BlockingScorer:
    """Synchronous fake scorer that waits until the test releases it."""

    name = SPEC

    def __init__(self, fail: bool = False):
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self.fail = fail

    def submit(self, requests):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10), "the test never released the scorer"
        if self.fail:
            raise RuntimeError("scorer down")
        return [ScoreResult(custom_id=r.custom_id, status="succeeded", text=BODY) for r in requests]

    def cost(self, usage, batch):
        return 0.001


class FakeBatches:
    def __init__(self):
        self.created = []

    def create(self, *, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}", processing_status="in_progress")


class FakeClient:
    def __init__(self):
        self.messages = SimpleNamespace(batches=FakeBatches())


@pytest.fixture
def env(tmp_path):
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (pdir / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    db_path = tmp_path / "e.db"
    c = db.connect(db_path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    c.close()
    return SimpleNamespace(pdir=pdir, db_path=db_path, data=tmp_path / "data")


def make_client(env, allow_remote=False, **scoring) -> TestClient:
    scoring.setdefault("screen_scorer", SPEC)
    settings = Settings(
        paths=Paths(
            profile_dir=env.pdir,
            db_path=env.db_path,
            cache_dir=env.pdir.parent / "cache",
            data_dir=env.data,
        ),
        scoring=Scoring(**scoring),
        console=Console(port=8808),
    )
    app = create_app(
        settings, lambda: db.connect(env.db_path), clock=lambda: NOW, allow_remote=allow_remote
    )
    app.state.packet_scorer_factory = lambda spec: pytest.fail("no scorer was expected")
    app.state.packet_client_factory = lambda: pytest.fail("no batch client was expected")
    app.state.ext_fetcher = lambda url: pytest.fail("no fetch was expected")
    return TestClient(app, base_url=BASE, follow_redirects=False)


@pytest.fixture
def client(env):
    return make_client(env)


@pytest.fixture
def conn(env):
    c = db.connect(env.db_path)
    yield c
    c.close()


def pair(client, env) -> str:
    code, _ = ext_pairing.new_code(ext_pairing.state_path(env.data), NOW)
    r = client.post(
        "/ext/v1/pair", json={"code": code}, headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def token(client, env):
    return pair(client, env)


def hdrs(token, **extra):
    h = {"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0", "Authorization": f"Bearer {token}"}
    h.update(extra)
    return h


def post(client, token, path, body=None, **extra):
    t0 = time.monotonic()
    r = client.post(f"/ext/v1/{path}", json=body or {}, headers=hdrs(token, **extra))
    assert time.monotonic() - t0 < 20, f"{path} took longer than the 20 s deadline"
    return r


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


ROUTES = [
    "version",
    "capture",
    "capture/add",
    "capture/fetch",
    "link",
    "groups/1/describe",
    "groups/1/estimate",
    "groups/1/score",
    "groups/1/status",
    "groups/1/prepare",
]


# ─── contract ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [*ROUTES, "pair"])
def test_get_is_refused(client, token, path):
    r = client.get(f"/ext/v1/{path}", headers=hdrs(token))
    assert r.status_code == 405


@pytest.mark.parametrize("path", ROUTES)
def test_no_token_or_a_wrong_token_is_401(client, token, path):
    h = {"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"}
    assert client.post(f"/ext/v1/{path}", json={}, headers=h).status_code == 401
    assert post(client, "wrong-token", path).status_code == 401
    assert (
        client.post(f"/ext/v1/{path}", json={}, headers={**h, "Authorization": token}).status_code
        == 401
    )  # no Bearer scheme


@pytest.mark.parametrize("path", [*ROUTES, "pair"])
@pytest.mark.parametrize(
    "origin",
    [
        None,
        "https://careers.acme.example",
        "http://127.0.0.1:8808",
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
        "null",
    ],
)
def test_wrong_origin_is_403(client, token, path, origin):
    h = hdrs(token)
    if origin is None:
        h.pop("Origin")
    else:
        h["Origin"] = origin
    assert client.post(f"/ext/v1/{path}", json={}, headers=h).status_code == 403


@pytest.mark.parametrize("path", [*ROUTES, "pair"])
@pytest.mark.parametrize(
    "host", ["evil.example:8808", "127.0.0.1:9999", "127.0.0.1", "testserver", "0.0.0.0:8808"]
)
def test_rebinding_host_is_403(client, token, path, host):
    assert post(client, token, path, Host=host).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8808", "localhost:8808", "[::1]:8808"])
def test_loopback_hosts_on_the_port_are_allowed(client, token, host):
    assert post(client, token, "version", Host=host).status_code == 200


def test_allow_remote_still_refuses_a_non_loopback_host(env):
    c = make_client(env, allow_remote=True)
    t = pair(c, env)
    assert post(c, t, "version", Host="evil.example:8808").status_code == 403
    assert post(c, t, "version").status_code == 200


def test_preflight_is_answered_only_for_the_paired_origin(client):
    r = client.options(
        "/ext/v1/capture",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["access-control-allow-methods"] == "POST"
    assert "access-control-allow-private-network" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
    bad = client.options(
        "/ext/v1/capture",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert bad.status_code == 403 and "access-control-allow-origin" not in bad.headers


def test_other_console_routes_send_no_cors_headers(client):
    r = client.get("/captured", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("version", [None, "0.0.9", "abc"])
def test_old_or_missing_extension_version_is_426(client, token, version):
    h = hdrs(token)
    if version is None:
        h.pop("X-Jobhunter-Ext")
    else:
        h["X-Jobhunter-Ext"] = version
    r = client.post("/ext/v1/version", json={}, headers=h)
    assert (r.status_code == 426 and "Reload" in r.json()["error"]) or "reload" in r.json()["error"]


def test_body_over_1_mb_is_413(client, token):
    big = {"action_id": aid(), "facts": {"url": PAGE, "page_text": "x" * (MAX_BODY_BYTES + 10)}}
    r = post(client, token, "capture", big)
    assert r.status_code == 413


def test_oversized_and_unknown_fields_are_422(client, token):
    r = post(client, token, "capture", {"action_id": aid(), "facts": facts(), "extra": 1})
    assert r.status_code == 422
    r = post(client, token, "capture", {"action_id": "not-a-uuid", "facts": facts()})
    assert r.status_code == 422
    r = post(
        client,
        token,
        "capture",
        {"action_id": aid(), "facts": {"url": PAGE, "og_title": "x" * 1001}},
    )
    assert r.status_code == 422


def test_console_pages_cannot_use_the_api_without_the_token(client):
    # Same-origin requests from the console's own pages (or curl) are refused under /ext/.
    r = client.post("/ext/v1/version", json={}, headers={"Origin": BASE})
    assert r.status_code == 403
    r = client.post("/ext/v1/version", json={})
    assert r.status_code == 403


def test_token_never_appears_in_logs(client, env, caplog):
    t = pair(client, env)
    post(client, "wrong-" + t, "version")
    post(client, t, "version", Host="evil.example:8808")
    assert t not in caplog.text


def test_schema_file_matches_the_models():
    path = REPO / "extension" / "api" / "v1.schema.json"
    assert path.read_text(encoding="utf-8") == schema_json(), (
        "regenerate: uv run python -m jobhunter.apply.capture_models > extension/api/v1.schema.json"
    )


# ─── pairing ───────────────────────────────────────────────────────────────


def pair_post(client, code, origin=ORIGIN):
    return client.post(
        "/ext/v1/pair", json={"code": code}, headers={"Origin": origin, "X-Jobhunter-Ext": "0.1.0"}
    )


def test_pairing_code_is_single_use(client, env):
    code, _ = ext_pairing.new_code(ext_pairing.state_path(env.data), NOW)
    assert pair_post(client, code).status_code == 200
    assert pair_post(client, code).status_code == 403


def test_pairing_code_expires(env):
    path = ext_pairing.state_path(env.data)
    code, _ = ext_pairing.new_code(path, NOW)
    with pytest.raises(ext_pairing.PairError, match="expired"):
        ext_pairing.redeem(path, code, ext_pairing.EXTENSION_ID, NOW + timedelta(minutes=11))


def test_five_wrong_codes_void_the_pending_code(client, env):
    code, _ = ext_pairing.new_code(ext_pairing.state_path(env.data), NOW)
    for _ in range(5):
        assert pair_post(client, "AAAAA-AAAAA").status_code == 403
    assert pair_post(client, code).status_code == 403


def test_a_code_made_by_the_cli_is_accepted_by_the_console(client, env, monkeypatch):
    from typer.testing import CliRunner

    from jobhunter.cli import app as cli_app

    cfg = env.pdir.parent / "config.toml"
    cfg.write_text(
        f'[paths]\ndata_dir = "{env.data}"\ndb_path = "{env.db_path}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    out = CliRunner().invoke(cli_app, ["ext", "pair"])
    assert out.exit_code == 0, out.output
    code = out.output.split("Pairing code: ", 1)[1].split()[0]
    assert pair_post(client, code).status_code == 200
    assert (ext_pairing.state_path(env.data).stat().st_mode & 0o777) == 0o600


def test_another_extension_id_is_refused(env):
    path = ext_pairing.state_path(env.data)
    code, _ = ext_pairing.new_code(path, NOW)
    with pytest.raises(ext_pairing.PairError):
        ext_pairing.redeem(path, code, "abcdefghijklmnopabcdefghijklmnop", NOW)


def test_pairing_again_revokes_the_old_token_and_unpair_revokes(client, env, token):
    assert post(client, token, "version").status_code == 200
    second = pair(client, env)
    assert post(client, token, "version").status_code == 401
    assert post(client, second, "version").status_code == 200
    assert ext_pairing.unpair(ext_pairing.state_path(env.data))
    assert post(client, second, "version").status_code == 401


def test_extension_json_holds_hashes_only(client, env, token):
    text = ext_pairing.state_path(env.data).read_text(encoding="utf-8")
    assert token not in text and "token_sha256" in text


def test_pinned_id_comes_from_the_manifest_key():
    import base64
    import hashlib

    manifest = json.loads((REPO / "extension" / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == ext_pairing.EXTENSION_ID


def test_prefs_pair_button_shows_a_code_once(client, env):
    page = client.get("/prefs").text
    assert 'id="ext-pair"' in page
    r = client.post("/prefs/ext/pair", headers={"hx-request": "true"})
    assert r.status_code == 200 and 'id="ext-pair-code"' in r.text
    code = r.text.split('id="ext-pair-code">', 1)[1].split("<", 1)[0]
    assert pair_post(client, code).status_code == 200
    cross = client.post("/prefs/ext/pair", headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403


# ─── version ───────────────────────────────────────────────────────────────


def test_version_returns_tracking_params_and_host_lists(env):
    c = make_client(env)
    c.app.state.settings.capture.disabled_hosts.append("blocked.example")
    t = pair(c, env)
    v = post(c, t, "version").json()
    assert v["api"] == "v1" and v["min_extension"] == ext_routes.MIN_EXTENSION
    assert "trk" in v["tracking_params"] and v["tracking_prefixes"] == ["utm_"]
    assert v["disabled_hosts"] == ["blocked.example"]
    assert len(v["notice_hosts"]) == 2


# ─── capture through the routes ────────────────────────────────────────────


def test_capture_added_then_existing_and_never_scored(client, token, conn):
    r = post(client, token, "capture", {"action_id": aid(), "facts": facts(posting())})
    assert r.status_code == 200 and r.json()["outcome"] == "added"
    gid = r.json()["group"]["group_id"]
    again = post(client, token, "capture", {"action_id": aid(), "facts": facts(posting())})
    assert again.json()["outcome"] == "existing" and again.json()["group"]["group_id"] == gid
    for table in ("fit_score", "prefilter_result", "llm_spend"):
        assert count(conn, table) == 0


def test_preview_then_add_through_the_routes(client, token, conn):
    f = {"url": PAGE, "og_title": "SRE at Acme", "page_text": "Hi, Pat\n" + LONG}
    r = post(client, token, "capture", {"action_id": aid(), "facts": f}).json()
    assert r["outcome"] == "previewed" and count(conn, "job") == 0
    body = {
        "action_id": aid(),
        "facts": f,
        "title": "SRE",
        "employer": "Acme",
        "description": LONG,
        "source": "edited",
    }
    added = post(client, token, "capture/add", body)
    assert added.json()["outcome"] == "added"
    again = post(client, token, "capture/add", body)  # resent after a lost answer
    assert again.json()["replayed"] and count(conn, "job") == 1


def test_describe_route_and_gone_groups(client, token, conn):
    from jobhunter.console.proposals import _manual_group

    gid = _manual_group(conn, "msg-1", "Acme Synthetic", "Platform Engineer", ISO)
    f = facts(posting())
    r = post(client, token, "capture", {"action_id": aid(), "facts": f}).json()
    assert r["outcome"] == "possible" and r["describe"] == [gid]
    out = post(client, token, f"groups/{gid}/describe", {"action_id": aid(), "facts": f})
    assert out.status_code == 200 and out.json()["group"]["has_description"]
    missing = post(client, token, "groups/999/status", {"job_id": None})
    assert missing.status_code == 404 and missing.json() == {"gone": True, "current_group": None}


def test_group_routes_follow_a_merged_group(client, token, conn):
    a = post(
        client, token, "capture", {"action_id": aid(), "facts": facts(posting(title="Alpha role"))}
    )
    b = post(
        client,
        token,
        "capture",
        {"action_id": aid(), "facts": facts(posting(url=GH, title="Beta role"), url=GH)},
    )
    ga, gb = a.json()["group"]["group_id"], b.json()["group"]["group_id"]
    ja = a.json()["group"]["job_id"]
    linked = post(client, token, "link", {"action_id": aid(), "a": ga, "b": gb})
    assert linked.json()["outcome"] == "linked_groups"
    kept = linked.json()["group_id"]
    gone = ga if kept == gb else gb
    for path in ("status", "estimate", "prepare"):
        r = post(client, token, f"groups/{gone}/{path}", {"job_id": ja})
        assert r.status_code == 404 and r.json()["current_group"] == kept


def test_link_route_refuses_while_scoring(client, token, conn):
    a = post(
        client, token, "capture", {"action_id": aid(), "facts": facts(posting(title="Alpha role"))}
    )
    b = post(
        client,
        token,
        "capture",
        {"action_id": aid(), "facts": facts(posting(url=GH, title="Beta role"), url=GH)},
    )
    ga, gb = a.json()["group"]["group_id"], b.json()["group"]["group_id"]
    scorer = BlockingScorer()
    client.app.state.packet_scorer_factory = lambda spec: scorer
    est = post(client, token, f"groups/{ga}/estimate").json()
    assert (
        post(
            client, token, f"groups/{ga}/score", {"action_id": aid(), "token": est["token"]}
        ).status_code
        == 202
    )
    assert scorer.started.wait(5)
    r = post(client, token, "link", {"action_id": aid(), "a": ga, "b": gb})
    assert r.status_code == 409 and "scoring in progress" in r.json()["error"]
    assert count(conn, "job_group") == 2
    scorer.release.set()
    for t in client.app.state.ext_score_threads:
        t.join(10)
    ok = post(client, token, "link", {"action_id": aid(), "a": ga, "b": gb})
    assert ok.status_code == 200
    kept = ok.json()["group_id"]
    assert kept == ga  # the scored one
    assert conn.execute("SELECT job_group_id FROM fit_score").fetchone()[0] == ga


def test_fetch_refuses_non_ats_urls_without_fetching(client, token, conn):
    for url in ("https://careers.acme.example/jobs/1", "http://boards.greenhouse.io/a/jobs/1"):
        r = post(client, token, "capture/fetch", {"action_id": aid(), "url": url})
        assert r.status_code == 422
    assert count(conn, "capture_log") == 0


def test_fetch_robots_refusal_shows_the_paste_message(client, token):
    def refuse(url):
        raise paste.PasteError("boards.greenhouse.io's robots.txt does not allow fetching")

    client.app.state.ext_fetcher = refuse
    r = post(client, token, "capture/fetch", {"action_id": aid(), "url": GH})
    assert r.status_code == 422 and "robots.txt" in r.json()["error"]


def test_fetch_gives_up_after_the_timeout(client, token, monkeypatch):
    monkeypatch.setattr(ext_routes, "FETCH_TIMEOUT_S", 0.2)
    gate = threading.Event()
    client.app.state.ext_fetcher = lambda url: gate.wait(5) and paste.Fetched(LONG)
    r = post(client, token, "capture/fetch", {"action_id": aid(), "url": GH})
    gate.set()
    assert r.status_code == 504 and "select the text" in r.json()["error"]


def test_fetch_returns_text_for_the_preview(client, token):
    client.app.state.ext_fetcher = lambda url: paste.Fetched(LONG, "Platform Engineer", "Acme")
    r = post(client, token, "capture/fetch", {"action_id": aid(), "url": GH})
    assert r.status_code == 200 and r.json()["text"] == LONG


def test_prepare_route_opens_a_packet(client, token, conn):
    gid = post(client, token, "capture", {"action_id": aid(), "facts": facts(posting())}).json()[
        "group"
    ]["group_id"]
    r = post(client, token, f"groups/{gid}/prepare")
    assert r.status_code == 200 and r.json()["path"].startswith("/packet/")
    assert not r.json()["needs_text"]
    assert count(conn, "fit_score") == 0


# ─── Score it ──────────────────────────────────────────────────────────────


@pytest.fixture
def captured(client, token):
    r = post(client, token, "capture", {"action_id": aid(), "facts": facts(posting())})
    return r.json()["group"]["group_id"]


def test_estimate_has_the_notice_and_the_prefilter_line(client, token, captured):
    est = post(client, token, f"groups/{captured}/estimate").json()
    assert est["refusal"] is None and est["token"]
    assert est["notice"] and "sends your resume-derived profile" in est["notice"]
    assert est["prefilter_line"] == ext_routes.PREFILTER_LINE
    assert est["confirm_label"].startswith("Confirm, spend")


def test_a_scorer_error_in_the_notice_is_a_refusal(client, token, captured, monkeypatch):
    def broken(spec, scoring):
        raise ScorerError("no key for that scorer")

    monkeypatch.setattr(ext_routes, "privacy_notice", broken)
    est = post(client, token, f"groups/{captured}/estimate").json()
    assert est["refusal"] == "no key for that scorer"
    r = post(client, token, f"groups/{captured}/score", {"action_id": aid(), "token": est["token"]})
    assert r.status_code == 409


def test_stale_token_is_409_before_anything_is_written(client, token, captured, conn):
    r = post(client, token, f"groups/{captured}/score", {"action_id": aid(), "token": "stale"})
    assert r.status_code == 409 and r.json()["estimate"]["token"]
    assert count(conn, "prefilter_result") == 0 and count(conn, "fit_score") == 0


def test_score_returns_202_and_status_moves_to_the_bucket(client, token, captured, conn):
    scorer = BlockingScorer()
    client.app.state.packet_scorer_factory = lambda spec: scorer
    est = post(client, token, f"groups/{captured}/estimate").json()
    action = aid()
    r = post(
        client, token, f"groups/{captured}/score", {"action_id": action, "token": est["token"]}
    )
    assert r.status_code == 202 and r.json()["status"] == "in_progress"
    assert scorer.started.wait(5)
    assert post(client, token, f"groups/{captured}/status").json()["state"] == "in_progress"
    # a second confirm while the claim is held: 409, nothing spent
    second = post(
        client, token, f"groups/{captured}/score", {"action_id": aid(), "token": est["token"]}
    )
    assert second.status_code == 409
    # the same action resent after a lost answer: the first answer
    assert (
        post(
            client, token, f"groups/{captured}/score", {"action_id": action, "token": est["token"]}
        ).status_code
        == 202
    )
    scorer.release.set()
    for t in client.app.state.ext_score_threads:
        t.join(10)
    st = post(client, token, f"groups/{captured}/status").json()
    assert st["state"] == "scored" and st["bucket"] and st["verdict"]
    assert scorer.calls == 1 and count(conn, "fit_score") == 1


def test_a_scorer_failure_shows_in_status_and_leaves_nothing_waiting(client, token, captured, conn):
    scorer = BlockingScorer(fail=True)
    scorer.release.set()
    client.app.state.packet_scorer_factory = lambda spec: scorer
    est = post(client, token, f"groups/{captured}/estimate").json()
    assert (
        post(
            client, token, f"groups/{captured}/score", {"action_id": aid(), "token": est["token"]}
        ).status_code
        == 202
    )
    for t in client.app.state.ext_score_threads:
        t.join(10)
    st = post(client, token, f"groups/{captured}/status").json()
    assert st["state"] == "error" and "nothing is left waiting" in st["message"]
    assert count(conn, "prefilter_result") == 0
    stage = conn.execute("SELECT stage FROM job").fetchone()[0]
    assert stage == "normalized"


def test_an_old_claim_with_no_score_shows_stopped(client, token, captured, conn):
    job = conn.execute("SELECT canonical_job_id FROM job_group WHERE id = ?", (captured,))
    conn.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[\"user-requested\"]', 'v', ?)",
        (job.fetchone()[0], (NOW - timedelta(minutes=20)).isoformat()),
    )
    st = post(client, token, f"groups/{captured}/status").json()
    assert st["state"] == "stopped" and "retry" in st["message"]


def test_the_batch_path_shows_submitted(env, conn):
    c = make_client(env, screen_scorer="anthropic:claude-haiku-4-5")
    fake = FakeClient()
    c.app.state.packet_client_factory = lambda: fake
    t = pair(c, env)
    gid = post(c, t, "capture", {"action_id": aid(), "facts": facts(posting())}).json()["group"][
        "group_id"
    ]
    est = post(c, t, f"groups/{gid}/estimate").json()
    assert est["batch"] and "when the batch is collected" in est["confirm_label"]
    assert (
        post(c, t, f"groups/{gid}/score", {"action_id": aid(), "token": est["token"]}).status_code
        == 202
    )
    for th in c.app.state.ext_score_threads:
        th.join(10)
    st = post(c, t, f"groups/{gid}/status").json()
    assert st["state"] == "submitted" and st["batch_id"] == "msgbatch_1"
    assert len(fake.messages.batches.created) == 1


def test_not_scored_status(client, token, captured):
    assert post(client, token, f"groups/{captured}/status").json()["state"] == "not_scored"


# ─── console pages ─────────────────────────────────────────────────────────


def test_captured_page_lists_captures_newest_first(client, token):
    post(client, token, "capture", {"action_id": aid(), "facts": facts(posting())})
    page = client.get("/captured").text
    assert "Platform Engineer" in page and "careers.acme.example" in page and "not scored" in page
    assert 'href="/captured"' in client.get("/inbox").text


def test_job_page_offers_score_now_and_scores_on_confirm(client, token, captured, conn):
    page = client.get(f"/job/{captured}").text
    assert 'id="score-now"' in page and "It skips your prefilter rules" in page
    tok = page.split('name="token" value="', 1)[1].split('"', 1)[0]
    scorer = BlockingScorer()
    scorer.release.set()
    client.app.state.packet_scorer_factory = lambda spec: scorer
    r = client.post(f"/job/{captured}/score", data={"token": tok}, headers={"hx-request": "true"})
    assert r.status_code == 200 and 'id="score-result"' in r.text
    assert scorer.calls == 1 and count(conn, "fit_score") == 1


def test_job_page_score_needs_the_confirmed_estimate(client, token, captured, conn):
    r = client.post(
        f"/job/{captured}/score", data={"token": "stale"}, headers={"hx-request": "true"}
    )
    assert "estimate changed" in r.text and count(conn, "prefilter_result") == 0


def test_ingested_job_page_has_no_score_button(client, conn):
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "stage, first_seen_at, last_seen_at) VALUES (50, 'co', '50', 'https://example.com/50', "
        "'Ingested', 'Acme', 'text', 'grouped', ?, ?)",
        (ISO, ISO),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (50, 50, 'exact_hash', ?)",
        (ISO,),
    )
    conn.execute("UPDATE job SET job_group_id = 50 WHERE id = 50")
    assert 'id="score-now"' not in client.get("/job/50").text
    assert client.post("/job/50/score", data={"token": "x"}).status_code == 409


def test_job_page_link_them_for_a_same_job_candidate(client, token, conn):
    li = "https://www.linkedin.com/jobs/view/4012345678/"
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "stage, first_seen_at, last_seen_at) VALUES (60, 'co', '60', ?, 'Sr Platform Engineer', "
        "'Acme Synthetic', 'alert text', 'grouped', ?, ?)",
        (li, ISO, ISO),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (60, 60, 'exact_hash', ?)",
        (ISO,),
    )
    conn.execute("UPDATE job SET job_group_id = 60 WHERE id = 60")
    gid = post(
        client, token, "capture", {"action_id": aid(), "facts": facts(posting(url=GH), url=GH)}
    ).json()["group"]["group_id"]
    # the board page says its Apply goes to GH: recorded on the alert group's job
    conn.execute(
        "INSERT INTO job_board_ref (board, board_id, job_id, apply_mode, apply_url, seen_at) "
        "VALUES ('linkedin', '4012345678', 60, 'offsite', ?, ?)",
        (GH, ISO),
    )
    page = client.get(f"/job/{gid}").text
    assert 'id="link-60"' in page
    r = client.post(f"/job/{gid}/link", data={"other": "60"})
    assert r.status_code == 303 and r.headers["location"] == "/job/60"  # the ingested group
    assert count(conn, "job_group") == 1
    assert client.post("/job/60/link", data={"other": "999"}).status_code == 409


def test_apply_new_prefills_the_url_only(client, conn):
    r = client.get("/apply/new", params={"url": GH})
    assert r.status_code == 200 and f'value="{GH}"' in r.text and 'value="Acme"' in r.text
    js = client.get("/apply/new", params={"url": "javascript:alert(1)"}).text
    assert "javascript:" not in js
    assert count(conn, "job") == 0
