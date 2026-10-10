"""Saved answers and /prefs Application answers are never sent to a model (specs/017
"Never sent", Testing row "Generator").

Every document kind is generated on both runners with sentinels planted in every answer store:
a saved ``q:`` answer for the very question being drafted, and an ``answers:`` block with a link,
a notice period, work authorization and a "why us" template for that question. The test reads
everything that would leave the machine (the fake ``claude``'s argv and stdin, the fake API
client's parameters) and fails if any sentinel is in it. It would fail if the generator ever
started offering saved answers to the model as context.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_generator import ENTAIL_OUT, LETTER_OUT, POSTING, RESUME, RESUME_OUT, FakeClient

from jobhunter.apply import answers, generator, paste
from jobhunter.config import Apply, Paths, Settings
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
WHY = "Why do you want to work at Synthetic Widgets?"
SENTINELS = (
    "SENTINEL-PREFS-LINK",
    "SENTINEL-NOTICE",
    "SENTINEL-TEMPLATE",
    "SENTINEL-SAVED-Q",
    "SENTINEL-OTHER-PACKET",
)
PREFS = f"""\
resume_path: resume.md
current_focus:
  doing: platform work
answers:
  links:
    github: https://example.com/SENTINEL-PREFS-LINK
  notice_period: SENTINEL-NOTICE
  work_authorization: true
  custom:
    - question: {WHY}
      answer: SENTINEL-TEMPLATE
"""
DRAFT_OUT = {
    "draft": {
        "sentences": [
            {"text": "I build Kubernetes platforms.", "sources": ["L6"], "posting_quotes": []}
        ]
    }
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
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
        apply=Apply(daily_cap_usd=5.0, entailment_check=False),
    )
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    _g, pid = paste.create_pasted_packet(
        conn, url=None, text=POSTING, employer="Synthetic Widgets Inc", title="Engineer", now=NOW
    )
    _g2, other = paste.create_pasted_packet(
        conn, url=None, text=POSTING, employer="Other Synthetic Co", title="Engineer", now=NOW
    )
    # Save as answer, on this packet and on an earlier one, for the question being drafted.
    answers.save_question_answer(conn, pid, WHY, "SENTINEL-SAVED-Q")
    answers.save_question_answer(conn, other, WHY, "SENTINEL-OTHER-PACKET")
    assert answers.load_answers(pdir).answers.notice_period == "SENTINEL-NOTICE"
    yield SimpleNamespace(conn=conn, settings=settings, pid=pid, profile=load_profile(pdir))
    conn.close()


def run(env, kind, runner, **kw):
    return generator.generate(
        env.conn, env.settings, env.profile, env.pid, kind, runner=runner, now=NOW, **kw
    )


def assert_clean(blob: str):
    for s in SENTINELS:
        assert s not in blob, s


def test_cli_runner_never_sends_answers(env, fake_claude, claude_stream):
    fake_claude.set(
        [claude_stream(RESUME_OUT), claude_stream(LETTER_OUT), claude_stream(DRAFT_OUT)]
    )
    run(env, "resume", "cli", client_factory=lambda: pytest.fail("no SDK"))
    run(env, "cover_letter", "cli", client_factory=lambda: pytest.fail("no SDK"))
    run(env, "question_draft", "cli", question=WHY, client_factory=lambda: pytest.fail("no SDK"))
    calls = fake_claude.calls()
    assert len(calls) == 3
    for call in calls:
        assert_clean(json.dumps(call["argv"]) + call["stdin"])
    assert WHY in calls[2]["stdin"]  # the question itself does go, as the spec says


def test_api_runner_never_sends_answers(env):
    client = FakeClient([RESUME_OUT, LETTER_OUT, DRAFT_OUT])
    run(env, "resume", "api", client_factory=lambda: client)
    run(env, "cover_letter", "api", client_factory=lambda: client)
    run(env, "question_draft", "api", question=WHY, client_factory=lambda: client)
    assert len(client.params) == 3
    for params in client.params:
        assert_clean(json.dumps(params, default=str))


def test_the_entailment_pass_never_sends_answers_either(env, fake_claude, claude_stream):
    env.settings = env.settings.model_copy(
        update={"apply": env.settings.apply.model_copy(update={"entailment_check": True})}
    )
    fake_claude.set([claude_stream(DRAFT_OUT), claude_stream(ENTAIL_OUT)])
    run(env, "question_draft", "cli", question=WHY, client_factory=lambda: pytest.fail("no SDK"))
    for call in fake_claude.calls():
        assert_clean(json.dumps(call["argv"]) + call["stdin"])
