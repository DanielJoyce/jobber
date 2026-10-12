"""Drafting in the background for the extension (specs/022 A7): estimate, generate (202) and
draft-status on ``/ext/v1/packets/{id}/``.

No test launches ``claude`` or calls the API: the CLI runner is a fake object installed on
``app.state.packet_runner`` and the API client a fake on ``app.state.packet_api_client_factory``.
Fakes that must not be called fail the test when they are, so "no spend without a confirm" is
checked on every path. Every test joins the draft threads it started (tests/conftest.py fails
a test that leaves one running).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobhunter.apply import cli_runner, draft_runs, ext_pairing, generator, inflight, runner_state
from jobhunter.config import Apply, Console, Paths, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

NOW = datetime(2026, 10, 12, 14, 2, tzinfo=UTC)
ORIGIN = ext_pairing.EXTENSION_ORIGIN
BASE = "http://127.0.0.1:8808"
RESUME = """\
Synthetic Person
<you>@example.com | Denver, CO
Experience
Platform Engineer, Acme Synthetic Corp
2019 - 2023
- Contributed to the migration of 40 services to Kubernetes
- Wrote Terraform modules used by 3 teams
Skills: Python, Terraform, Kubernetes
"""
QUOTE = "build our Kubernetes platform"
POSTING = f"Synthetic Widgets is hiring.\n\nYou will {QUOTE} and own the deploy pipeline."
RESUME_OUT = {
    "resume": {
        "header": ["L1", "L2"],
        "summary": {"text": "Platform engineer who moves services to Kubernetes.",
                    "sources": ["L4", "L6"]},
        "sections": [{"heading": "Experience", "entries": [{
            "source_line": "L4", "employer": "Acme Synthetic Corp", "title": "Platform Engineer",
            "dates": "2019 - 2023",
            "bullets": [{"text": "Contributed to moving 40 services onto Kubernetes",
                         "sources": ["L6"]}],
        }]}],
        "skills": [{"name": "Terraform", "sources": ["L8"]}],
        "omitted": [],
        "change_notes": [],
    }
}  # fmt: skip
ENTAIL = {"lines": [{"id": "s0.e0.b0", "verdict": "partly"}]}
DRAFT_OUT = {
    "draft": {
        "sentences": [
            {"text": "I moved 40 services to Kubernetes.", "sources": ["L6"],
             "posting_quotes": []}
        ]
    }
}  # fmt: skip
QUESTION = "Why do you want to work here?"


class FakeRunner:
    """Stands in for ``cli_runner`` (``app.state.packet_runner``). ``outputs`` are the
    structured outputs in call order; ``gate`` (when set) holds each run until released."""

    def __init__(self, outputs=(), *, gate: bool = False, fail: str | None = None):
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not gate:
            self.release.set()
        self.fail = fail
        self.installed = True

    def find_binary(self):
        return "/fake/claude" if self.installed else None

    def work_dir(self, cache_dir):
        return cli_runner.work_dir(cache_dir)

    def check_auth(self, binary, cwd, environ):
        return None

    def run(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        assert self.release.wait(10), "the test never released the fake runner"
        if self.fail:
            raise cli_runner.CliFailure(self.fail, started=False)
        return cli_runner.CliResult(
            structured=self.outputs.pop(0),
            model="claude-opus-5-5",
            input_tokens=8000,
            output_tokens=2000,
            total_cost_usd=0.11,
        )


class FakeClient:
    """The Anthropic SDK client: records every call (a call is a spend)."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.params: list[dict] = []
        self.messages = self

    def stream(self, **params):
        self.params.append(params)
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.outputs.pop(0)))],
            usage=SimpleNamespace(input_tokens=8000, output_tokens=3000,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
            model=params["model"],
            stop_reason="end_turn",
        )  # fmt: skip

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: msg)

            def __exit__(self_inner, *a):
                return False

        return Ctx()


def no_api():
    pytest.fail("no API call without the confirmed click")


def build_app(tmp_path, **apply):
    pdir = tmp_path / "profile"
    pdir.mkdir(exist_ok=True)
    (pdir / "preferences.yaml").write_text("resume_path: resume.md\n", encoding="utf-8")
    (pdir / "resume.md").write_text(RESUME, encoding="utf-8")
    db_path = tmp_path / "t.db"
    if not db_path.exists():
        c = db.connect(db_path)
        db.migrate(c)
        c.close()
    settings = Settings(
        paths=Paths(profile_dir=pdir, db_path=db_path, data_dir=tmp_path / "data",
                    cache_dir=tmp_path / "cache"),
        console=Console(port=8808),
        apply=Apply(**apply),
    )  # fmt: skip
    app = create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW)
    app.state.packet_api_client_factory = no_api
    app.state.packet_runner = FakeRunner()
    return app, settings, db_path


