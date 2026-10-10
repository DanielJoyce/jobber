"""``jobhunter apply draft`` (specs/017 "CLI runner", row "Command"), offline.

The subscription runner is the fake ``claude``; the API client is replaced, and fails the
test unless the test expects the API run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobhunter.apply import generator, paste
from jobhunter.cli import app
from jobhunter.core import db

cli = CliRunner()
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
RESUME = (
    "Synthetic Person\n<you>@example.com\nPlatform Engineer, Acme Synthetic Corp\n2019 - 2023\n"
    "- Wrote Terraform modules used by 3 teams\n"
)
OUT = {
    "resume": {
        "header": ["L1", "L2"],
        "summary": {"text": "Platform engineer.", "sources": ["L3"]},
        "sections": [
            {
                "heading": "Experience",
                "entries": [
                    {
                        "source_line": "L3",
                        "employer": "Acme Synthetic Corp",
                        "title": "Platform Engineer",
                        "dates": "2019 - 2023",
                        "bullets": [
                            {"text": "Wrote Terraform modules for 3 teams", "sources": ["L5"]}
                        ],
                    }
                ],
            }
        ],
        "skills": [],
        "omitted": [],
        "change_notes": [],
    }
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "preferences.yaml").write_text("resume_path: resume.md\n", encoding="utf-8")
    (pdir / "resume.md").write_text(RESUME, encoding="utf-8")
    db_path = tmp_path / "jh.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\ndb_path = "{db_path}"\nprofile_dir = "{pdir}"\n'
        f'data_dir = "{tmp_path / "data"}"\ncache_dir = "{tmp_path / "cache"}"\n'
    )
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(generator, "_default_client", lambda: pytest.fail("no API call expected"))
    c = db.connect(db_path)
    db.migrate(c)
    _gid, pid = paste.create_pasted_packet(
        c,
        url=None,
        text="Build our platform.",
        employer="Synthetic Widgets",
        title="Engineer",
        now=NOW,
    )
    yield SimpleNamespace(conn=c, pid=pid)
    c.close()


def docs(env):
    return env.conn.execute("SELECT kind, version, runner FROM packet_document").fetchall()


def test_resume_draft_on_the_subscription(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(OUT), claude_stream({"lines": []})])
    out = cli.invoke(app, ["apply", "draft", str(env.pid)])
    assert out.exit_code == 0, out.output
    assert "resume v1 written (subscription)" in out.output
    assert [tuple(r) for r in docs(env)] == [("resume", 1, "cli")]


def test_failure_never_switches_to_the_api(env, fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    fake_claude.set([{"steps": [], "exit": 1, "stderr": "Error: limit"}])
    out = cli.invoke(app, ["apply", "draft", str(env.pid)])
    assert out.exit_code == 1
    assert "failed, nothing saved" in out.output and "--runner api" in out.output
    assert docs(env) == []


def test_api_run_shows_the_cost_and_can_be_declined(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    out = cli.invoke(app, ["apply", "draft", str(env.pid), "--runner", "api"], input="n\n")
    assert out.exit_code == 1
    assert "API run: about $" in out.output and "not run" in out.output
    assert docs(env) == []


def test_api_run_on_yes(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    calls = []

    class Client:
        messages = None

        def __init__(self):
            self.messages = self

        def stream(self, **params):
            calls.append(params["model"])
            body = OUT if len(calls) == 1 else {"lines": []}
            msg = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(body))],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
                model=params["model"],
                stop_reason="end_turn",
            )

            class Ctx:
                def __enter__(self_inner):
                    return SimpleNamespace(get_final_message=lambda: msg)

                def __exit__(self_inner, *a):
                    return False

            return Ctx()

    monkeypatch.setattr(generator, "_default_client", Client)
    out = cli.invoke(app, ["apply", "draft", str(env.pid), "--runner", "api"], input="y\n")
    assert out.exit_code == 0, out.output
    assert calls == ["claude-opus-5", "claude-haiku-4-5"]
    assert [tuple(r) for r in docs(env)] == [("resume", 1, "api")]


def test_never_store_question_is_refused(env, fake_claude):
    out = cli.invoke(app, ["apply", "draft", str(env.pid), "--question", "Desired salary"])
    assert out.exit_code == 1 and "yours" in out.output
    assert fake_claude.calls(auth=None) == []


def test_numeric_question_prints_evidence(env, fake_claude):
    out = cli.invoke(app, ["apply", "draft", str(env.pid), "--question", "Years of Terraform?"])
    assert out.exit_code == 0
    assert "L5 - Wrote Terraform modules used by 3 teams" in out.output
    assert fake_claude.calls(auth=None) == []


def test_letter_and_question_together_is_a_usage_error(env):
    out = cli.invoke(app, ["apply", "draft", str(env.pid), "--letter", "--question", "Why us?"])
    assert out.exit_code == 2
