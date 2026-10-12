"""The /prefs contact block and sponsorship answer reach no model (specs/022 "The contact block").

Canary values are set ONLY in ``answers.contact`` (and the sponsorship flag); the resume fixture
does not contain them, so a failure means the answers store leaked into a request, not that the
resume (which goes to models as 017 sends it) carried them. Every request body that would leave
the machine is read: scoring (Anthropic params and the provider-neutral pieces), the deep pass,
and drafting on both runners with the entailment pass on.
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
from jobhunter.scoring import deep, screen
from jobhunter.scoring.profile import load_profile

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
CANARIES = (
    "canaryfirst",
    "canarylast",
    "canarypreferred",
    "canary.contact@invalid.test",
    "canaryphone-5550100",
    "canarycity",
    "canaryregion",
    "canarycountry",
)
PREFS = """\
resume_path: resume.md
current_focus:
  doing: platform work
answers:
  requires_sponsorship: true
  contact:
    first_name: canaryfirst
    last_name: canarylast
    preferred_name: canarypreferred
    email: canary.contact@invalid.test
    phone: canaryphone-5550100
    city: canarycity
    region: canaryregion
    country: canarycountry
"""
DRAFT_OUT = {
    "draft": {
        "sentences": [
            {"text": "I build Kubernetes platforms.", "sources": ["L6"], "posting_quotes": []}
        ]
    }
}
JOB = {
    "title": "Systems Engineer",
    "employer": "Synthetic Widgets Inc",
    "description_text": POSTING,
    "location_scope": "single",
    "remote": "hybrid",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "preferences.yaml").write_text(PREFS, encoding="utf-8")
    (pdir / "resume.md").write_text(RESUME, encoding="utf-8")
    assert not any(c in RESUME for c in CANARIES)  # the canaries live only in the contact block
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
    loaded = answers.load_answers(pdir)
    assert loaded.answers.contact.email == "canary.contact@invalid.test"  # the block is live
    assert loaded.answers.requires_sponsorship is True
    yield SimpleNamespace(conn=conn, settings=settings, pid=pid, profile=load_profile(pdir))
    conn.close()


def assert_clean(blob: str):
    for c in CANARIES:
        assert c not in blob, c


def test_scoring_and_deep_requests_carry_no_contact_value(env):
    request = screen.build_score_request(JOB, "Denver, CO", env.profile)
    assert_clean(json.dumps(request.__dict__, default=str))
    params = screen.build_request(JOB, "Denver, CO", env.profile)
    assert_clean(json.dumps(params, default=str))
    assert "Synthetic Person" in json.dumps(params)  # the resume did go: the check can fail
    assert_clean(json.dumps(deep.build_request(JOB, "Denver, CO", env.profile), default=str))


def test_drafting_requests_carry_no_contact_value_on_either_runner(env, fake_claude, claude_stream):
    run = lambda kind, runner, **kw: generator.generate(  # noqa: E731
        env.conn, env.settings, env.profile, env.pid, kind, runner=runner, now=NOW, **kw
    )
    why = "Why do you want to work at Synthetic Widgets?"
    fake_claude.set(
        [
            claude_stream(RESUME_OUT),
            claude_stream(LETTER_OUT),
            claude_stream(DRAFT_OUT),
        ]
    )
    nosdk = lambda: pytest.fail("no SDK")  # noqa: E731
    run("resume", "cli", client_factory=nosdk)
    run("cover_letter", "cli", client_factory=nosdk)
    run("question_draft", "cli", question=why, client_factory=nosdk)
    calls = fake_claude.calls()
    assert len(calls) == 3
    for call in calls:
        assert_clean(json.dumps(call["argv"]) + call["stdin"])

    client = FakeClient([RESUME_OUT, LETTER_OUT, DRAFT_OUT])
    run("resume", "api", client_factory=lambda: client)
    run("cover_letter", "api", client_factory=lambda: client)
    run("question_draft", "api", question=why, client_factory=lambda: client)
    assert len(client.params) == 3
    for params in client.params:
        assert_clean(json.dumps(params, default=str))


def test_the_entailment_pass_carries_no_contact_value_either(env, fake_claude, claude_stream):
    env.settings = env.settings.model_copy(
        update={"apply": env.settings.apply.model_copy(update={"entailment_check": True})}
    )
    fake_claude.set([claude_stream(DRAFT_OUT), claude_stream(ENTAIL_OUT)])
    generator.generate(
        env.conn,
        env.settings,
        env.profile,
        env.pid,
        "question_draft",
        runner="cli",
        now=NOW,
        question="Why do you want to work at Synthetic Widgets?",
        client_factory=lambda: pytest.fail("no SDK"),
    )
    calls = fake_claude.calls()
    assert len(calls) == 2  # the draft and the entailment check
    for call in calls:
        assert_clean(json.dumps(call["argv"]) + call["stdin"])