@pytest.fixture
def env(tmp_path):
    app, settings, db_path = build_app(tmp_path)
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    r = client.post(
        "/apply/new",
        data={"text": POSTING, "employer": "Synthetic Widgets Inc", "title": "Platform Engineer"},
    )
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    code, _ = ext_pairing.new_code(ext_pairing.state_path(settings.paths.data_dir), NOW)
    r = client.post(
        "/ext/v1/pair", json={"code": code}, headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"}
    )
    token = r.json()["token"]
    conn = db.connect(db_path)
    ns = SimpleNamespace(
        client=client, app=app, pid=pid, conn=conn, settings=settings, token=token,
        tmp_path=tmp_path,
    )  # fmt: skip
    yield ns
    for t in list(app.state.ext_draft_threads):
        app.state.packet_runner.release.set()
        t.join(10)
    conn.close()


def post(env, path, body=None, client=None):
    headers = {"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0", "Authorization": f"Bearer {env.token}"}
    t0 = time.monotonic()
    r = (client or env.client).post(f"/ext/v1/{path}", json=body or {}, headers=headers)
    assert time.monotonic() - t0 < 20, f"{path} took longer than the 20 s deadline"
    return r


def estimate(env, kind="resume", **extra):
    r = post(env, f"packets/{env.pid}/estimate", {"kind": kind, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def generate(env, est, kind="resume", **extra):
    body = {"action_id": str(uuid.uuid4()), "kind": kind, "token": est["token"], **extra}
    return post(env, f"packets/{env.pid}/generate", body)


def status(env, kind="resume", **extra):
    r = post(env, f"packets/{env.pid}/draft-status", {"kind": kind, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def join(env):
    for t in list(env.app.state.ext_draft_threads):
        t.join(10)
        assert not t.is_alive()


def rows(env, table, where=""):
    return env.conn.execute(f"SELECT * FROM {table} {where}").fetchall()


def use_base_resume(env):
    assert env.client.post(f"/packet/{env.pid}/base").status_code == 303


# ─── the subscription runner: 202, drafting, done ──────────────────────────


def test_estimate_names_the_subscription_runner_at_no_cost(env):
    est = estimate(env)
    assert est["runner"] == "cli" and est["model"] == "opus"
    assert est["line"].startswith("Drafted on your Claude subscription with `claude -p` (opus)")
    assert "$0 on your subscription" in est["line"] and "at API prices" in est["line"]
    assert est["charged"] is False and est["confirm_label"] == "Generate"
    assert est["refusal"] is None and est["token"]
    assert env.app.state.packet_runner.calls == []  # an estimate runs nothing


def test_generate_answers_202_then_drafting_then_done(env):
    runner = FakeRunner([RESUME_OUT, ENTAIL], gate=True)
    env.app.state.packet_runner = runner
    est = estimate(env)
    r = generate(env, est)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "drafting" and body["kind"] == "resume"
    assert runner.started.wait(5)
    # While it runs: registered, claimed, stored as drafting, shown on the packet page.
    assert env.app.state.work.labels() == [f"an extension draft (packet {env.pid})"]
    key = generator.claim_key(env.pid, "resume", "")
    assert inflight.held(env.settings.paths.data_dir, key)
    s = status(env)
    assert s["state"] == "drafting" and s["message"] == "Drafting on your subscription"
    assert s["started_at"].startswith("2026-10-12T14:02")
    page = env.client.get(f"/packet/{env.pid}").text
    assert f'id="draft-running-{body["run_id"]}"' in page and "14:02 UTC" in page
    # A second Generate for the same document is refused while the thread holds the claim.
    again = generate(env, estimate(env))
    assert again.status_code == 409 and "already running" in again.json()["error"]
    runner.release.set()
    join(env)
    assert env.app.state.work.labels() == []
    assert not inflight.held(env.settings.paths.data_dir, key)
    s = status(env)
    assert s["state"] == "done" and s["version"] == 1 and s["runner"] == "cli"
    assert s["check_ok"] is True and s["unsupported"] == 0
    assert s["entailment"] == "done" and s["entail_partly"] == 1 and s["entail_no"] == 0
    assert [d["runner"] for d in rows(env, "packet_document")] == ["cli"]
    assert len(runner.calls) == 2  # the draft and its fact check, nothing else
    assert "draft-running" not in env.client.get(f"/packet/{env.pid}").text


def test_same_action_id_answers_the_same_and_runs_once(env):
    env.app.state.packet_runner = runner = FakeRunner([RESUME_OUT, ENTAIL])
    est = estimate(env)
    body = {"action_id": str(uuid.uuid4()), "kind": "resume", "token": est["token"]}
    first = post(env, f"packets/{env.pid}/generate", body)
    join(env)
    second = post(env, f"packets/{env.pid}/generate", body)
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    assert len(runner.calls) == 2 and len(rows(env, "draft_run")) == 1


def test_stale_token_is_409_with_the_new_estimate_and_nothing_written(env):
    env.app.state.packet_runner = runner = FakeRunner()
    r = generate(env, {"token": "stale"})
    assert r.status_code == 409
    assert "estimate changed" in r.json()["error"] and r.json()["estimate"]["token"]
    assert runner.calls == [] and rows(env, "draft_run") == []
    assert env.app.state.work.labels() == []


def test_a_cli_failure_ends_failed_offers_the_api_and_never_calls_it(env):
    env.app.state.packet_runner = FakeRunner(fail="Error: rate limited")
    r = generate(env, estimate(env))
    assert r.status_code == 202
    join(env)
    s = status(env)
    assert s["state"] == "failed" and "rate limited" in s["message"]
    assert s["offer_api"] is True and s["api_estimated_usd"] > 0  # no_api() was never called
    assert rows(env, "packet_document") == []


# ─── spend: never without a confirm ────────────────────────────────────────


def test_api_runner_needs_the_confirm_and_spends_only_after_it(env):
    est = estimate(env, runner="api")
    assert est["charged"] is True and est["model"] == "claude-opus-5-5"
    assert est["line"].startswith("Drafted on the Anthropic API (claude-opus-5-5), ≈ $")
    assert "apply cap $1.00 left today" in est["line"]
    assert est["confirm_label"].startswith("Generate, spend ≈ $")
    r = generate(env, est, runner="api")  # no confirm_paid: refused before anything runs
    assert r.status_code == 409 and "confirm the spend" in r.json()["error"]
    assert rows(env, "draft_run") == [] and rows(env, "llm_spend") == []
    client = FakeClient([RESUME_OUT, ENTAIL])
    env.app.state.packet_api_client_factory = lambda: client
    r = generate(env, est, runner="api", confirm_paid=True)
    assert r.status_code == 202
    join(env)
    assert status(env)["state"] == "done"
    assert client.params[0]["model"] == "claude-opus-5-5"
    assert env.app.state.packet_runner.calls == []  # the CLI was never touched


def test_subscription_on_paid_extra_usage_needs_the_confirm(env):
    runner_state.set_overage(env.settings.paths.data_dir, NOW)
    env.app.state.packet_runner = runner = FakeRunner([RESUME_OUT, ENTAIL])
    est = estimate(env)
    assert est["charged"] is True and "paid extra usage" in est["line"]
    assert est["confirm_label"].startswith("Generate, spend ≈ $")
    r = generate(env, est)
    assert r.status_code == 409 and runner.calls == [] and rows(env, "draft_run") == []
    r = generate(env, est, confirm_paid=True)
    assert r.status_code == 202
    join(env)
    assert status(env)["state"] == "done" and len(runner.calls) >= 1


def test_over_the_apply_cap_offers_no_generate(tmp_path):
    app, settings, _db = build_app(tmp_path, daily_cap_usd=0.0)
    env = SimpleNamespace(app=app, client=TestClient(app, base_url=BASE, follow_redirects=False))
    r = env.client.post("/apply/new", data={"text": POSTING, "employer": "E", "title": "T"})
    env.pid = int(r.headers["location"].rsplit("/", 1)[1])
    code, _ = ext_pairing.new_code(ext_pairing.state_path(settings.paths.data_dir), NOW)
    env.token = env.client.post(
        "/ext/v1/pair", json={"code": code}, headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"}
    ).json()["token"]
    est = estimate(env, runner="api")
    assert est["refusal"] == "Over today's apply cap ($0.00)" and est["confirm_label"] is None
    r = generate(env, est, runner="api", confirm_paid=True)
    assert r.status_code == 409 and app.state.ext_draft_threads == []


def test_model_fallback_notice_rides_with_the_line(env, monkeypatch):
    from jobhunter.config import Settings as S

    monkeypatch.setattr(S, "models_notices", property(lambda self: ["drafting model invalid"]))
    assert estimate(env)["notices"] == ["drafting model invalid"]


def test_no_runner_available_gives_the_no_access_message_and_help(env):
    env.app.state.packet_runner.installed = False
    est = estimate(env)
    assert est["confirm_label"] is None and est["help"] == "/help/drafting"
    assert est["refusal"] == "the claude command is not installed. Drafting cannot run."
    assert env.client.get(est["help"]).status_code == 200


def test_container_refuses_with_the_host_command(env, monkeypatch):
    from jobhunter import container

    monkeypatch.setattr(container, "is_container", lambda: True)
    est = estimate(env)
    assert est["refusal"] == f"Drafting runs on the host: `jobhunter apply draft {env.pid}`"
    r = generate(env, est)
    assert r.status_code == 409 and "on the host" in r.json()["error"]
    assert rows(env, "draft_run") == [] and env.app.state.packet_runner.calls == []


# ─── restart: 503, and stopped ─────────────────────────────────────────────


def test_generate_is_503_while_restarting_with_nothing_claimed_or_written(env):
    env.app.state.packet_runner = runner = FakeRunner()
    est = estimate(env)
    assert env.app.state.work.begin_restart() == []
    r = generate(env, est)
    assert r.status_code == 503 and r.json()["error"].startswith("jobhunter is restarting")
    assert r.headers["X-Jobhunter-Restarting"] == "1"
    assert rows(env, "draft_run") == [] and runner.calls == []
    assert env.app.state.ext_draft_threads == [] and env.app.state.work.labels() == []
    key = generator.claim_key(env.pid, "resume", "")
    assert not inflight.held(env.settings.paths.data_dir, key)


def test_restart_waits_for_a_running_draft(env):
    env.app.state.packet_runner = runner = FakeRunner([RESUME_OUT, ENTAIL], gate=True)
    assert generate(env, estimate(env)).status_code == 202
    assert runner.started.wait(5)
    assert env.app.state.work.begin_restart() == [f"an extension draft (packet {env.pid})"]
    runner.release.set()
    join(env)
    assert env.app.state.work.begin_restart() == []


def test_a_draft_cut_off_by_a_restart_reads_as_stopped(env):
    # What a restart leaves: a draft_run row with no outcome and nobody holding its claim.
    from jobhunter.console.inbox_routes import load_console_profile

    profile, _ = load_console_profile(env.settings)
    started = generator.start(
        env.conn, env.settings, profile, env.pid, "resume", runner="cli", now=NOW,
        cli=FakeRunner(),
    )  # fmt: skip
    assert status(env)["state"] == "drafting"  # the claim is held (by "the old process")
    started.claim.release()  # the process ended: the kernel drops its lock
    # A fresh console (the restarted process) reads it as stopped.
    app2, _s, _db = build_app(env.tmp_path)
    c2 = TestClient(app2, base_url=BASE, follow_redirects=False)
    s = status_with(env, c2)
    assert s["state"] == "stopped" and s["run_id"] == started.run_id
    assert s["message"] == (
        "The draft did not finish (the console restarted); nothing was charged on the subscription"
    )
    page = c2.get(f"/packet/{env.pid}").text
    assert f'id="draft-stopped-{started.run_id}"' in page and "did not finish" in page
    # Generate again works and replaces it.
    app2.state.packet_runner = FakeRunner([RESUME_OUT, ENTAIL])
    env2 = SimpleNamespace(**{**vars(env), "client": c2, "app": app2})
    assert generate(env2, estimate(env2)).status_code == 202
    join(env2)
    assert status(env2)["state"] == "done"
    assert "draft-stopped" not in c2.get(f"/packet/{env.pid}").text


def status_with(env, client, kind="resume"):
    r = post(env, f"packets/{env.pid}/draft-status", {"kind": kind}, client=client)
    assert r.status_code == 200, r.text
    return r.json()


def test_shutdown_joins_a_running_draft(tmp_path):
    app, settings, db_path = build_app(tmp_path)
    runner = FakeRunner([RESUME_OUT, ENTAIL], gate=True)
    app.state.packet_runner = runner
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        env = SimpleNamespace(client=client, app=app)
        r = client.post("/apply/new", data={"text": POSTING, "employer": "E", "title": "T"})
        env.pid = int(r.headers["location"].rsplit("/", 1)[1])
        code, _ = ext_pairing.new_code(ext_pairing.state_path(settings.paths.data_dir), NOW)
        env.token = client.post(
            "/ext/v1/pair", json={"code": code},
            headers={"Origin": ORIGIN, "X-Jobhunter-Ext": "0.1.0"},
        ).json()["token"]  # fmt: skip
        assert generate(env, estimate(env)).status_code == 202
        assert runner.started.wait(5)
        threading.Timer(0.3, runner.release.set).start()
    # Leaving the client ran the lifespan shutdown: it waited for the thread to finish.
    assert all(not t.is_alive() for t in app.state.ext_draft_threads)
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT outcome FROM draft_run").fetchone()[0] == "done"
    finally:
        conn.close()


# ─── kinds: letter with notes, question drafts ─────────────────────────────


def test_question_draft_needs_a_packet_and_offers_prepare(env):
    r = post(env, "packets/99999/estimate", {"kind": "question_draft", "question": QUESTION})
    assert r.status_code == 404 and r.json()["needs_packet"] is True
    assert "Prepare" in r.json()["error"]
    r = post(
        env,
        "packets/99999/generate",
        {"action_id": str(uuid.uuid4()), "kind": "question_draft", "question": QUESTION,
         "token": "x"},
    )  # fmt: skip
    assert r.status_code == 404 and r.json()["needs_packet"] is True
    assert rows(env, "draft_run") == []


def test_question_draft_runs_in_the_background(env):
    env.app.state.packet_runner = FakeRunner([DRAFT_OUT, ENTAIL])
    est = estimate(env, "question_draft", question=QUESTION)
    assert est["question_key"]
    r = generate(env, est, "question_draft", question=QUESTION)
    assert r.status_code == 202 and r.json()["question_key"] == est["question_key"]
    join(env)
    s = status(env, "question_draft", question=QUESTION)
    assert s["state"] == "done" and s["version"] == 1
    assert status(env, "question_draft", question="Another question?")["state"] == "none"


def test_never_store_question_is_refused_before_anything(env):
    est = post(env, f"packets/{env.pid}/estimate",
               {"kind": "question_draft", "question": "What is your date of birth?"})  # fmt: skip
    assert est.status_code == 409 and env.app.state.packet_runner.calls == []


def test_letter_notes_are_estimated_and_saved_with_the_run(env):
    use_base_resume(env)
    env.app.state.packet_runner = FakeRunner(
        [{"cover_letter": {"paragraphs": [{"text": f"You want someone to {QUOTE}.",
                                           "resume_sources": ["L6"],
                                           "posting_quotes": [QUOTE]}]}}, ENTAIL]
    )  # fmt: skip
    notes = "They ship synthetic widgets"
    est = estimate(env, "cover_letter", notes=notes)
    assert rows(env, "packet_answer") == []  # an estimate writes nothing
    r = generate(env, est, "cover_letter", notes=notes)
    assert r.status_code == 202
    join(env)
    assert status(env, "cover_letter")["state"] == "done"
    saved = rows(env, "packet_answer", "WHERE field_key = 'notes:employer'")
    assert saved and saved[0]["value"] == notes
    stdin = env.app.state.packet_runner.calls[0]["user_message"]
    assert "N1: They ship synthetic widgets" in stdin


def test_status_before_any_run_is_none(env):
    s = status(env)
    assert s["state"] == "none" and s["run_id"] is None


# ─── the claim object ──────────────────────────────────────────────────────


def test_claim_object_is_held_until_released_and_release_is_idempotent(tmp_path):
    c = inflight.Claim.take(tmp_path, "7-resume-")
    assert c.held and inflight.held(tmp_path, "7-resume-")
    with pytest.raises(inflight.Busy):
        inflight.Claim.take(tmp_path, "7-resume-")
    with pytest.raises(inflight.Busy), inflight.claim(tmp_path, "7-resume-"):
        pass
    done = threading.Thread(target=c.release)  # released from another thread, as a draft does
    done.start()
    done.join()
    c.release()
    assert not c.held and not inflight.held(tmp_path, "7-resume-")
    with inflight.claim(tmp_path, "7-resume-"):
        assert inflight.held(tmp_path, "7-resume-")
    assert not inflight.held(tmp_path, "7-resume-")


def test_draft_runs_state_without_outcome_follows_the_claim(tmp_path):
    conn = db.connect(":memory:")
    db.migrate(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    rid = draft_runs.begin(conn, 5, "resume", "", "cli", NOW)
    with inflight.claim(tmp_path, generator.claim_key(5, "resume", "")):
        assert draft_runs.latest(conn, tmp_path, 5, "resume").state == "drafting"
    assert draft_runs.latest(conn, tmp_path, 5, "resume").state == "stopped"
    draft_runs.finish(conn, rid, NOW, error="boom")
    run = draft_runs.latest(conn, tmp_path, 5, "resume")
    assert run.state == "failed" and run.error == "boom"
    assert draft_runs.unfinished(conn, tmp_path, 5) == []
    conn.close()
