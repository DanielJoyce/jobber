"""Packet answers, reuse, /prefs offers, checklists and the already-applied warning
(specs/017 phase 1d), through the routes. Synthetic data; nothing here calls a model.
"""

from __future__ import annotations

import json

from ruamel.yaml import YAML
from test_packet_pages import (  # noqa: F401
    GH,
    ISO,
    NOW,
    _Links,
    client,
    conn,
    db_path,
    new_packet,
    packet_group,
    pages,
    pdir,
)

from jobhunter.apply import answers

WHY = "Why do you want to work here?"


def saved(conn, pid=None):  # noqa: F811
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT packet_id, field_key, label, value, source FROM packet_answer "
            "WHERE field_key LIKE 'q:%'"
        )
    ]
    return [r for r in rows if pid is None or r[0] == pid]


def add_draft(conn, pid, question, text):  # noqa: F811
    from jobhunter.apply import labels

    doc = {
        "question": question,
        "qkind": "why_us",
        "draft": {"sentences": [{"text": text, "sources": [], "posting_quotes": []}]},
        "context": {"lines": {}},
    }
    cur = conn.execute(
        "INSERT INTO packet_document (packet_id, kind, version, question_key, origin, doc_json, "
        "body_md, check_report, created_at) VALUES (?, 'question_draft', 1, ?, 'generated', ?, "
        "?, ?, ?)",
        (pid, labels.question_key(question), json.dumps(doc), text, '{"ok": true}', ISO),
    )
    return cur.lastrowid


# ─── Save as answer ─────────────────────────────────────────────────────────


def test_save_your_own_answer_keeps_it_on_this_packet_only(client, conn, pdir):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    before = (pdir / "preferences.yaml").read_text()
    r = client.post(f"/packet/{pid}/answers", data={"question": WHY, "value": "Synthetic reasons."})
    assert r.status_code == 303 and r.headers["location"].endswith("#answers")
    key = "q:why do you want to work here"
    assert saved(conn) == [(pid, key, WHY, "Synthetic reasons.", "user")]
    assert (pdir / "preferences.yaml").read_text() == before  # nothing written to /prefs
    page = client.get(r.headers["location"]).text
    assert "Synthetic reasons." in page and "Promote to /prefs" in page


