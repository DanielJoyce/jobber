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


# ─── final check of 5aed9f6 ────────────────────────────────────────────────

CAREERS = "https://www.acme.example/careers"


@pytest.mark.parametrize(
    ("url", "posting"),
    [
        ("https://www.usajobs.gov/job/812345600", True),
        ("https://www.usajobs.gov/GetJob/ViewDetails/812345600", True),
        ("https://www.usajobs.gov/search/results/?k=analyst", False),
        ("https://nlx.jobsyn.org/0123456789ABCDEF0123456789ABCDEF", True),
        ("https://usnlx.com/seattle-wa/platform-engineer/0123ABCD/job/", True),
        ("https://usnlx.com/viewjob.asp?sjobid=12345", True),
        ("https://usnlx.com/jobs/", False),
        ("https://jobs.mitalent.org/job-seeker/job-details/JobCode/1234567", True),
        ("https://hire.wyo.gov/job/abc-123", True),
        ("https://worksource.my.site.com/worksourcewa/job-search/job-details?jobId=a1B2", True),
        ("https://jobs.utah.gov/jsp/utjobs/single-job?j=123456", True),
        ("https://labor.eightfold.ai/careerhub/explore/jobs/1234567", True),
        (GH, True),
        ("https://boards.greenhouse.io/acme", False),
        (CAREERS, False),
        ("https://careers.acme.example/", False),
        ("https://www.acme.example/careers/software-engineer-1234", False),
    ],
)
def test_posting_url_vectors(url, posting):
    from jobhunter.pipeline.posting_urls import is_posting_url

    assert is_posting_url(url) is posting


def test_a_careers_path_apply_does_not_make_two_linkedin_jobs_link_candidates(conn):
    a = add(conn, li_page(1, "Software Engineer", CAREERS), "Software Engineer")
    b = add(conn, li_page(2, "Office Manager", CAREERS), "Office Manager")
    assert (a.outcome, b.outcome) == ("added", "added")
    ga, gb = a.group.group_id, b.group.group_id
    assert capture.same_job_candidates(conn, ga) == []
    assert capture.same_job_candidates(conn, gb) == []
    with pytest.raises(capture.CaptureError):
        capture.link(
            conn,
            LinkRequest(
                action_id=aid(), a=ga, b=gb, board_key="linkedin:4000000001", apply_url=CAREERS
            ),
            now=NOW,
        )
    assert count(conn, "job_group") == 2


def test_a_careers_path_apply_url_is_no_key_for_a_later_ingest(conn):
    from jobhunter.pipeline.dedupe import group_pending

    gid = add(conn, li_page(1, "Software Engineer", CAREERS), "Software Engineer").group.group_id
    conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, description_text, "
        "stage, first_seen_at, last_seen_at) VALUES ('co', 'x9', ?, 'Office Manager', "
        "'Other Employer', 'Unrelated office work in another town.', 'normalized', ?, ?)",
        (CAREERS, ISO, ISO),
    )
    group_pending(conn, now=NOW)
    jid = conn.execute("SELECT job_group_id FROM job WHERE external_id = 'x9'").fetchone()[0]
    assert jid != gid


def test_a_crafted_link_with_a_board_key_alone_is_refused(conn):
    ingested(conn, 1, url="https://boards.greenhouse.io/acme/jobs/1", title="One")
    ingested(conn, 2, url="https://boards.greenhouse.io/acme/jobs/2", title="Two")
    conn.execute(
        "INSERT INTO job_board_ref (board, board_id, job_id, seen_at) "
        "VALUES ('linkedin', '4012345678', 1, ?)",
        (ISO,),
    )
    for apply_url in (None, "https://boards.greenhouse.io/acme/jobs/3"):
        with pytest.raises(capture.CaptureError):
            capture.link(
                conn,
                LinkRequest(
                    action_id=aid(), a=1, b=2, board_key="linkedin:4012345678", apply_url=apply_url
                ),
                now=NOW,
            )
    assert count(conn, "job_group") == 2
    ok = capture.link(
        conn,
        LinkRequest(
            action_id=aid(),
            a=1,
            b=2,
            board_key="linkedin:4012345678",
            apply_url="https://boards.greenhouse.io/acme/jobs/2",
        ),
        now=NOW,
    )
    assert ok.outcome == "linked_groups"


def pair_setup(conn):
    ingested(
        conn, 1, url="https://www.linkedin.com/jobs/view/4012345678/", title="Platform Engineer"
    )
    ingested(conn, 2, url=GH, title="Platform Engineer")
    return li(href=GH)


def test_pair_mode_cannot_be_added_as_new(conn):
    f = pair_setup(conn)
    r = cap(conn, f)
    assert r.outcome == "same_job" and r.link_mode == "pair"
    out = add(conn, f, "Platform Engineer", force_new=True)
    assert out.outcome == "same_job" and count(conn, "job_group") == 2


def test_not_the_same_on_a_pair_suppresses_the_offer(conn):
    f = pair_setup(conn)
    assert cap(conn, f).outcome == "same_job"
    capture.link(conn, LinkRequest(action_id=aid(), a=1, b=2, not_same=True), now=NOW)
    again = cap(conn, f)
    assert again.outcome == "existing" and again.group.group_id == 1


def test_not_the_same_on_a_pick_suppresses_the_offer(conn):
    path = "https://www.acme.example/careers/software-engineer"
    ingested(conn, 1, url=path, title="Software Engineer")
    f = li_page(1, "Software Engineer II", path)
    r = cap(conn, f)
    assert r.outcome == "same_job" and r.link_mode == "pick"
    capture.link(
        conn,
        LinkRequest(action_id=aid(), a=1, board_key=r.board.key, apply_url=path, not_same=True),
        now=NOW,
    )
    assert cap(conn, f).outcome == "previewed"
    assert count(conn, "job_board_ref") == 0


def test_a_linkedin_apply_to_usajobs_links_at_once(conn):
    usaj = "https://www.usajobs.gov/job/812345600"
    ingested(conn, 1, url=usaj, title="IT Specialist (SYSADMIN)")
    r = cap(conn, li_page(1, "IT Specialist (SYSADMIN)", usaj))
    assert r.outcome == "linked" and r.group.group_id == 1


def test_a_shared_careers_apply_path_offers_only_a_job_whose_title_agrees(conn):
    """56d7fcf (1): an employer whose every LinkedIn Apply goes to one generic careers path
    must not get an is-it-this-job offer on each later job; only a matching title is offered."""
    path = "https://www.acme.example/careers/apply"
    ingested(conn, 1, url=path, title="Software Engineer")
    assert cap(conn, li_page(1, "Office Manager", path)).outcome == "previewed"
    r = cap(conn, li_page(2, "Software Engineer", path))
    assert r.outcome == "same_job" and r.link_mode == "pick"


def test_an_ats_posting_destination_with_other_titles_is_still_offered(conn):
    ingested(conn, 1, url=GH, title="Platform Engineer")
    r = cap(conn, li_page(1, "Office Manager", GH))
    assert r.outcome == "same_job" and r.link_mode == "pick"
