"""Packet documents in the console (specs/017 phase 1b): generate, review, edit, Mark ready.

The CLI runner is a fake ``claude`` on PATH (tests/conftest.py); the API client is a fake
injected into ``app.state``. Tests that must not reach the API install a factory that fails
the test when called, so "no SDK call without the click" is checked on every path.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobhunter.apply import runner_state
from jobhunter.config import Paths, Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

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
POSTING = f"Synthetic Widgets is hiring.\n\nYou will {QUOTE} and own the deploy pipeline."
LED = "Led the Terraform work for 3 teams"
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
                            {"text": LED, "sources": ["L7"]},
                        ],
                    }
                ],
            }
        ],
        "skills": [{"name": "Terraform", "sources": ["L9"]}],
        "omitted": ["L8"],
        "change_notes": ["Led with Kubernetes: the posting asks for it."],
    }
}
ENTAIL = {"lines": [{"id": "s0.e0.b1", "verdict": "partly"}]}
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
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pdir = tmp_path / "profile"
    pdir.mkdir()
    (pdir / "preferences.yaml").write_text("resume_path: resume.md\n", encoding="utf-8")
    (pdir / "resume.md").write_text(RESUME, encoding="utf-8")
    db_path = tmp_path / "t.db"
    c = db.connect(db_path)
    db.migrate(c)
    c.close()
    settings = Settings(
        paths=Paths(
            profile_dir=pdir,
            db_path=db_path,
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
        )
    )
    app = create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW)
    app.state.packet_api_client_factory = lambda: pytest.fail("no API call without the click")
    client = TestClient(app, follow_redirects=False)
    r = client.post(
        "/apply/new",
        data={"text": POSTING, "employer": "Synthetic Widgets Inc", "title": "Platform Engineer"},
    )
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    conn = db.connect(db_path)
    yield SimpleNamespace(client=client, app=app, pid=pid, conn=conn, settings=settings)
    conn.close()


def generate(env, kind="resume", runner="cli", **data):
    return env.client.post(
        f"/packet/{env.pid}/generate", data={"kind": kind, "runner": runner, **data}
    )


def docs(env, kind="resume"):
    return env.conn.execute(
        "SELECT * FROM packet_document WHERE kind = ? ORDER BY version", (kind,)
    ).fetchall()


def packet(env):
    return env.conn.execute("SELECT * FROM application_packet WHERE id = ?", (env.pid,)).fetchone()


class Forms(HTMLParser):
    """Every form's action and its inputs (name -> value; checkboxes only when checked)."""

    def __init__(self):
        super().__init__()
        self.forms: dict[str, dict[str, str]] = {}
        self.buttons: list[dict[str, str]] = []
        self.links: list[str] = []
        self._cur: str | None = None
        self._ta: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = a.get("id") or a.get("action")
            self.forms.setdefault(self._cur, {"__action": a.get("action", "")})
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif self._cur and tag == "input" and a.get("name"):
            if a.get("type") == "checkbox" and "checked" not in a:
                return
            self.forms[self._cur][a["name"]] = a.get("value") or ""
        elif self._cur and tag == "textarea" and a.get("name"):
            self._ta = a["name"]
            self.forms[self._cur][self._ta] = ""
        elif tag == "button":
            self.buttons.append({**a, "in_form": self._cur or ""})

    def handle_data(self, data):
        if self._cur and self._ta:
            self.forms[self._cur][self._ta] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._ta = None
        elif tag == "form":
            self._cur = None


def parse(html: str) -> Forms:
    f = Forms()
    f.feed(html)
    return f


def target(page_html: str, btn: dict[str, str]) -> str:
    """Where a button posts: its own form= form's action (never the editor's)."""
    assert btn.get("form") and "formaction" not in btn, btn
    return parse(page_html).forms[btn["form"]]["__action"]


# ─── buttons and runners ────────────────────────────────────────────────────


