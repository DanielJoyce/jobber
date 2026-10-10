"""The packet generator (specs/017 phase 1b) on both runners, offline.

The CLI runner runs a fake ``claude`` (tests/conftest.py) replaying the recorded-shape
transcript; the API runner gets a mocked ``anthropic`` client. Every test that expects no SDK
call passes a client factory that fails the test if called, so "no API call without the
click" is asserted, not assumed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jobhunter.apply import answers, generator, paste, review, runner_state
from jobhunter.config import Apply, Paths, Settings
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
RESUME = """\
Synthetic Person
<you>@example.com | Denver, CO

Experience
Platform Engineer, Acme Synthetic Corp
2019 - 2023
- Contributed to the migration of 40 services to Kubernetes
- Wrote Terraform modules used by 3 teams
- Cut deploy time by 35%
Skills: Python, Go, Terraform, Kubernetes
"""
QUOTE = "build our Kubernetes platform"
POSTING = f"Synthetic Widgets is hiring.\nYou will {QUOTE} and own the deploy pipeline."
PREFS = """\
resume_path: resume.md
current_focus:
  doing: platform work
narrative:
  want: Calm platform teams.
answers:
  links:
    portfolio: https://example.com/SECRET-PREFS-LINK
"""

RESUME_OUT = {
    "resume": {
        "header": ["L1", "L2"],
        "summary": {
            "text": "Platform engineer who moves services to Kubernetes.",
            "sources": ["L4", "L6"],
        },
        "sections": [
            {
                "heading": "Experience",
                "entries": [
                    {
                        "source_line": "L4",
                        "employer": "Acme Synthetic Corp",
                        "title": "Platform Engineer",
                        "dates": "2019 - 2023",
                        "bullets": [
                            {
                                "text": "Contributed to moving 40 services onto Kubernetes",
                                "sources": ["L6"],
                            },
                            {"text": "Led the Terraform work for 3 teams", "sources": ["L7"]},
                        ],
                    }
                ],
            }
        ],
        "skills": [{"name": "Terraform", "sources": ["L9"]}],
        "omitted": ["L8"],
        "change_notes": ["Led with Kubernetes: the posting asks to build a Kubernetes platform."],
    }
}
ENTAIL_OUT = {
    "lines": [{"id": "summary", "verdict": "yes"}, {"id": "s0.e0.b1", "verdict": "partly"}]
}
LETTER_OUT = {
    "cover_letter": {
        "paragraphs": [
            {
                "text": f"You want someone to {QUOTE}; I moved 40 services to Kubernetes.",
                "resume_sources": ["L6"],
                "posting_quotes": [QUOTE],
            }
        ]
    }
}


@pytest.fixture
def env(tmp_path):
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (pdir / "resume.md").write_text(RESUME, encoding="utf-8")
    settings = Settings(
        paths=Paths(
            profile_dir=pdir,
            db_path=tmp_path / "t.db",
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
        ),
        apply=Apply(daily_cap_usd=1.0),
    )
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    _gid, pid = paste.create_pasted_packet(
        conn,
        url=None,
        text=POSTING,
        employer="Synthetic Widgets Inc",
        title="Platform Engineer",
        now=NOW,
    )
    # Saved answers are never sent to a model (specs/017 "Never sent").
    conn.execute(
        "INSERT INTO packet_answer (packet_id, field_key, label, value, source) "
        "VALUES (?, 'q:why us', 'Why us?', 'SECRET-SAVED-ANSWER', 'user')",
        (pid,),
    )
    profile = load_profile(pdir)
    yield SimpleNamespace(settings=settings, conn=conn, pid=pid, profile=profile, tmp=tmp_path)
    conn.close()


def no_sdk():
    pytest.fail("the SDK must not be called without the API click")


def gen(env, kind="resume", runner="cli", **kw):
    kw.setdefault("client_factory", no_sdk)
    return generator.generate(
        env.conn, env.settings, env.profile, env.pid, kind, runner=runner, now=NOW, **kw
    )


def spend(env):
    return {
        (r["tier"], r["model"]): (r["calls"], r["cost_usd"])
        for r in env.conn.execute("SELECT * FROM llm_spend")
    }


# ─── CLI runner ─────────────────────────────────────────────────────────────


def test_cli_resume_writes_v1_logs_zero_cost_and_runs_haiku_entailment(
    env, fake_claude, claude_stream
):
    fake_claude.set(
        [claude_stream(RESUME_OUT), claude_stream(ENTAIL_OUT, model="claude-haiku-4-5")]
    )
    out = gen(env)
    assert (out.version, out.runner, out.cost_usd, out.entailment) == (1, "cli", 0.0, "done")
    v = review.get_version(env.conn, out.doc_id)
    assert (v.origin, v.runner, v.cost_usd) == ("generated", "cli", 0.0)
    assert v.api_equiv_usd == pytest.approx(0.1259 * 2)
    # The checker ran: "Led" is stronger than L7 says; entailment is advisory only.
    by_key = {i["key"]: i for i in v.report["items"]}
    assert by_key["s0.e0.b1"]["status"] == "unsupported"
    assert by_key["s0.e0.b1"]["entail"] == "partly"
    assert by_key["summary"]["entail"] == "yes"
    assert v.report["ok"] is False
    assert (
        env.conn.execute("SELECT resume_doc_id FROM application_packet").fetchone()[0] == out.doc_id
    )
    # Two calls: Opus with effort, then Haiku with none.
    gen_call, entail_call = fake_claude.calls()
    assert gen_call["argv"][gen_call["argv"].index("--model") + 1] == "opus"
    assert gen_call["argv"][gen_call["argv"].index("--effort") + 1] == "medium"
    assert entail_call["argv"][entail_call["argv"].index("--model") + 1] == "haiku"
    assert "--effort" not in entail_call["argv"]
    # Subscription calls are counted and logged at $0 under packet-cli.
    rows = spend(env)
    assert rows == {
        ("packet-cli", "claude-opus-5"): (1, 0.0),
        ("packet-cli", "claude-haiku-4-5"): (1, 0.0),
    }


def test_cli_request_sends_resume_posting_and_direction_but_no_saved_answers(
    env, fake_claude, claude_stream
):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL_OUT)])
    gen(env)
    stdin = fake_claude.calls()[0]["stdin"]
    assert "L1: Synthetic Person" in stdin
    assert "L6: - Contributed to the migration of 40 services to Kubernetes" in stdin
    assert QUOTE in stdin
    assert "Current focus: platform work" in stdin and "Calm platform teams." in stdin
    for call in fake_claude.calls():
        blob = json.dumps(call["argv"]) + call["stdin"]
        assert "SECRET-SAVED-ANSWER" not in blob
        assert "SECRET-PREFS-LINK" not in blob


def test_cli_failure_saves_nothing_and_offers_the_api_without_calling_it(env, fake_claude):
    fake_claude.set([{"steps": [], "exit": 1, "stderr": "Error: usage limit reached"}])
    with pytest.raises(generator.GenerateFailed) as exc:
        gen(env)
    assert exc.value.offer_api is True
    assert "usage limit" in exc.value.reason
    assert env.conn.execute("SELECT count(*) FROM packet_document").fetchone()[0] == 0
    assert not runner_state.load(env.settings.paths.data_dir).off


def test_invalid_structured_output_is_logged_but_not_saved(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream({"resume": {"summary": "not an object"}})])
    with pytest.raises(generator.GenerateFailed, match="did not match") as exc:
        gen(env)
    assert exc.value.offer_api
    assert env.conn.execute("SELECT count(*) FROM packet_document").fetchone()[0] == 0
    assert spend(env) == {("packet-cli", "claude-opus-5"): (1, 0.0)}


def test_api_key_auth_on_init_turns_the_runner_off_and_it_stays_off(
    env, fake_claude, claude_stream
):
    fake_claude.set([claude_stream(RESUME_OUT, init={"apiKeySource": "ANTHROPIC_API_KEY"})])
    with pytest.raises(generator.GenerateFailed, match="API key"):
        gen(env)
    state = runner_state.load(env.settings.paths.data_dir)
    assert state.off and "API key" in state.off_reason
    saved = json.loads((env.settings.paths.data_dir / "apply-runner.json").read_text())
    assert "API key" in saved["off"]["reason"]
    assert spend(env) == {}  # killed before the model call: nothing to log
    # A later click is refused up front and offers the API; the fake is not run again.
    n = len(fake_claude.calls())
    with pytest.raises(generator.ApplyRefused, match="CLI runner is off") as exc:
        gen(env)
    assert exc.value.offer_api and len(fake_claude.calls()) == n
    runner_state.turn_on(env.settings.paths.data_dir)
    assert not runner_state.load(env.settings.paths.data_dir).off


def test_auth_status_not_claude_ai_turns_the_runner_off(env, fake_claude, claude_stream):
    fake_claude.set(
        [claude_stream(RESUME_OUT)],
        auth={"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"},
    )
    with pytest.raises(generator.GenerateFailed) as exc:
        gen(env)
    assert exc.value.offer_api
    assert runner_state.load(env.settings.paths.data_dir).off
    assert fake_claude.calls() == []  # never got past auth status


def test_overage_is_charged_to_the_apply_cap_and_holds_further_cli_calls(
    env, fake_claude, claude_stream
):
    fake_claude.set(
        [
            claude_stream(RESUME_OUT, overage=True),
            claude_stream(ENTAIL_OUT, overage=True, model="claude-haiku-4-5"),
        ]
    )
    out = gen(env)
    assert out.cost_usd == pytest.approx(0.1259)
    rows = spend(env)
    assert rows[("packet", "claude-opus-5")] == (1, pytest.approx(0.1259))
    assert runner_state.load(env.settings.paths.data_dir).overage
    with pytest.raises(generator.ApplyRefused, match="paid extra usage") as exc:
        gen(env)
    assert exc.value.needs_paid_confirm
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL_OUT)])
    out2 = gen(env, confirm_paid=True)
    assert out2.version == 2
    assert not runner_state.load(env.settings.paths.data_dir).overage  # cleared by a free call


def test_overage_with_the_cap_reached_kills_the_call(env, fake_claude, claude_stream):
    env.conn.execute(
        "INSERT INTO llm_spend VALUES ('2026-10-10', 'claude-opus-5', 'packet', 5, 0, 0, 1.0)"
    )
    run = claude_stream(RESUME_OUT, overage=True)
    run["steps"][2:2] = [{"sleep": 3}, {"touch": "went_on"}]
    fake_claude.set([run])
    with pytest.raises(generator.GenerateFailed, match="paid extra usage"):
        gen(env)
    assert not fake_claude.reached("went_on")
    assert env.conn.execute("SELECT count(*) FROM packet_document").fetchone()[0] == 0


def test_missing_cli_is_refused_and_offers_the_api(env):
    with pytest.raises(generator.ApplyRefused, match="not installed") as exc:
        gen(env)
    assert exc.value.offer_api


def test_cli_spend_never_counts_against_the_apply_cap(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL_OUT)])
    gen(env)
    assert generator.packet_spent_today(env.conn, NOW) == 0.0


# ─── API runner ─────────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, outputs, usage=(8000, 3000)):
        self.outputs = list(outputs)
        self.params: list[dict] = []
        self.usage = usage
        self.messages = self

    def stream(self, **params):
        self.params.append(params)
        body = self.outputs.pop(0)
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(body))],
            usage=SimpleNamespace(
                input_tokens=self.usage[0],
                output_tokens=self.usage[1],
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            model=params["model"],
            stop_reason="end_turn",
        )

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: msg)

            def __exit__(self_inner, *a):
                return False

        return Ctx()


def test_api_run_on_click_streams_opus_with_effort_and_caches_the_resume(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    client = FakeClient([RESUME_OUT, ENTAIL_OUT])
    out = gen(env, runner="api", client_factory=lambda: client)
    p, e = client.params
    assert p["model"] == "claude-opus-5"
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"]["effort"] == "medium"
    assert p["output_config"]["format"]["type"] == "json_schema"
    assert p["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "L1: Synthetic Person" in p["system"][1]["text"]
    assert QUOTE in p["messages"][0]["content"]
    assert "SECRET-SAVED-ANSWER" not in json.dumps(p) and "SECRET-PREFS-LINK" not in json.dumps(p)
    # Haiku entailment: no thinking, no effort.
    assert e["model"] == "claude-haiku-4-5"
    assert "thinking" not in e and "effort" not in e["output_config"]
    opus = (8000 * 5 + 3000 * 25) / 1e6
    assert out.cost_usd == pytest.approx(opus)
    rows = spend(env)
    assert rows[("packet", "claude-opus-5")] == (1, pytest.approx(opus))
    assert rows[("packet", "claude-haiku-4-5")][0] == 1
    v = review.get_version(env.conn, out.doc_id)
    assert (v.runner, v.cost_usd) == ("api", pytest.approx(opus))


def test_api_over_the_apply_cap_is_refused_before_any_call(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    env.conn.execute(
        "INSERT INTO llm_spend VALUES ('2026-10-10', 'claude-opus-5', 'packet', 5, 0, 0, 0.95)"
    )
    with pytest.raises(generator.ApplyRefused, match="daily cap"):
        gen(env, runner="api", client_factory=no_sdk)
    # Scoring spend does not count against the apply cap.
    env.conn.execute("DELETE FROM llm_spend")
    env.conn.execute(
        "INSERT INTO llm_spend VALUES "
        "('2026-10-10', 'anthropic:claude-haiku-4-5', 'screen', 5, 0, 0, 5.0)"
    )
    client = FakeClient([RESUME_OUT, ENTAIL_OUT])
    assert gen(env, runner="api", client_factory=lambda: client).version == 1


def test_api_without_a_key_is_refused(env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(generator.ApplyRefused, match="ANTHROPIC_API_KEY"):
        gen(env, runner="api", client_factory=no_sdk)


def test_estimate_uses_the_median_api_cost_once_there_is_history(env, monkeypatch):
    first = generator.estimate(env.conn, env.settings, "resume", 32000)
    assert first.source == "token estimate"
    assert first.usd == pytest.approx((8000 * 5 + 4500 * 25) / 1e6 + 0.003)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    for usage in ((8000, 3000), (8000, 5000), (8000, 4000)):
        gen(
            env,
            runner="api",
            client_factory=lambda u=usage: FakeClient([RESUME_OUT, ENTAIL_OUT], u),
        )
    est = generator.estimate(env.conn, env.settings, "resume", 1)
    assert est.source == "median of 3"
    assert est.usd == pytest.approx((8000 * 5 + 4000 * 25) / 1e6 + 0.003)


# ─── letter and drafts ──────────────────────────────────────────────────────


def test_cover_letter_needs_a_resume_version_and_never_makes_one(env, fake_claude, claude_stream):
    with pytest.raises(generator.ApplyRefused, match="resume version first"):
        gen(env, kind="cover_letter")
    base = generator.use_base_resume(env.conn, env.profile, env.pid, now=NOW)
    review.mark_ready(env.conn, env.pid, NOW)
    answers.save_packet_answer(
        env.conn, env.pid, answers.NOTES_KEY, "Employer notes", "Their docs taught me Terraform."
    )
    fake_claude.set([claude_stream(LETTER_OUT), claude_stream({"lines": []})])
    out = gen(env, kind="cover_letter")
    row = env.conn.execute(
        "SELECT status, resume_doc_id, cover_doc_id FROM application_packet"
    ).fetchone()
    assert tuple(row) == ("draft", base, out.doc_id)  # back to draft; resume untouched
    assert (
        env.conn.execute("SELECT count(*) FROM packet_document WHERE kind = 'resume'").fetchone()[0]
        == 1
    )
    stdin = fake_claude.calls()[0]["stdin"]
    assert "N1: Their docs taught me Terraform." in stdin
    assert "Synthetic Person" in stdin  # the current resume version as context
    v = review.get_version(env.conn, out.doc_id)
    assert v.report["ok"] is True


@pytest.mark.parametrize(
    "question", ["What are your salary expectations?", "Gender", "Desired pay*", "Date of birth:"]
)
def test_never_store_question_gets_no_draft_and_no_call(env, fake_claude, question):
    with pytest.raises(generator.ApplyRefused, match="yours"):
        gen(env, kind="question_draft", question=question)
    assert fake_claude.calls(auth=None) == []


def test_behavioral_question_needs_story_facts_then_sends_them(env, fake_claude, claude_stream):
    q = "Describe a time you handled a failed deploy."
    with pytest.raises(generator.ApplyRefused, match="story"):
        gen(env, kind="question_draft", question=q)
    answers.save_packet_answer(
        env.conn, env.pid, answers.story_key(q), q, "The 2am upgrade failed.\nI rolled it back."
    )
    out_draft = {
        "draft": {
            "sentences": [
                {
                    "text": "When an upgrade failed at 2am, I rolled it back.",
                    "sources": ["S1", "S2"],
                    "posting_quotes": [],
                }
            ]
        }
    }
    fake_claude.set([claude_stream(out_draft), claude_stream({"lines": []})])
    out = gen(env, kind="question_draft", question=q)
    stdin = fake_claude.calls()[0]["stdin"]
    assert "S1: The 2am upgrade failed." in stdin and "S2: I rolled it back." in stdin
    v = review.get_version(env.conn, out.doc_id)
    assert v.question_key == "describe a time you handled a failed deploy"
    assert v.report["ok"] is True


def test_numeric_question_shows_evidence_lines_and_never_calls(env, fake_claude):
    q = "How many years of experience do you have with Terraform?"
    with pytest.raises(generator.ApplyRefused, match="evidence"):
        gen(env, kind="question_draft", question=q)
    lines = {"L7": "- Wrote Terraform modules used by 3 teams", "L1": "Synthetic Person"}
    assert generator.numeric_evidence(lines, q) == [("L7", lines["L7"])]
    assert fake_claude.calls(auth=None) == []


def test_story_facts_on_the_never_store_list_are_refused(env):
    with pytest.raises(answers.NeverStore):
        answers.save_packet_answer(env.conn, env.pid, "story:gender", "Gender", "x")
    assert (
        env.conn.execute(
            "SELECT count(*) FROM packet_answer WHERE field_key = 'story:gender'"
        ).fetchone()[0]
        == 0
    )
