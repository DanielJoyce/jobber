"""Closing the loop (specs/017 "Closing the loop"), through the routes, and /followups.

Every path that records 'applied' must attach the ready packet: the "Did you apply?" Yes (which
does not go through tracking.add_event), a mail proposal accept, a manual event and a pipeline
move. Each test reads ``application.resume_version`` and the ``attachment`` rows back.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest
from test_packet_documents import env  # noqa: F401
from test_packet_export import FakeRenderer
from test_packet_pages import (  # noqa: F401
    GH,
    ISO,
    NOW,
    client,
    conn,
    db_path,
    new_packet,
    packet_group,
    pages,
    pdir,
)

from jobhunter.apply import packets
from jobhunter.console import tracking
from jobhunter.xdg import data_home


@pytest.fixture
def rendered():
    """Rendered packet files in the console's data dir (the default, below the test's HOME),
    stored as absolute paths the way rows written before C14 hold them."""
    d = data_home() / "packets"
    d.mkdir(parents=True)
    resume, letter = d / "Synthetic-Person-Resume.pdf", d / "Synthetic-Person-Cover-Letter.pdf"
    resume.write_bytes(b"%PDF-synthetic")
    letter.write_bytes(b"%PDF-synthetic")
    return resume, letter


def make_ready(conn, pid, rendered=None, *, letter=True, ready_at=ISO, version=2):  # noqa: F811
    """A packet with a checked resume (and letter) version, marked ready."""
    ids = {}
    for kind, path in (("resume", rendered[0] if rendered else None),) + (
        (("cover_letter", rendered[1] if rendered else None),) if letter else ()
    ):
        doc = {"base": True} if kind == "resume" else {"cover_letter": {"paragraphs": []}}
        cur = conn.execute(
            "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
            "check_report, rendered_path, created_at) "
            "VALUES (?, ?, ?, 'edited', ?, 'x', ?, ?, ?)",
            (
                pid,
                kind,
                version,
                json.dumps(doc),
                json.dumps({"ok": True, "items": []}),
                path and str(path),
                ISO,
            ),
        )
        ids[kind] = cur.lastrowid
    conn.execute(
        "UPDATE application_packet SET status = 'ready', ready_at = ?, resume_doc_id = ?, "
        "cover_doc_id = ? WHERE id = ?",
        (ready_at, ids["resume"], ids.get("cover_letter"), pid),
    )


def app_of(conn, pid):  # noqa: F811
    return conn.execute(
        "SELECT a.* FROM application a JOIN application_packet p ON p.application_id = a.id "
        "WHERE p.id = ?",
        (pid,),
    ).fetchone()


def attachments(conn, app_id):  # noqa: F811
    return sorted(
        (r["kind"], r["path"])
        for r in conn.execute("SELECT * FROM attachment WHERE application_id = ?", (app_id,))
    )


def assert_attached(conn, pid, rendered):  # noqa: F811
    """Attached, and stored relative to the data dir (specs/018 D18), whatever the row held."""
    app = app_of(conn, pid)
    assert app["status"] == "applied"
    assert app["resume_version"] == f"packet:{pid}/resume/v2"
    assert app["cover_letter_path"] == f"packets/{rendered[1].name}"
    assert attachments(conn, app["id"]) == sorted(
        [("resume", f"packets/{rendered[0].name}"), ("cover_letter", f"packets/{rendered[1].name}")]
    )


def test_did_you_apply_yes_attaches_the_ready_packet(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    gid = packet_group(conn, pid)
    r = client.post(f"/job/{gid}/applied?choice=yes")
    assert r.status_code == 200
    assert_attached(conn, pid, rendered)


def test_mail_proposal_accept_attaches_the_ready_packet(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    app = app_of(conn, pid)
    cur = conn.execute(
        "INSERT INTO mail_proposal (gmail_message_id, received_at, kind, proposed_action, "
        "application_id, job_group_id, proposed_status, confidence, evidence, created_at) "
        "VALUES ('m-1', ?, 'confirmation', 'add_event', ?, ?, 'applied', 0.9, ?, ?)",
        (ISO, app["id"], app["job_group_id"], json.dumps({"subject": "Thanks"}), ISO),
    )
    r = client.post(f"/proposals/{cur.lastrowid}/accept")
    assert r.status_code in (200, 303), r.text
    assert_attached(conn, pid, rendered)


def test_manual_event_attaches_the_ready_packet(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    app = app_of(conn, pid)
    r = client.post(f"/pipeline/{app['id']}/event", data={"status": "applied", "note": "sent"})
    assert r.status_code in (200, 303)
    assert_attached(conn, pid, rendered)


def test_pipeline_move_to_applied_attaches_it_too(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    app = app_of(conn, pid)
    client.post(f"/pipeline/{app['id']}/move?to=applied")
    assert_attached(conn, pid, rendered)


@pytest.fixture
def secret(tmp_path):
    """A file outside the data dir that must never be copied into it."""
    f = tmp_path / ".ssh" / "id_rsa"
    f.parent.mkdir()
    f.write_bytes(b"PRIVATE KEY")
    return f


def escape_shapes(secret):
    """rendered_path values that leave the data dir: absolute elsewhere, '..', a symlink."""
    link_dir = data_home() / "packets" / "9"
    link_dir.mkdir(parents=True, exist_ok=True)
    link = link_dir / "resume-v2.pdf"
    if not link.is_symlink():
        link.symlink_to(secret)
    dots = os.path.relpath(secret, data_home())
    assert dots.startswith("..")
    return {"absolute": str(secret), "dotdot": dots, "symlink": "packets/9/resume-v2.pdf"}


@pytest.mark.parametrize("shape", ["absolute", "dotdot", "symlink"])
def test_a_rendered_path_outside_the_data_dir_is_refused_not_copied(
    client,  # noqa: F811
    conn,  # noqa: F811
    secret,
    shape,
):
    """Review of C14: a tampered rendered_path (or a symlink under packets/) is never
    followed; 'applied' is still recorded and nothing is copied into the data dir."""
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None)
    conn.execute(
        "UPDATE packet_document SET rendered_path = ? WHERE packet_id = ?",
        (escape_shapes(secret)[shape], pid),
    )
    r = client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    assert r.status_code == 200
    app = app_of(conn, pid)
    assert app["status"] == "applied" and app["resume_version"] == f"packet:{pid}/resume/v2"
    assert attachments(conn, app["id"]) == [] and not app["cover_letter_path"]
    assert not (data_home() / "attachments").exists()


def test_attach_late_export_refuses_a_dotdot_rendered_path(client, conn, secret):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None)
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    conn.execute(
        "UPDATE packet_document SET rendered_path = ? WHERE packet_id = ? AND kind = 'resume'",
        (escape_shapes(secret)["dotdot"], pid),
    )
    assert packets.attach_late_export(conn, pid, data_home()) == 0
    assert attachments(conn, app_of(conn, pid)["id"]) == []
    assert not (data_home() / "attachments").exists()


def test_a_legacy_absolute_row_for_the_same_file_is_not_duplicated(
    client,  # noqa: F811
    conn,  # noqa: F811
    rendered,
):
    """Sent before C14 (absolute row), exported again after: still one row per file."""
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None)
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    app_id = app_of(conn, pid)["id"]
    conn.execute(
        "INSERT INTO attachment (application_id, kind, path, added_at) VALUES (?, 'resume', ?, ?)",
        (app_id, os.path.realpath(rendered[0]), ISO),
    )
    conn.execute(
        "UPDATE packet_document SET rendered_path = ? WHERE packet_id = ? AND kind = 'resume'",
        (f"packets/{rendered[0].name}", pid),
    )
    packets.attach_late_export(conn, pid, data_home())
    assert len(attachments(conn, app_id)) == 1


def test_a_draft_packet_attaches_nothing(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    client.post(f"/job/{gid}/applied?choice=yes")
    app = app_of(conn, pid)
    assert app["status"] == "applied" and app["resume_version"] is None
    assert attachments(conn, app["id"]) == []


def test_without_rendered_files_the_version_is_still_recorded(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None, letter=False)
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    app = app_of(conn, pid)
    assert app["resume_version"] == f"packet:{pid}/resume/v2"
    assert attachments(conn, app["id"]) == []


def test_files_exported_after_applied_are_attached_then(client, conn, tmp_path):  # noqa: F811
    """950d5eb (6): marked applied first, exported later: the sent version's files are
    attached on export. A newer version exported afterwards is not what was sent."""
    from jobhunter.apply import export

    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None)
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    app = app_of(conn, pid)
    assert app["resume_version"] == f"packet:{pid}/resume/v2"
    assert attachments(conn, app["id"]) == []
    data = tmp_path / "data"
    for kind in ("resume", "cover_letter"):
        exp = export.current_export(conn, pid, kind)
        export.write_export(conn, data, pid, exp, FakeRenderer())
    d = f"packets/{pid}"
    assert attachments(conn, app["id"]) == sorted(
        [("resume", f"{d}/resume-v2.pdf"), ("cover_letter", f"{d}/cover-letter-v2.pdf")]
    )
    assert app_of(conn, pid)["cover_letter_path"] == f"{d}/cover-letter-v2.pdf"
    # A v3 made after applying (the packet goes back to draft) is never attached.
    cur = conn.execute(
        "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
        "check_report, created_at) VALUES (?, 'resume', 3, 'edited', ?, 'y', ?, ?)",
        (pid, json.dumps({"base": True}), json.dumps({"ok": True, "items": []}), ISO),
    )
    conn.execute(
        "UPDATE application_packet SET resume_doc_id = ?, status = 'draft' WHERE id = ?",
        (cur.lastrowid, pid),
    )
    export.write_export(conn, data, pid, export.current_export(conn, pid, "resume"), FakeRenderer())
    assert len(attachments(conn, app["id"])) == 2


def test_a_letter_written_after_applying_is_not_attached(client, conn, tmp_path):  # noqa: F811
    """Review of 950d5eb (6): letter v3 written after Yes, the packet marked ready again with
    the same resume, then exported: v3 was never sent, so it is not attached."""
    from jobhunter.apply import export

    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None)
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    app = app_of(conn, pid)
    later = (NOW + timedelta(days=1)).isoformat()
    cur = conn.execute(
        "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
        "check_report, created_at) VALUES (?, 'cover_letter', 3, 'edited', ?, 'z', ?, ?)",
        (
            pid,
            json.dumps({"cover_letter": {"paragraphs": []}}),
            json.dumps({"ok": True, "items": []}),
            later,
        ),
    )
    conn.execute(
        "UPDATE application_packet SET cover_doc_id = ?, status = 'ready' WHERE id = ?",
        (cur.lastrowid, pid),
    )
    data = tmp_path / "data"
    for kind in ("resume", "cover_letter"):
        export.write_export(conn, data, pid, export.current_export(conn, pid, kind), FakeRenderer())
    assert attachments(conn, app["id"]) == [("resume", f"packets/{pid}/resume-v2.pdf")]
    assert not app_of(conn, pid)["cover_letter_path"]


def test_attach_is_once_and_keeps_your_own_resume_version(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    app = app_of(conn, pid)
    conn.execute("UPDATE application SET resume_version = 'my-own-v7' WHERE id = ?", (app["id"],))
    client.post(f"/pipeline/{app['id']}/event", data={"status": "applied"})
    client.post(f"/pipeline/{app['id']}/event", data={"status": "applied", "note": "again"})
    after = app_of(conn, pid)
    assert after["resume_version"] == "my-own-v7"
    assert len(attachments(conn, app["id"])) == 2  # no duplicates on the second event
    # once a packet ref is recorded, a later applied event changes nothing
    conn.execute("UPDATE application SET resume_version = NULL WHERE id = ?", (app["id"],))
    assert packets.attach_sent_packet(conn, app["id"]) is True
    assert packets.attach_sent_packet(conn, app["id"]) is False


def test_a_file_inside_the_tracked_tree_is_not_attached(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    tracked = tracking.REPO_ROOT / "pyproject.toml"
    make_ready(conn, pid, (tracked, tracked))
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    app = app_of(conn, pid)
    assert app["resume_version"] == f"packet:{pid}/resume/v2"
    assert attachments(conn, app["id"]) == []


# ─── /followups: ready, not applied? ────────────────────────────────────────


def test_ready_for_a_week_without_applying_shows_on_followups(client, conn):  # noqa: F811
    old = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    fresh = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    make_ready(conn, old, None, ready_at=(NOW - timedelta(days=8)).isoformat())
    make_ready(conn, fresh, None, ready_at=(NOW - timedelta(days=6)).isoformat())
    page = client.get("/followups").text
    app_old, app_fresh = app_of(conn, old)["id"], app_of(conn, fresh)["id"]
    assert f'id="fu-packet-{app_old}"' in page and "not applied?" in page
    assert f'href="/packet/{old}"' in page
    assert f'id="fu-packet-{app_fresh}"' not in page
    assert client.get(f"/packet/{old}").status_code == 200
    # "I applied" records the event (and attaches the packet); the item goes away.
    r = client.post(f"/followups/{app_old}/applied")
    assert r.status_code == 303
    assert app_of(conn, old)["resume_version"] == f"packet:{old}/resume/v2"
    assert f'id="fu-packet-{app_old}"' not in client.get("/followups").text


def test_an_applied_or_closed_application_is_not_nagged(client, conn):  # noqa: F811
    a = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    b = new_packet(client, text="Synthetic posting text.", employer="Beta", title="Analyst")
    for pid in (a, b):
        make_ready(conn, pid, None, ready_at=(NOW - timedelta(days=30)).isoformat())
    tracking.add_event(conn, app_of(conn, a)["id"], "applied", "sent", NOW - timedelta(days=20))
    tracking.add_event(conn, app_of(conn, b)["id"], "withdrawn", None, NOW)
    items = [i for i in tracking.followups(conn, NOW) if i.kind == "packet"]
    assert items == []


def test_ready_not_applied_skips_applications_already_past_applied(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, None, ready_at=(NOW - timedelta(days=30)).isoformat())
    app_id = app_of(conn, pid)["id"]
    client.post(f"/pipeline/{app_id}/move?to=screening")  # dragged straight past applied
    assert not [i for i in tracking.followups(conn, NOW) if i.kind == "packet"]
    # and the button never moves it back
    client.post(f"/followups/{app_id}/applied")
    assert app_of(conn, pid)["status"] == "screening"


def test_only_a_ready_packet_is_attached(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    conn.execute("UPDATE application_packet SET status = 'draft' WHERE id = ?", (pid,))
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    app = app_of(conn, pid)
    assert app["resume_version"] is None and attachments(conn, app["id"]) == []


def test_your_own_cover_letter_path_is_kept(client, conn, rendered):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    make_ready(conn, pid, rendered)
    app = app_of(conn, pid)
    conn.execute(
        "UPDATE application SET cover_letter_path = '/elsewhere/mine.pdf' WHERE id = ?",
        (app["id"],),
    )
    client.post(f"/job/{packet_group(conn, pid)}/applied?choice=yes")
    after = app_of(conn, pid)
    assert after["cover_letter_path"] == "/elsewhere/mine.pdf"
    assert after["resume_version"] == f"packet:{pid}/resume/v2"


# ─── with phase 1c's real export (rendered_path relative to the data dir) ───


def test_did_you_apply_attaches_1c_export_resolved_against_the_data_dir(
    env,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    env.app.state.packet_pdf_renderer = FakeRenderer()
    monkeypatch.chdir(tmp_path.parent)  # the working directory is never the data dir
    c = env.client
    assert c.post(f"/packet/{env.pid}/base").status_code == 303
    assert c.post(f"/packet/{env.pid}/ready").status_code == 303
    assert c.post(f"/packet/{env.pid}/export", data={"kind": "resume"}).status_code == 303
    stored = env.conn.execute(
        "SELECT rendered_path FROM packet_document WHERE packet_id = ? AND kind = 'resume'",
        (env.pid,),
    ).fetchone()[0]
    assert stored == f"packets/{env.pid}/resume-v1.pdf"  # what 1c writes: relative
    gid, app_id = env.conn.execute(
        "SELECT a.job_group_id, a.id FROM application a JOIN application_packet p "
        "ON p.application_id = a.id WHERE p.id = ?",
        (env.pid,),
    ).fetchone()
    assert c.post(f"/job/{gid}/applied?choice=yes").status_code == 200
    assert attachments(env.conn, app_id) == [("resume", stored)]  # relative, as 1c wrote it
    assert (env.settings.paths.data_dir / stored).is_file()
    assert app_of(env.conn, env.pid)["resume_version"] == f"packet:{env.pid}/resume/v1"