def test_save_as_answer_refuses_a_never_store_question(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    for q in ("Desired salary", "Are you a protected veteran?", "Signature"):
        r = client.post(f"/packet/{pid}/answers", data={"question": q, "value": "x"})
        assert r.status_code == 409
        assert "This one is yours" in r.text
    assert saved(conn) == []


def test_save_a_draft_as_an_answer(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    doc_id = add_draft(conn, pid, WHY, "I like synthetic widgets.")
    assert "Save as answer" in client.get(f"/packet/{pid}").text
    r = client.post(f"/packet/{pid}/answers", data={"doc_id": str(doc_id)})
    assert r.status_code == 303
    assert saved(conn) == [
        (pid, "q:why do you want to work here", WHY, "I like synthetic widgets.", "draft")
    ]
    other = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    assert client.post(f"/packet/{other}/answers", data={"doc_id": str(doc_id)}).status_code == 404


# ─── reuse ──────────────────────────────────────────────────────────────────


def test_a_later_packet_offers_earlier_answers_to_the_same_question(client, conn):  # noqa: F811
    first = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    client.post(f"/packet/{first}/answers", data={"question": WHY, "value": "Earlier words."})
    later = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    assert "Earlier words." not in client.get(f"/packet/{later}").text  # no such question yet
    add_draft(conn, later, "why do you want to work here", "A new draft.")
    page = client.get(f"/packet/{later}").text
    assert "Your earlier answers" in page and "Earlier words." in page
    assert f'href="/packet/{first}#answers"' in page
    # Use on this packet copies it here, as your text.
    r = client.post(f"/packet/{later}/answers", data={"question": WHY, "value": "Earlier words."})
    assert r.status_code == 303
    assert (later, "q:why do you want to work here", WHY, "Earlier words.", "user") in saved(conn)


# ─── Promote to /prefs ──────────────────────────────────────────────────────


def test_promote_copies_into_prefs_answers_and_replaces_the_same_question(
    client,  # noqa: F811
    conn,  # noqa: F811
    pdir,  # noqa: F811
):
    prefs = pdir / "preferences.yaml"
    prefs.write_text(prefs.read_text() + "# my note\nanswers:\n  notice_period: two weeks\n")
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    client.post(f"/packet/{pid}/answers", data={"question": WHY, "value": "First words."})
    (aid,) = [r[0] for r in conn.execute("SELECT id FROM packet_answer WHERE field_key LIKE 'q:%'")]
    r = client.post(f"/packet/{pid}/answers/{aid}/promote")
    assert r.status_code == 303 and "answer=promoted" in r.headers["location"]
    assert "now in Application answers" in client.get(r.headers["location"]).text
    client.post(f"/packet/{pid}/answers", data={"question": WHY + " ", "value": "Better words."})
    client.post(f"/packet/{pid}/answers/{aid}/promote")
    text = (pdir / "preferences.yaml").read_text()
    data = YAML(typ="safe").load(text)
    assert data["answers"]["custom"] == [{"question": WHY, "answer": "Better words."}]
    assert data["answers"]["notice_period"] == "two weeks"
    assert "# my note" in text  # the 014 round-trip writer keeps comments
    # every packet now offers it, the matching question first
    other = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    assert "Better words." in client.get(f"/packet/{other}").text


def test_promote_refuses_a_never_store_answer_and_writes_nothing(client, conn, pdir):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    # A row that predates the check (written straight to the table, as an old build might).
    cur = conn.execute(
        "INSERT INTO packet_answer (packet_id, field_key, label, value, source) "
        "VALUES (?, 'q:expected ctc', 'Expected CTC', '30 lakhs', 'user')",
        (pid,),
    )
    before = (pdir / "preferences.yaml").read_text()
    r = client.post(f"/packet/{pid}/answers/{cur.lastrowid}/promote")
    assert r.status_code == 409 and "This one is yours" in r.text
    assert (pdir / "preferences.yaml").read_text() == before
    message = r.text.split('id="answers-message">')[1].split("</div>")[0]
    assert "Expected CTC" in message and "30 lakhs" not in message  # label, never the value


def test_promote_of_another_packets_answer_is_404(client, conn):  # noqa: F811
    a = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    b = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    client.post(f"/packet/{a}/answers", data={"question": WHY, "value": "x"})
    (aid,) = [r[0] for r in conn.execute("SELECT id FROM packet_answer WHERE field_key LIKE 'q:%'")]
    assert client.post(f"/packet/{b}/answers/{aid}/promote").status_code == 404


# ─── /prefs answers on the packet page ──────────────────────────────────────


def test_packet_offers_prefs_answers_and_never_a_dropped_never_store_one(client, pdir):  # noqa: F811
    (pdir / "preferences.yaml").write_text(
        (pdir / "preferences.yaml").read_text() + "answers:\n"
        "  links:\n    github: https://example.com/synthetic-gh\n"
        "  notice_period: two weeks\n"
        "  work_authorization: true\n"
        "  custom:\n"
        "    - question: Desired salary\n      answer: SECRET-NUMBER\n"
        f"    - question: {WHY}\n      answer: Template words.\n"
    )
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    page = client.get(f"/packet/{pid}").text
    assert 'data-copy="https://example.com/synthetic-gh"' in page
    assert 'data-copy="two weeks"' in page and 'data-copy="Yes"' in page
    assert 'data-copy="Template words."' in page
    assert "SECRET-NUMBER" not in page
    assert 'id="answers-warnings"' in page and "never-store list" in page


# ─── checklists ─────────────────────────────────────────────────────────────


def test_checklist_follows_the_board(client, conn):  # noqa: F811
    gh = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    page = client.get(f"/packet/{gh}").text
    assert 'data-board="any"' in page and "Do not re-upload the file" in page
    wd = new_packet(
        client,
        url="https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Denver-CO/Analyst_R-1",
        employer="Acme Two",
        title="Analyst",
    )
    page = client.get(f"/packet/{wd}").text
    assert 'data-board="workday"' in page and "splits or merges jobs" in page
    ng = new_packet(
        client,
        url="https://www.governmentjobs.com/careers/acme/jobs/4567890/analyst",
        employer="Acme Three",
        title="Analyst",
    )
    assert 'data-board="neogov"' in client.get(f"/packet/{ng}").text


def test_usajobs_checklist_shows_relevant_lines_never_a_level(client, conn, pdir):  # noqa: F811
    (pdir / "resume.md").write_text(
        "Synthetic Person\n- Planned the migration of payroll services\n- Baked bread\n"
    )
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('usajobs', 'A', 'USAJOBS', 'usajobs', 'api', 'https://example.com', 'enabled')"
    )
    conn.execute(
        "UPDATE job SET source_key = 'usajobs' WHERE id = "
        "(SELECT canonical_job_id FROM job_group WHERE id = ?)",
        (gid,),
    )
    page = client.get(f"/packet/{pid}").text
    assert 'data-board="usajobs"' in page and 'id="evidence-form"' in page
    r = client.post(
        f"/packet/{pid}/evidence",
        data={"statement": "I have planned migrations of payroll systems."},
    )
    assert r.status_code == 200
    ev = r.text.split('id="evidence"')[1].split("</div>")[0]
    assert "Planned the migration of payroll services" in ev and "Baked bread" not in ev


# ─── already applied ────────────────────────────────────────────────────────


def test_warning_before_generate_when_already_applied_to_employer_and_title(
    client,  # noqa: F811
    conn,  # noqa: F811
):
    first = new_packet(client, url=GH, employer="Acme Inc.", title="Systems Engineer")
    gid = packet_group(conn, first)
    client.post(f"/job/{gid}/applied?choice=yes")
    again = new_packet(
        client,
        text="Synthetic posting text.",
        employer="ACME",
        title="Systems engineer",
        force="1",
    )
    page = client.get(f"/packet/{again}").text
    warn = page.index('id="already-applied"')
    assert warn < page.index('id="documents"')  # before Generate
    app_id = conn.execute("SELECT id FROM application WHERE job_group_id = ?", (gid,)).fetchone()[0]
    assert f'href="/pipeline/{app_id}"' in page
    assert client.get(f"/pipeline/{app_id}").status_code == 200
    # the first packet itself carries no warning about its own application
    assert 'id="already-applied"' not in client.get(f"/packet/{first}").text


# ─── links and forms on the new section ─────────────────────────────────────


def test_every_link_and_form_in_the_answers_section_resolves(client, conn, pdir):  # noqa: F811
    first = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    client.post(f"/packet/{first}/answers", data={"question": WHY, "value": "Words."})
    later = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    add_draft(conn, later, WHY, "Draft words.")
    html = client.get(f"/packet/{later}").text
    section = html[html.index('id="answers"') :]
    p = _Links()
    p.feed(section)
    assert p.hrefs and p.actions
    for href in p.hrefs:
        assert client.get(href.split("#")[0]).status_code == 200, href
    for action in p.actions:
        assert client.post(action, data={}).status_code not in (404, 405), action


def test_save_packet_answer_is_refused_at_the_function_too(conn, client):  # noqa: F811
    # The one writer of packet_answer raises on a never-store label, whatever the caller.
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    for key, label in (("q:gender", "Gender"), ("q:x", "Date of birth"), ("story:race", "x")):
        try:
            answers.save_packet_answer(conn, pid, key, label, "v")
        except answers.NeverStore as exc:
            assert "This one is yours" in str(exc)
        else:
            raise AssertionError(f"{label} was stored")
    assert conn.execute("SELECT count(*) FROM packet_answer").fetchone()[0] == 0
