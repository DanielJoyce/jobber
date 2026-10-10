"""Closing the loop (specs/017 "Closing the loop"), through the routes, and /followups.

Every path that records 'applied' must attach the ready packet: the "Did you apply?" Yes (which
does not go through tracking.add_event), a mail proposal accept, a manual event and a pipeline
move. Each test reads ``application.resume_version`` and the ``attachment`` rows back.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
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


@pytest.fixture
def rendered(tmp_path):
    """Rendered packet files under a data dir outside the repository (phase 1c writes them)."""
    d = tmp_path / "data" / "packets"
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
    app = app_of(conn, pid)
    assert app["status"] == "applied"
    assert app["resume_version"] == f"packet:{pid}/resume/v2"
    assert app["cover_letter_path"] == str(rendered[1])
    assert attachments(conn, app["id"]) == sorted(
        [("resume", str(rendered[0])), ("cover_letter", str(rendered[1]))]
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