def test_page_offers_the_subscription_run_and_no_api_button_when_the_cli_is_there(env, fake_claude):
    page = env.client.get(f"/packet/{env.pid}")
    assert page.status_code == 200
    runners = [b.get("data-runner") for b in parse(page.text).buttons if b.get("data-runner")]
    assert "cli" in runners and "api" not in runners
    assert "Generate resume (subscription)" in page.text
    assert fake_claude.calls(auth=None) == []  # rendering runs nothing


def test_without_the_cli_the_api_button_shows_its_cost_and_needs_a_key(env, monkeypatch):
    page = env.client.get(f"/packet/{env.pid}")
    api = [b for b in parse(page.text).buttons if b.get("data-runner") == "api"]
    assert api and all("disabled" in b for b in api)
    assert "not installed" in page.text and "no ANTHROPIC_API_KEY" in page.text
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    page = env.client.get(f"/packet/{env.pid}")
    api = [b for b in parse(page.text).buttons if b.get("data-runner") == "api"]
    assert api and not any("disabled" in b for b in api)
    assert re.search(r"Generate resume \(API, &asymp; \$0\.\d\d\)", page.text)
    assert all("data-confirm" in b for b in api)


def test_generate_on_the_cli_shows_each_line_beside_its_cited_lines(
    env, fake_claude, claude_stream
):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL, model="claude-haiku-4-5")])
    r = generate(env)
    assert r.status_code == 303 and r.headers["location"].endswith("#resume")
    (d,) = docs(env)
    assert (d["runner"], d["origin"], d["cost_usd"]) == ("cli", "generated", 0.0)
    page = env.client.get(f"/packet/{env.pid}").text
    assert "L7</b> - Wrote Terraform modules used by 3 teams" in page
    assert "unsupported" in page and "claims more (leadership)" in page
    assert "support: partly" in page
    assert "This is true, keep it" in page
    assert "Led with Kubernetes" in page  # change notes
    # Mark ready is disabled while a line is unsupported.
    ready = [b for b in parse(page).buttons if b.get("id") == "mark-ready"]
    assert ready and "disabled" in ready[0]


def test_cli_failure_offers_the_api_and_does_not_call_it(env, fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    fake_claude.set([{"steps": [], "exit": 1, "stderr": "Error: rate limited"}])
    r = generate(env)
    assert r.status_code == 409
    assert "Nothing was saved" in r.text and "rate limited" in r.text
    offer = [b for b in parse(r.text).buttons if b.get("id") == "run-api-resume"]
    assert offer and offer[0].get("value") == "api" and "disabled" not in offer[0]
    assert docs(env) == []
    # The click: the API runs now, with the fake client.
    client = FakeClient([RESUME_OUT, ENTAIL])
    env.app.state.packet_api_client_factory = lambda: client
    r = generate(env, runner="api")
    assert r.status_code == 303
    assert [d["runner"] for d in docs(env)] == ["api"]
    assert client.params[0]["model"] == "claude-opus-5"


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.params: list[dict] = []
        self.messages = self

    def stream(self, **params):
        self.params.append(params)
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.outputs.pop(0)))],
            usage=SimpleNamespace(
                input_tokens=8000,
                output_tokens=3000,
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


def test_runner_off_is_shown_and_turned_back_on(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT, init={"mcp_servers": [{"name": "x"}]})])
    r = generate(env)
    assert r.status_code == 409 and "MCP" in r.text
    page = env.client.get(f"/packet/{env.pid}").text
    assert "CLI runner off." in page and 'id="runner-on"' in page
    r = env.client.post("/apply/runner/on", data={"next": f"/packet/{env.pid}#documents"})
    assert r.status_code == 303 and r.headers["location"] == f"/packet/{env.pid}#documents"
    assert not runner_state.load(env.settings.paths.data_dir).off
    assert "CLI runner off." not in env.client.get(f"/packet/{env.pid}").text


def test_turn_back_on_never_redirects_off_site(env):
    for target in ("//evil.example/x", "https://evil.example/", "/\\evil.example"):
        r = env.client.post("/apply/runner/on", data={"next": target})
        assert r.headers["location"] == "/costs"


