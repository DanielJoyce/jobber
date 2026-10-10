"""Review fixes for assisted apply phase 1a (specs/017, bug 288631b). No network, no scorer."""

from __future__ import annotations

import json

import httpx
import pytest
from test_packet_pages import (  # noqa: F401  (fixtures)
    DIMS,
    GH,
    ISO,
    NOW,
    QUOTE,
    TEXT,
    _fresh_shared_state,
    client,
    conn,
    db_path,
    job_of,
    new_packet,
    packet_group,
    pages,
    pdir,
)

from jobhunter.console import dashboard as dash
from jobhunter.console import tracking
from jobhunter.pipeline.dedupe import _refresh_group, group_pending
from jobhunter.scoring.profile import load_profile


def score_group(conn, gid):  # noqa: F811
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 80, ?, '[]', ?)",
        (gid, json.dumps(DIMS), ISO),
    )


def test_sankey_node_counts_match_the_pipeline_lists(client, conn, pdir):  # noqa: F811
    """Every application node's count is exactly what /pipeline?node=<id> lists."""
    scored = new_packet(client, text=TEXT, employer="Synthetic Co", title="Systems Engineer")
    new_packet(client, text=TEXT, employer="Other Co", title="Fleet Engineer")  # unscored
    gid = packet_group(conn, scored)
    score_group(conn, gid)
    client.post("/job/1/prepare")
    client.post("/job/2/prepare")
    app_id = conn.execute("SELECT id FROM application WHERE job_group_id = ?", (gid,)).fetchone()[0]
    tracking.add_event(conn, app_id, "applied", None, ISO)
    values = {n["id"]: n["value"] for n in dash.sankey(conn, load_profile(pdir))["nodes"]}
    assert values["pasted"] == 1 and values["applied"] >= 1
    for node in dash.PIPELINE_NODES:
        assert values.get(node, 0) == len(dash.pipeline_node_apps(conn, node)), node
    assert app_id in dash.pipeline_node_apps(conn, "applied")
    assert "Systems Engineer" in client.get("/pipeline?node=applied").text


