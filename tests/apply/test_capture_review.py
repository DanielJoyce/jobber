"""Fixes from the review of 5aed9f6 (specs/017 phase 1e): Link them, same_job, URL matching,
claims, board hosts. Offline; no scorer."""

# ruff: noqa: F811  (pytest fixtures imported from test_capture)
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from test_capture import (  # noqa: F401  (fixtures and helpers)
    GH,
    ISO,
    LONG,
    NOW,
    PROFILE,
    aid,
    cap,
    conn,
    count,
    db_path,
    facts,
    ingested,
    li,
    posting,
)

from jobhunter.apply import capture, paste
from jobhunter.apply.capture_models import AddRequest, Facts, LinkRequest
from jobhunter.core import db

HOME = "https://careers.acme.example/"


def li_page(n: int, title: str, href: str) -> Facts:
    return Facts(
        url=f"https://www.linkedin.com/jobs/view/400000000{n}/",
        apply_control={"kind": "offsite", "label": "Apply", "href": href},
        og_title=title,
        page_text=LONG,
    )


def add(conn, f, title, employer="Acme Synthetic", force_new=False):
    return capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=f,
            title=title,
            employer=employer,
            description=LONG,
            force_new=force_new,
        ),
        PROFILE,
        now=NOW,
    )


# ─── a careers home is no match key; same_job can be added as new ──────────


@pytest.mark.parametrize(
    "href", [HOME, "https://acme.wd5.myworkdayjobs.com/External", "https://jobs.lever.co/acme"]
)
def test_a_second_board_job_with_the_same_careers_home_apply_is_not_same_job(conn, href):
    assert add(conn, li_page(1, "Platform Engineer", href), "Platform Engineer").outcome == "added"
    r = cap(conn, li_page(2, "Office Manager", href))
    assert r.outcome == "previewed"  # page text only; nothing matched
    assert add(conn, li_page(2, "Office Manager", href), "Office Manager").outcome == "added"
    assert count(conn, "job_group") == 2


def test_add_as_new_works_past_a_same_job_offer(conn):
    # A generic careers path on the employer's own site is offered, never linked at once.
    path = "https://www.acme.example/careers/software-engineer"
    ingested(conn, 1, url=path, title="Software Engineer")
    r = cap(conn, li_page(1, "Software Engineer II", path))
    assert r.outcome == "same_job" and r.link_mode == "pick"
    assert count(conn, "job_board_ref") == 0
    out = add(conn, li_page(1, "Software Engineer II", path), "Software Engineer II",
              force_new=True)  # fmt: skip
    assert out.outcome == "added" and count(conn, "job_group") == 2


def test_an_ats_posting_destination_with_agreeing_titles_still_links_at_once(conn):
    ingested(conn, 1, url=GH)
    assert cap(conn, li(href=GH)).outcome == "linked"


def test_two_candidates_are_never_linked_to_each_other(conn):
    for n, title in ((1, "Data Analyst"), (2, "Office Manager")):
        ingested(conn, n, url=f"https://www.acme.example/careers/role-{n}", title=title)
    with pytest.raises(capture.CaptureError, match="pick the one"):
        capture.link(
            conn,
            LinkRequest(action_id=aid(), a=1, b=2, board_key="linkedin:4012345678"),
            now=NOW,
        )
    assert count(conn, "job_group") == 2
    # Picking one candidate maps the board id to it, and merges nothing.
    out = capture.link(
        conn,
        LinkRequest(
            action_id=aid(),
            a=2,
            board_key="linkedin:4012345678",
            apply_url="https://www.acme.example/careers/role-2",
        ),
        now=NOW,
    )
    assert out.group_id == 2 and count(conn, "job_group") == 2
    ref = conn.execute("SELECT job_id FROM job_board_ref").fetchone()[0]
    assert ref == 2


def test_a_search_page_that_keeps_its_url_is_not_existing_for_another_posting(conn):
    search = "https://careers.acme.example/search?q=engineer"
    first = facts(posting(url="https://careers.acme.example/job/1"), url=search)
    assert cap(conn, first).outcome == "previewed"  # stale against the page: previewed
    added = capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=first,
            posting_index=0,
            title="Platform Engineer",
            employer="Acme Synthetic",
            description=LONG,
            source="structured",
        ),
        PROFILE,
        now=NOW,
    )
    assert added.outcome == "added"
    second = facts(
        posting(url="https://careers.acme.example/job/2", title="Office Manager"), url=search
    )

    def add_second(force_new):
        return capture.add(
            conn,
            AddRequest(
                action_id=aid(),
                facts=second,
                posting_index=0,
                title="Office Manager",
                employer="Acme Synthetic",
                description=LONG,
                source="structured",
                force_new=force_new,
            ),
            PROFILE,
            now=NOW,
        )

    # Only the page URL is shared: a weak match the user can add past, never "existing".
    assert add_second(False).outcome == "possible"
    assert add_second(True).outcome == "added" and count(conn, "job_group") == 2


# ─── Link them is refused while anything may write to the groups ───────────


def claim(conn, gid, minutes=1):
    job = conn.execute("SELECT canonical_job_id FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO prefilter_result (job_id, passed, reasons, filter_version, "
        "evaluated_at) VALUES (?, 1, '[\"user-requested\"]', 'v', ?)",
        (job, (NOW - timedelta(minutes=minutes)).isoformat()),
    )


def score(conn, gid, at, model="m"):
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', ?, 'p', 's', 'strong', 80, '{}', '[]', ?)",
        (gid, model, at.isoformat()),
    )


def test_a_live_claim_blocks_link_even_with_an_older_score(conn):
    ingested(conn, 1, url="https://a.example/jobs/1", title="One")
    ingested(conn, 2, url="https://b.example/jobs/2", title="Two")
    score(conn, 2, NOW - timedelta(days=3))
    claim(conn, 2)
    with db.transaction(conn), pytest.raises(capture.Conflict):
        capture.link_same_job(conn, 1, 2, NOW)
    # The claim answered by a score written after it is no claim any more.
    score(conn, 2, NOW, model="m2")
    with db.transaction(conn):
        assert capture.link_same_job(conn, 1, 2, NOW) in (1, 2)


def test_link_is_refused_while_a_rescore_runs(conn):
    ingested(conn, 1, url="https://a.example/jobs/1", title="One")
    ingested(conn, 2, url="https://b.example/jobs/2", title="Two")
    conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd, status, started_at, heartbeat_at) "
        "VALUES (?, 'all', 's', 1, 0.001, 0.001, 'running', ?, ?)",
        (ISO, ISO, ISO),
    )
    with db.transaction(conn), pytest.raises(capture.Conflict, match="re-score"):
        capture.link_same_job(conn, 1, 2, NOW)


# ─── board hosts are never fetched ─────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/4012345678/",
        "https://www.indeed.com/viewjob?jk=abcdef1234567890",
    ],
)
def test_new_packet_fetch_never_requests_a_board_host(url):
    def factory(u):
        raise AssertionError("no FetchContext for a board host")

    with pytest.raises(paste.PasteError, match="does not fetch"):
        paste.fetch_posting(factory, url)


def test_capture_never_reads_page_text_into_the_log(conn):
    cap(conn, facts(posting()))
    row = conn.execute("SELECT response FROM capture_log").fetchone()[0]
    assert LONG[:40] not in json.dumps(row)