def test_paid_extra_usage_waits_for_a_confirmed_click(env, fake_claude, claude_stream):
    fake_claude.set(
        [claude_stream(RESUME_OUT, overage=True), claude_stream(ENTAIL, overage=True, model="h")]
    )
    assert generate(env).status_code == 303
    page = env.client.get(f"/packet/{env.pid}").text
    assert "on paid extra usage" in page
    paid = [b for b in parse(page).buttons if b.get("data-paid") == "1"]
    assert paid and "data-confirm" in paid[0]
    n = len(fake_claude.calls())
    r = generate(env)  # no confirm_paid: refused, nothing run
    assert r.status_code == 409 and len(fake_claude.calls()) == n
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    assert generate(env, confirm_paid="1").status_code == 303
    assert [d["version"] for d in docs(env)] == [1, 2]


# ─── review: confirm, edit, restore, ready ──────────────────────────────────


def _editor(page_html: str, form_id: str = "resume-editor") -> dict[str, str]:
    form = dict(parse(page_html).forms[form_id])
    form.pop("__action")
    return form


def test_confirm_then_mark_ready(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    r = env.client.post(f"/packet/{env.pid}/ready")
    assert r.status_code == 409 and "unsupported" in r.text
    assert packet(env)["status"] == "draft"
    page = env.client.get(f"/packet/{env.pid}").text
    (btn,) = [b for b in parse(page).buttons if b.get("name") == "ckey"]
    doc_id = docs(env)[0]["id"]
    assert target(page, btn) == f"/packet/{env.pid}/doc/{doc_id}/confirm"
    r = env.client.post(target(page, btn), data={"ckey": btn["value"]})
    assert r.status_code == 303
    assert len(docs(env)) == 1  # a confirmation is recorded, not a new version
    assert env.client.post(f"/packet/{env.pid}/ready").status_code == 303
    p = packet(env)
    assert p["status"] == "ready" and p["ready_at"]


def test_edit_saves_a_new_version_rechecks_and_carries_confirmations(
    env, fake_claude, claude_stream
):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    page = env.client.get(f"/packet/{env.pid}").text
    (btn,) = [b for b in parse(page).buttons if b.get("name") == "ckey"]
    env.client.post(target(page, btn), data={"ckey": btn["value"]})
    page = env.client.get(f"/packet/{env.pid}").text
    form = _editor(page)
    assert form["b.0.1.text"] == LED and form["b.0.1.sources"] == "L7"
    # Unchanged "Led ..." keeps its confirmation; the summary is edited with an invented number.
    form["summary.text"] = "Platform engineer who moved 400 services to Kubernetes."
    doc_id = docs(env)[0]["id"]
    r = env.client.post(f"/packet/{env.pid}/doc/{doc_id}/save", data=form)
    assert r.status_code == 303
    v1, v2 = docs(env)
    assert (v2["version"], v2["origin"], v2["parent_id"], v2["runner"]) == (
        2,
        "edited",
        v1["id"],
        None,
    )
    items = {i["key"]: i for i in json.loads(v2["check_report"])["items"]}
    assert items["s0.e0.b1"]["status"] == "confirmed"
    assert items["summary"]["status"] == "unsupported"
    assert any("400" in why for why in items["summary"]["reasons"])
    assert json.loads(v2["doc_json"])["resume"]["sections"][0]["entries"][0]["bullets"][1][
        "sources"
    ] == ["L7"]
    assert packet(env)["resume_doc_id"] == v2["id"]
    # The old version can no longer be edited.
    r = env.client.post(f"/packet/{env.pid}/doc/{doc_id}/save", data=form)
    assert r.status_code == 409 and "newer version" in r.text


def test_new_item_without_a_source_blocks_ready_until_marked_true(env, fake_claude, claude_stream):
    out = json.loads(json.dumps(RESUME_OUT))
    out["resume"]["sections"][0]["entries"][0]["bullets"].pop()  # drop the "Led" bullet
    fake_claude.set([claude_stream(out), claude_stream({"lines": []})])
    generate(env)
    form = _editor(env.client.get(f"/packet/{env.pid}").text)
    form["nb.0.text"] = "Runs a calm on-call rotation"
    env.client.post(f"/packet/{env.pid}/doc/{docs(env)[0]['id']}/save", data=form)
    assert env.client.post(f"/packet/{env.pid}/ready").status_code == 409
    form = _editor(env.client.get(f"/packet/{env.pid}").text)
    form["nb.0.text"] = "Mentors new hires"
    form["nb.0.true"] = "1"
    form["b.0.1.remove"] = "1"  # drop the unsourced line added before
    env.client.post(f"/packet/{env.pid}/doc/{docs(env)[-1]['id']}/save", data=form)
    last = docs(env)[-1]
    bullets = json.loads(last["doc_json"])["resume"]["sections"][0]["entries"][0]["bullets"]
    assert [b["text"] for b in bullets][-1] == "Mentors new hires"
    assert "Runs a calm on-call rotation" not in [b["text"] for b in bullets]
    assert env.client.post(f"/packet/{env.pid}/ready").status_code == 303


def test_restore_an_omitted_line(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    page = env.client.get(f"/packet/{env.pid}").text
    (btn,) = [b for b in parse(page).buttons if b.get("name") == "line"]
    assert btn["value"] == "L8"
    r = env.client.post(target(page, btn), data={"line": "L8"})
    assert r.status_code == 303
    doc = json.loads(docs(env)[-1]["doc_json"])
    bullets = doc["resume"]["sections"][0]["entries"][0]["bullets"]
    assert bullets[-1] == {"text": "Cut deploy time by 35%", "sources": ["L8"]}
    assert doc["resume"]["omitted"] == []


def test_use_base_resume_is_free_and_can_be_marked_ready(env, fake_claude):
    r = env.client.post(f"/packet/{env.pid}/base")
    assert r.status_code == 303
    (d,) = docs(env)
    assert (d["origin"], d["runner"], d["cost_usd"]) == ("base", None, None)
    assert d["body_md"].startswith("Synthetic Person")
    assert env.client.post(f"/packet/{env.pid}/ready").status_code == 303
    assert fake_claude.calls(auth=None) == []
    assert env.conn.execute("SELECT count(*) FROM llm_spend").fetchone()[0] == 0


def test_versions_are_listed_and_an_old_one_is_read_only(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    env.client.post(f"/packet/{env.pid}/base")
    v1, v2 = docs(env)
    page = env.client.get(f"/packet/{env.pid}?resume={v1['id']}").text
    assert f'href="/packet/{env.pid}?resume={v2["id"]}#resume"' in page
    assert "back to the current version" in page
    assert 'id="save-resume"' not in page  # not editable


# ─── letter and drafts ──────────────────────────────────────────────────────


def test_letter_on_request_with_notes_returns_a_ready_packet_to_draft(
    env, fake_claude, claude_stream
):
    page = env.client.get(f"/packet/{env.pid}").text
    assert "Add cover letter" not in page  # needs a resume version first
    env.client.post(f"/packet/{env.pid}/base")
    env.client.post(f"/packet/{env.pid}/ready")
    assert packet(env)["status"] == "ready"
    r = env.client.post(
        f"/packet/{env.pid}/notes", data={"notes": "Their docs taught me Terraform."}
    )
    assert r.status_code == 303
    page = env.client.get(f"/packet/{env.pid}").text
    assert "Add cover letter (subscription)" in page
    fake_claude.set([claude_stream(LETTER_OUT), claude_stream({"lines": []})])
    r = generate(env, kind="cover_letter")
    assert r.status_code == 303 and r.headers["location"].endswith("#letter")
    p = packet(env)
    assert p["status"] == "draft" and p["cover_doc_id"] is not None
    assert "N1: Their docs taught me Terraform." in fake_claude.calls()[0]["stdin"]
    assert len(docs(env, "resume")) == 1
    assert env.client.post(f"/packet/{env.pid}/ready").status_code == 303


def test_four_note_lines_are_refused(env):
    r = env.client.post(f"/packet/{env.pid}/notes", data={"notes": "a\nb\nc\nd"})
    assert r.status_code == 409 and "at most 3" in r.text


@pytest.mark.parametrize("q", ["What is your desired salary?", "Race/Ethnicity", "Veteran status"])
def test_never_store_question_is_yours(env, fake_claude, q):
    r = generate(env, kind="question_draft", question=q, facts="something")
    assert r.status_code == 409 and "This one is yours" in r.text
    assert fake_claude.calls(auth=None) == []
    assert env.conn.execute("SELECT count(*) FROM packet_answer").fetchone()[0] == 0


def test_numeric_question_shows_evidence_lines(env, fake_claude):
    r = generate(env, kind="question_draft", question="Years of experience with Terraform?")
    assert r.status_code == 200
    assert "L7</b> - Wrote Terraform modules used by 3 teams" in r.text
    assert "never a computed number" in r.text
    assert fake_claude.calls(auth=None) == []


def test_behavioral_draft_saves_story_facts_and_is_checked(env, fake_claude, claude_stream):
    q = "Tell us about a time you fixed a failed deploy."
    r = generate(env, kind="question_draft", question=q)
    assert r.status_code == 409 and "story" in r.text
    out = {
        "draft": {
            "sentences": [
                {
                    "text": "When a 2am upgrade failed, I rolled it back.",
                    "sources": ["S1"],
                    "posting_quotes": [],
                },
                {"text": "I then cut deploy time by 50%.", "sources": ["L8"], "posting_quotes": []},
            ]
        }
    }
    fake_claude.set([claude_stream(out), claude_stream({"lines": []})])
    r = generate(
        env, kind="question_draft", question=q, facts="A 2am upgrade failed and I rolled it back."
    )
    assert r.status_code == 303
    (d,) = docs(env, "question_draft")
    items = json.loads(d["check_report"])["items"]
    assert [i["status"] for i in items] == ["pass", "unsupported"]
    page = env.client.get(f"/packet/{env.pid}").text
    assert "Tell us about a time you fixed a failed deploy." in page
    assert "S1</b> A 2am upgrade failed and I rolled it back." in page
    # 019561b (2): an unsupported sentence gates Copy, as Export is gated.
    draft = page.split(f'id="draft-{d["id"]}"', 1)[1].split('class="editor"', 1)[0]
    assert "copy-btn" not in draft and "1 unsupported sentence" in draft
    (btn,) = [
        b
        for b in parse(page).buttons
        if b.get("name") == "ckey" and f"/doc/{d['id']}/" in target(page, b)
    ]
    env.client.post(f"/packet/{env.pid}/doc/{d['id']}/confirm", data={"ckey": btn["value"]})
    page = env.client.get(f"/packet/{env.pid}").text
    draft = page.split(f'id="draft-{d["id"]}"', 1)[1].split('class="editor"', 1)[0]
    assert 'class="act copy-btn"' in draft and "copy-gate" not in draft


# ─── page hygiene ───────────────────────────────────────────────────────────


def test_cross_site_generate_is_refused(env, fake_claude):
    r = env.client.post(
        f"/packet/{env.pid}/generate",
        data={"kind": "resume", "runner": "cli"},
        headers={
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
            "Host": "127.0.0.1:8808",
        },
    )
    assert r.status_code == 403
    assert fake_claude.calls(auth=None) == []


def test_packet_page_is_not_cached_and_every_link_resolves(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    env.client.post(f"/packet/{env.pid}/base")
    page = env.client.get(f"/packet/{env.pid}")
    assert page.headers["cache-control"] == "no-store"
    links = {h.split("#")[0] for h in parse(page.text).links if h.startswith("/")}
    assert links
    for href in sorted(links):
        assert env.client.get(href).status_code == 200, href
    for f in parse(page.text).forms.values():
        assert f["__action"].startswith("/"), f


# ─── review round ───────────────────────────────────────────────────────────


def test_turn_back_on_keeps_the_paid_hold_and_the_page_shows_both(env, fake_claude, claude_stream):
    d = env.settings.paths.data_dir
    runner_state.set_overage(d)
    runner_state.turn_off(d, "auth check failed")
    page = env.client.get(f"/packet/{env.pid}").text
    assert 'id="runner-off"' in page and 'id="runner-overage"' in page
    env.client.post("/apply/runner/on", data={"next": f"/packet/{env.pid}"})
    assert runner_state.load(d).overage
    page = env.client.get(f"/packet/{env.pid}").text
    cli = [b for b in parse(page).buttons if b.get("data-runner") == "cli"]
    assert cli and all(b.get("data-paid") == "1" and b.get("data-confirm") for b in cli)
    fake_claude.set([claude_stream(RESUME_OUT)])
    r = generate(env)  # a plain click without confirm_paid is refused, nothing runs
    assert r.status_code == 409 and fake_claude.calls(auth=None) == []
    r = env.client.post("/apply/runner/clear-overage", data={"next": f"/packet/{env.pid}"})
    assert r.status_code == 303 and not runner_state.load(d).overage
    assert (
        env.client.post("/apply/runner/clear-overage", data={"next": "//evil"}).headers["location"]
        == "/costs"
    )


def test_confirm_and_restore_post_their_own_forms_and_save_is_the_default(
    env, fake_claude, claude_stream
):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    page = env.client.get(f"/packet/{env.pid}").text
    f = parse(page)
    editor_buttons = [b for b in f.buttons if b["in_form"] == "resume-editor"]
    # The first submit button of the editor, the one Enter presses, is a Save with no form=.
    first = editor_buttons[0]
    assert "default-save" in first.get("class", "") and "form" not in first
    for b in editor_buttons:
        if b.get("name") in ("ckey", "line"):
            assert b.get("form", "").startswith(("confirm-", "restore-")), b


def test_add_cover_letter_saves_the_notes_typed_beside_it(env, fake_claude, claude_stream):
    env.client.post(f"/packet/{env.pid}/base")
    fake_claude.set([claude_stream(LETTER_OUT), claude_stream({"lines": []})])
    r = generate(env, kind="cover_letter", notes="Their docs taught me Terraform.")
    assert r.status_code == 303
    assert "N1: Their docs taught me Terraform." in fake_claude.calls()[0]["stdin"]
    row = env.conn.execute(
        "SELECT value FROM packet_answer WHERE field_key = 'notes:employer'"
    ).fetchone()
    assert row[0] == "Their docs taught me Terraform."


def test_api_estimate_is_for_this_packets_request(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic")
    small = env.client.get(f"/packet/{env.pid}").text
    env.conn.execute(
        "UPDATE job SET description_text = ? WHERE id = (SELECT max(id) FROM job)",
        (POSTING + "\n" + "Long federal announcement text. " * 4000,),
    )
    big = env.client.get(f"/packet/{env.pid}").text

    def est(html):
        return float(re.search(r"Generate resume \(API, &asymp; \$(\d+\.\d\d)\)", html).group(1))

    assert est(big) > est(small) + 0.1


def test_an_edit_never_confirms_a_line_nobody_confirmed(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    form = _editor(env.client.get(f"/packet/{env.pid}").text)
    form["summary.text"] = "Platform engineer who moves services onto Kubernetes."
    env.client.post(f"/packet/{env.pid}/doc/{docs(env)[0]['id']}/save", data=form)
    v2 = docs(env)[-1]
    report = json.loads(v2["check_report"])
    items = {i["key"]: i for i in report["items"]}
    assert items["s0.e0.b1"]["status"] == "unsupported"
    assert report["confirmed"] == []
    # The unchanged line keeps its advisory verdict across the edit.
    assert items["s0.e0.b1"]["entail"] == "partly"