def test_ingested_copy_never_joins_a_pasted_group(client, conn):  # noqa: F811
    pid = new_packet(client, text=TEXT, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    pasted = job_of(conn, gid)
    conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, content_hash, stage, first_seen_at, last_seen_at) "
        "VALUES ('co', 'copy', 'https://example.com/copy', 'Systems Engineer', 'Acme', ?, "
        "'full', ?, 'normalized', ?, ?)",
        (pasted["description_text"], pasted["content_hash"], ISO, ISO),
    )
    res = group_pending(conn, now=NOW)
    assert res.joined == 0 and res.created == 1
    assert job_of(conn, gid)["source_key"] == "paste-manual"
    count = conn.execute("SELECT member_count FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
    assert count == 1


def test_prepare_on_an_applied_job_writes_no_shortlist_label(client, conn):  # noqa: F811
    conn.execute(
        "INSERT INTO application (job_group_id, status, applied_at, created_at, updated_at) "
        "VALUES (2, 'applied', ?, ?, ?)",
        (ISO, ISO, ISO),
    )
    assert client.post("/job/2/prepare").status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0
    status = conn.execute("SELECT status FROM application WHERE job_group_id = 2").fetchone()[0]
    assert status == "applied"


def test_backdated_applied_event_outranks_a_later_prepare(client, conn):  # noqa: F811
    client.post("/inbox/2/label?label=interesting")
    client.post("/job/2/prepare")  # 'preparing', stamped NOW (Oct 10), after the shortlist
    app_id = conn.execute("SELECT id FROM application WHERE job_group_id = 2").fetchone()[0]
    tracking.add_event(conn, app_id, "applied", "from email", "2026-10-02T09:00:00+00:00", "email")
    row = conn.execute(
        "SELECT status, applied_at FROM application WHERE id = ?", (app_id,)
    ).fetchone()
    assert row["status"] == "applied" and row["applied_at"].startswith("2026-10-02")
    # A manual move back is still the user's call.
    tracking.add_event(conn, app_id, "interested", "moved back", "2026-10-11T09:00:00+00:00")
    status = conn.execute("SELECT status FROM application WHERE id = ?", (app_id,)).fetchone()[0]
    assert status == "interested"


def test_auto_notes_match_the_notes_written():
    from jobhunter.console import inbox

    assert inbox.SHORTLIST_NOTE == tracking.SHORTLIST_NOTE


def test_pasted_placeholder_is_never_a_sibling_link(client, conn):  # noqa: F811
    pid = new_packet(client, text=TEXT, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    # A full copy in the same group becomes canonical; the pasted job is now a sibling.
    conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "description_completeness, job_group_id, stage, first_seen_at, last_seen_at) "
        "VALUES ('co', 'x9', 'https://example.com/x9', 'Systems Engineer', 'Acme', ?, 'full', "
        "?, 'grouped', ?, ?)",
        (TEXT + " longer", gid, ISO, ISO),
    )
    _refresh_group(conn, gid)
    page = client.get(f"/job/{gid}").text
    assert "Also posted (1)" in page and "paste:" not in page


@pytest.mark.parametrize("bad", ["http://[", "https://[::1/x", "http://example.com:99999/x"])
def test_malformed_url_rerenders_the_form_with_the_text_kept(client, conn, bad):  # noqa: F811
    data = {"url": bad, "text": TEXT, "employer": "Acme", "title": "Engineer"}
    r = client.post("/apply/new", data=data)
    assert r.status_code == 422 and "not valid" in r.text and QUOTE in r.text
    r = client.post("/apply/new/fetch", data=data)
    assert r.status_code == 200 and "not valid" in r.text and QUOTE in r.text
    assert conn.execute("SELECT COUNT(*) FROM application_packet").fetchone()[0] == 0


def test_json_ld_scalar_falls_back_to_the_page(client, pages):  # noqa: F811
    body = f"<main><p>{TEXT}</p><p>{'More detail. ' * 30}</p></main>"
    for scalar in ("null", '"a string"', "42", "true"):
        html = f"<script type='application/ld+json'>{scalar}</script>{body}"
        pages.responses[GH] = httpx.Response(200, text=html)
        r = client.post("/apply/new/fetch", data={"url": GH})
        assert r.status_code == 200 and "Fetched." in r.text, scalar


def test_packet_page_asks_did_you_apply_after_open_application(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, text=TEXT, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    assert 'id="did-apply"' not in client.get(f"/packet/{pid}").text
    conn.execute(
        "INSERT INTO apply_click (job_group_id, at, final_url, outcome) "
        "VALUES (?, '2026-10-11T00:00:00+00:00', ?, 'redirected')",
        (gid, GH),
    )
    page = client.get(f"/packet/{pid}").text
    assert 'id="did-apply"' in page and f'hx-post="/job/{gid}/applied?choice=yes"' in page


def test_url_only_pasted_job_page_has_no_email_alert_banner(client, conn):  # noqa: F811
    pid = new_packet(client, url=GH, employer="Acme", title="Systems Engineer")
    gid = packet_group(conn, pid)
    page = client.get(f"/job/{gid}").text
    assert "email alert" not in page and "Save and re-score" not in page
    assert 'id="pasted-no-text"' in page and f'href="/packet/{pid}"' in page
    # Even posted directly, the text is stored without queueing a re-score.
    assert client.post(f"/job/{gid}/description", data={"text": TEXT}).status_code == 303
    rev = conn.execute("SELECT description_rev FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
    assert rev == 0
    assert QUOTE in job_of(conn, gid)["description_text"]


def test_new_packet_key_works_without_a_profile(client, pdir):  # noqa: F811
    (pdir / "preferences.yaml").unlink()
    page = client.get("/inbox").text
    assert "No profile yet" in page and 'href="/apply/new"' in page
    assert "inbox.js" in page  # binds n
