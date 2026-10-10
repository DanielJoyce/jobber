"""apply/capture.py (specs/017 phase 1e): map, URL choice, add or preview, dedupe, actions.

Everything offline. No scorer exists here: capture never scores, so "never auto-score" is
checked by counting fit_score, prefilter_result and llm_spend after every outcome.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobhunter.apply import capture, paste
from jobhunter.apply.capture_models import (
    AddRequest,
    BoardCapture,
    CaptureRequest,
    DescribeRequest,
    Facts,
    LinkRequest,
)
from jobhunter.core import db
from jobhunter.pipeline.dedupe_url import normalize_apply_url
from jobhunter.scoring.profile import Profile

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "capture" / "jsonld_vectors.json").read_text()
)["vectors"]
LONG = " ".join(["Operate the synthetic Linux fleet and write Terraform modules."] * 6)
PAGE = "https://careers.acme.example/jobs/123"
GH = "https://boards.greenhouse.io/acme/jobs/4012345"
LI_ID = "4012345678"


def aid() -> str:
    return str(uuid.uuid4())


def posting(**kw):
    base = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Platform Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme Synthetic"},
        "jobLocation": {"address": {"addressLocality": "Denver", "addressRegion": "CO"}},
        "description": f"<p>{LONG}</p>",
        "url": PAGE,
    }
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def facts(*postings, url=PAGE, **kw) -> Facts:
    return Facts(url=url, jsonld=[json.dumps(p) for p in postings], **kw)


# ─── map and sanitize ──────────────────────────────────────────────────────


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_jsonld_vectors(vec):
    a = capture.analyse(Facts(url=vec["page"], jsonld=[json.dumps(vec["jsonld"])]))
    m = a.mapped
    assert m is not None and m.source == "jsonld"
    exp = vec["expect"]
    for key in ("title", "employer", "location_raw", "salary_raw", "posted_at", "closes_at"):
        if key in exp:
            assert getattr(m, key) == exp[key], key
    if "employment_type" in exp:
        assert m.employment_type == exp["employment_type"]
    for s in exp.get("description_has", []):
        assert s in a.description
    for s in exp.get("description_lacks", []):
        assert s not in a.description


def test_salary_text_is_read_by_the_salary_parser():
    from jobhunter.core.textnorm import parse_salary

    got = parse_salary("USD 120,000-150,000 per YEAR")
    assert (got.min, got.max, got.period) == (120000, 150000, "year")


def test_og_site_name_is_ignored_on_board_hosts():
    page = facts(
        url=f"https://www.linkedin.com/jobs/view/{LI_ID}/",
        og_title="Platform Engineer",
        og_site_name="LinkedIn",
        page_text=LONG,
    )
    assert capture.analyse(page).employer == ""
    other = facts(
        url="https://careers.acme.example/jobs/1",
        og_title="Platform Engineer",
        og_site_name="Acme Synthetic",
        page_text=LONG,
    )
    assert capture.analyse(other).employer == "Acme Synthetic"


def test_page_title_rule_from_1a():
    a = capture.analyse(facts(document_title="Job Application for SRE at Acme", page_text=LONG))
    assert (a.title, a.employer, a.desc_source) == ("SRE", "Acme", "page")


def test_microdata_and_site_rows_map():
    md = Facts(
        url=PAGE,
        microdata=[
            {"name": "title", "value": "Analyst"},
            {"name": "hiringOrganization", "value": "Micro Co"},
            {"name": "description", "value": LONG},
            {"name": "employmentType", "value": "FULL_TIME"},
        ],
    )
    a = capture.analyse(md)
    assert (a.title, a.employer, a.desc_source, a.add_at_once) == (
        "Analyst",
        "Micro Co",
        "microdata",
        True,
    )
    site = Facts(url=PAGE, site={"title": "T", "employer": "E", "description": LONG})
    assert capture.analyse(site).desc_source == "site"


def test_oversized_fields_are_refused_by_the_models():
    with pytest.raises(ValueError):
        Facts(url=PAGE, page_text="x" * 100_001)
    with pytest.raises(ValueError):
        Facts(url=PAGE, jsonld=["{}"] * 6)
    with pytest.raises(ValueError):
        Facts(url=PAGE, og_title="x" * 1001)
    with pytest.raises(ValueError):
        Facts(url=PAGE, surprise="field")


# ─── URL choice ────────────────────────────────────────────────────────────


def test_generic_jsonld_url_is_not_used():
    a = capture.analyse(facts(posting(url="https://careers.acme.example/")))
    assert a.url == PAGE


def test_relative_jsonld_url_is_resolved():
    a = capture.analyse(facts(posting(url="/jobs/123?ref=x"), url=PAGE + "?utm_source=li"))
    assert a.url == PAGE


def test_jsonld_url_on_another_host_is_not_used():
    a = capture.analyse(facts(posting(url="https://evil.example/jobs/999"), url=PAGE))
    assert a.url == PAGE


def test_jsonld_url_on_an_ats_host_is_used_once_the_posting_is_chosen():
    page = "https://acme.example/careers/platform"
    # On its own it names another posting than this page: stale, previewed, page URL kept.
    a = capture.analyse(facts(posting(url=GH), url=page, canonical=page))
    assert a.stale and a.url == page
    # Picked in the preview: its ATS URL identifies the posting and is used.
    assert capture.analyse(facts(posting(url=GH), url=page), posting_index=0).url == GH


@pytest.mark.parametrize(
    "query", ["jobId=77", "sjobid=77", "j=77", "JobId=77", "adid=77", "job=77",
              "JobOpeningId=77", "career_job_req_id=77", "opportunityId=77"],
)  # fmt: skip
def test_query_ids_are_kept_and_tracking_dropped(query):
    page = f"https://jobs.example.org/careers/details?{query}&utm_source=x&trk=y&gclid=z"
    a = capture.analyse(Facts(url=page, page_text=LONG))
    assert a.url == f"https://jobs.example.org/careers/details?{query}"


def test_board_pages_reduce_to_their_id():
    a = capture.analyse(
        Facts(url=f"https://www.linkedin.com/jobs/search/?currentJobId={LI_ID}&keywords=sre")
    )
    assert a.url == f"https://www.linkedin.com/jobs/view/{LI_ID}/"
    assert a.board_key == f"linkedin:{LI_ID}"


# ─── add at once or preview ────────────────────────────────────────────────


def test_clear_structured_capture_adds_at_once():
    assert capture.analyse(facts(posting())).add_at_once


def test_page_text_is_previewed():
    a = capture.analyse(Facts(url=PAGE, og_title="SRE at Acme", page_text=LONG))
    assert not a.add_at_once and a.desc_source == "page"


def test_several_postings_with_no_match_give_a_picker():
    a = capture.analyse(
        facts(
            posting(url="https://careers.acme.example/jobs/1"),
            posting(title="Other", url="https://careers.acme.example/jobs/2"),
        )
    )
    assert a.ambiguous and not a.add_at_once
    assert len(capture._preview(a).postings) == 2


def test_several_postings_pick_the_one_for_this_page():
    a = capture.analyse(
        facts(posting(title="Other", url="https://careers.acme.example/jobs/2"), posting())
    )
    assert a.index == 1 and a.add_at_once


def test_stale_jsonld_on_a_board_page_is_previewed():
    a = capture.analyse(
        Facts(
            url="https://www.linkedin.com/jobs/search/?currentJobId=4000000002",
            jsonld=[json.dumps(posting(url="https://www.linkedin.com/jobs/view/4000000001/"))],
        )
    )
    assert a.stale and not a.add_at_once and a.mapped is None


def test_single_stale_jsonld_without_board_id_is_previewed():
    a = capture.analyse(facts(posting(url="https://careers.acme.example/jobs/999"), url=PAGE))
    assert a.stale and not a.add_at_once


def test_icon_selection_is_previewed_with_both_choices():
    a = capture.analyse(facts(posting(), selection="A triple-clicked paragraph of text " * 3))
    assert not a.add_at_once
    p = capture._preview(a)
    assert p.selection and p.page_description and p.description == p.page_description


def test_menu_selection_without_structured_title_is_previewed():
    a = capture.analyse(
        Facts(url=PAGE, trigger="menu-selection", selection=LONG, page_text="nav stuff")
    )
    assert a.desc_source == "selection" and not a.add_at_once


def test_menu_selection_with_structured_title_adds():
    a = capture.analyse(facts(posting(), trigger="menu-selection", selection=LONG))
    assert a.desc_source == "selection" and a.add_at_once


def test_short_structured_description_is_previewed():
    a = capture.analyse(facts(posting(description="Short.")))
    assert not a.add_at_once


# ─── apply control ─────────────────────────────────────────────────────────


def li(kind="offsite", href=None, **kw):
    return Facts(
        url=f"https://www.linkedin.com/jobs/view/{LI_ID}/",
        apply_control={"kind": kind, "label": "Apply", "href": href},
        og_title="Platform Engineer",
        page_text=LONG,
        **kw,
    )


def test_apply_control_direct_ats_href():
    a = capture.analyse(li(href=GH + "?gh_src=abc"))
    assert (a.apply_mode, a.destination) == ("offsite", GH)


def test_apply_control_redirect_wrapper_is_decoded_locally():
    wrapped = "https://www.linkedin.com/redir?url=" + GH.replace(":", "%3A").replace("/", "%2F")
    assert capture.analyse(li(href=wrapped)).destination == GH


def test_apply_control_button_has_no_destination():
    a = capture.analyse(li(kind="offsite", href=None))
    assert (a.apply_mode, a.destination) == ("offsite", None)


def test_easy_apply_is_recorded_without_destination():
    a = capture.analyse(li(kind="easy_apply", href=None))
    assert (a.apply_mode, a.destination) == ("easy_apply", None)


def test_apply_control_to_another_board_url_is_no_destination():
    a = capture.analyse(li(href="https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"))
    assert a.destination is None


def test_apply_control_ignored_off_board():
    a = capture.analyse(
        Facts(url=PAGE, apply_control={"kind": "offsite", "href": GH}, page_text=LONG)
    )
    assert a.destination is None


# ─── database: dedupe, writes, actions ─────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "c.db"
    c = db.connect(path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


PROFILE = Profile()


def cap(conn, f, bc=None, action=None):
    return capture.capture(
        conn,
        CaptureRequest(action_id=action or aid(), facts=f, board_capture=bc),
        PROFILE,
        now=NOW,
    )


def ingested(conn, n, *, url, title="Platform Engineer", employer="Acme Synthetic",
             text=LONG, completeness="full"):  # fmt: skip
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, employer, description_text, "
        "description_completeness, stage, first_seen_at, last_seen_at) "
        "VALUES (?, 'co', ?, ?, ?, ?, ?, ?, 'grouped', ?, ?)",
        (n, str(n), url, title, employer, text, completeness, ISO, ISO),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, ISO),
    )
    conn.execute("UPDATE job SET job_group_id = ? WHERE id = ?", (n, n))
    return n


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def never_scored(conn):
    assert count(conn, "fit_score") == 0
    assert count(conn, "prefilter_result") == 0
    assert count(conn, "llm_spend") == 0
    stages = {r[0] for r in conn.execute("SELECT stage FROM job WHERE source_key='paste-manual'")}
    assert stages <= {"normalized"}


def test_clear_capture_adds_a_flagged_paste_manual_group_with_locations(conn):
    r = cap(
        conn, facts(posting(baseSalary={"currency": "USD", "value": 90000, "unitText": "YEAR"}))
    )
    assert r.outcome == "added" and r.group is not None
    g = conn.execute("SELECT * FROM job_group WHERE id = ?", (r.group.group_id,)).fetchone()
    job = conn.execute("SELECT * FROM job WHERE id = ?", (g["canonical_job_id"],)).fetchone()
    assert g["score_on_request"] == 1 and job["source_key"] == "paste-manual"
    assert job["stage"] == "normalized" and job["salary_min"] == 90000
    assert job["url"] == PAGE
    locs = conn.execute("SELECT state FROM job_locations WHERE job_id = ?", (job["id"],))
    assert [r[0] for r in locs] == ["CO"]
    assert r.group.location and "CO" in r.group.location
    log = conn.execute("SELECT * FROM capture_log").fetchone()
    assert (log["outcome"], log["method"], log["host"]) == (
        "added",
        "jsonld",
        "careers.acme.example",
    )
    assert LONG[:30] not in (log["response"] or "")  # no page text in the log
    never_scored(conn)


def test_second_capture_says_already_in_jobhunter(conn):
    first = cap(conn, facts(posting()))
    again = cap(conn, facts(posting()))
    assert again.outcome == "existing" and again.group.group_id == first.group.group_id
    assert count(conn, "job_group") == 1
    never_scored(conn)


def test_existing_by_board_id_across_url_forms(conn):
    ingested(conn, 1, url=f"https://www.linkedin.com/jobs/view/{LI_ID}?trk=x")
    r = cap(
        conn,
        Facts(url=f"https://www.linkedin.com/jobs/search/?currentJobId={LI_ID}", page_text=LONG),
    )
    assert r.outcome == "existing" and r.group.group_id == 1
    assert conn.execute("SELECT job_id FROM job_board_ref").fetchone()[0] == 1


def test_existing_by_the_page_url_when_the_chosen_url_differs(conn):
    ingested(conn, 1, url=PAGE + "?ref=email")
    r = cap(
        conn,
        facts(
            posting(url="https://boards.greenhouse.io/acme/jobs/999"),
            url=PAGE,
            canonical=PAGE,
        ),
    )
    assert r.outcome == "existing" and r.group.group_id == 1


def test_careers_home_url_match_is_possible_and_writes_nothing(conn):
    ingested(conn, 1, url="https://careers.acme.example/")
    r = cap(conn, Facts(url="https://careers.acme.example/", og_title="SRE", page_text=LONG))
    assert r.outcome == "possible" and [c.group_id for c in r.candidates] == [1]
    assert count(conn, "job_group") == 1


def test_employer_and_title_only_is_possible_then_add_as_new(conn):
    ingested(conn, 1, url="https://other.example.net/x/1")
    f = facts(posting())
    r = cap(conn, f)
    assert r.outcome == "possible" and count(conn, "job_group") == 1
    added = capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=f,
            title="Platform Engineer",
            employer="Acme Synthetic",
            description=LONG,
            force_new=True,
            source="structured",
        ),
        PROFILE,
        now=NOW,
    )
    assert added.outcome == "added" and count(conn, "job_group") == 2


def test_add_after_preview_with_edited_description(conn):
    f = Facts(url=PAGE, og_title="SRE at Acme", page_text="Hi, Pat\n" + LONG)
    r = cap(conn, f)
    assert r.outcome == "previewed" and count(conn, "job") == 0
    assert "Hi, Pat" in r.preview.description
    edited = r.preview.description.replace("Hi, Pat\n", "")
    added = capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=f,
            title="SRE",
            employer="Acme",
            description=edited,
            source="edited",
        ),
        PROFILE,
        now=NOW,
    )
    assert added.outcome == "added"
    text = conn.execute("SELECT description_text FROM job").fetchone()[0]
    assert "Pat" not in text and "Terraform" in text
    assert (
        conn.execute("SELECT method FROM capture_log WHERE route='capture/add'").fetchone()[0]
        == "page"
    )
    never_scored(conn)


def test_add_this_description_on_an_email_manual_group(conn):
    from jobhunter.console.proposals import _manual_group

    gid = _manual_group(conn, "msg-1", "Acme Synthetic", "Platform Engineer", ISO)
    rev = conn.execute("SELECT description_rev FROM job_group WHERE id = ?", (gid,)).fetchone()[0]
    f = facts(posting(baseSalary={"currency": "USD", "value": 90000, "unitText": "YEAR"}))
    r = cap(conn, f)
    assert r.outcome == "possible" and r.describe == [gid]
    out = capture.describe(conn, gid, DescribeRequest(action_id=aid(), facts=f), PROFILE, now=NOW)
    assert out.outcome == "description_added" and out.group.has_description
    row = conn.execute("SELECT * FROM job WHERE job_group_id = ?", (gid,)).fetchone()
    assert "Terraform" in row["description_text"] and row["salary_raw"]
    assert row["url"].startswith("gmail:")
    g = conn.execute("SELECT description_rev, score_on_request FROM job_group WHERE id = ?", (gid,))
    assert tuple(g.fetchone()) == (rev, 1)
    with pytest.raises(capture.Conflict):
        capture.describe(conn, gid, DescribeRequest(action_id=aid(), facts=f), PROFILE, now=NOW)
    never_scored(conn)


def test_an_ingested_partial_group_is_existing_and_not_written(conn):
    ingested(conn, 1, url=PAGE, text="A short alert.", completeness="partial")
    r = cap(conn, facts(posting()))
    assert r.outcome == "existing" and r.group.partial and r.describe == []
    assert conn.execute("SELECT description_text FROM job WHERE id = 1").fetchone()[0] == (
        "A short alert."
    )


def test_normalize_apply_url_unchanged_on_existing_vectors():
    assert normalize_apply_url(GH + "?gh_src=x") == GH
    assert normalize_apply_url(PAGE + "/?utm_source=x&b=2&a=1") == PAGE + "?a=1&b=2"


def test_two_concurrent_identical_captures_give_one_group(db_path):
    f = facts(posting())
    results: list[str] = []
    barrier = threading.Barrier(2)

    def run():
        c = db.connect(db_path)
        try:
            barrier.wait()
            results.append(cap(c, f).outcome)
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c = db.connect(db_path)
    try:
        assert count(c, "job_group") == 1
        assert sorted(results) == ["added", "existing"]
    finally:
        c.close()


def test_repeated_action_id_on_a_write_returns_the_first_answer(conn):
    action = aid()
    first = cap(conn, facts(posting()), action=action)
    again = cap(conn, facts(posting()), action=action)
    assert again.replayed and again.outcome == "added"
    assert again.group.group_id == first.group.group_id
    assert count(conn, "capture_log") == 1 and count(conn, "job_group") == 1


def test_repeated_preview_capture_is_evaluated_again(conn):
    action = aid()
    f = Facts(url=PAGE, og_title="SRE at Acme", page_text=LONG)
    assert cap(conn, f, action=action).outcome == "previewed"
    ingested(conn, 1, url=PAGE)
    again = cap(conn, f, action=action)
    assert again.outcome == "existing" and not again.replayed
    assert count(conn, "capture_log") == 1


def test_later_actions_on_the_same_page_succeed_with_new_ids(conn):
    f = Facts(url=PAGE, og_title="SRE at Acme", page_text=LONG)
    assert cap(conn, f).outcome == "previewed"
    add = AddRequest(action_id=aid(), facts=f, title="SRE", employer="Acme", description=LONG)
    assert capture.add(conn, add, PROFILE, now=NOW).outcome == "added"
    assert cap(conn, f).outcome == "existing"  # Capture again


def test_capture_never_scores_after_any_outcome(conn):
    for f in (
        facts(posting()),
        Facts(url=PAGE, og_title="X at Y", page_text=LONG),
        Facts(url="https://careers.acme.example/", page_text=LONG),
    ):
        cap(conn, f)
    never_scored(conn)


# ─── LinkedIn and Indeed ───────────────────────────────────────────────────


def test_decoded_greenhouse_url_matches_an_ingested_group(conn):
    ingested(conn, 1, url=GH)
    r = cap(conn, li(href=GH))
    assert r.outcome == "linked" and r.group.group_id == 1
    assert "Greenhouse" in r.message
    ref = conn.execute("SELECT * FROM job_board_ref").fetchone()
    assert (ref["board_id"], ref["job_id"], ref["apply_mode"], ref["apply_url"]) == (
        LI_ID,
        1,
        "offsite",
        GH,
    )


def test_decoded_url_with_disagreeing_title_is_only_an_offer(conn):
    ingested(conn, 1, url=GH, title="Office Manager")
    r = cap(conn, li(href=GH))
    assert r.outcome == "same_job" and count(conn, "job_board_ref") == 0


def test_linkedin_id_matches_an_alert_group_and_writes_its_apply_link(conn):
    ingested(conn, 1, url=f"https://www.linkedin.com/comm/jobs/view/{LI_ID}/?trk=eml")
    r = cap(conn, li(href=GH))
    assert r.outcome == "existing"
    link = conn.execute("SELECT start_url, status FROM apply_link WHERE job_group_id = 1")
    assert tuple(link.fetchone()) == (GH, "unresolved")


def test_apply_link_is_never_written_over_a_live_one(conn):
    ingested(conn, 1, url=f"https://www.linkedin.com/jobs/view/{LI_ID}/")
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at) "
        "VALUES (1, 'https://x.example/a', 'https://x.example/a', '[]', 'live', ?)",
        (ISO,),
    )
    cap(conn, li(href=GH))
    assert conn.execute("SELECT start_url FROM apply_link").fetchone()[0] == "https://x.example/a"


def test_board_id_and_ats_url_in_different_groups_give_same_job(conn):
    ingested(conn, 1, url=f"https://www.linkedin.com/jobs/view/{LI_ID}/")
    ingested(conn, 2, url=GH)
    r = cap(conn, li(href=GH))
    assert r.outcome == "same_job" and {c.group_id for c in r.candidates} == {1, 2}
    assert count(conn, "job_board_ref") == 0 and count(conn, "apply_link") == 0


def test_new_board_capture_records_easy_apply(conn):
    r = cap(conn, li(kind="easy_apply"))
    assert r.outcome == "previewed"  # page text only
    added = capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=li(kind="easy_apply"),
            title="Platform Engineer",
            employer="Acme Synthetic",
            description=LONG,
        ),
        PROFILE,
        now=NOW,
    )
    ref = conn.execute("SELECT * FROM job_board_ref").fetchone()
    assert added.outcome == "added" and ref["apply_mode"] == "easy_apply"
    assert conn.execute("SELECT url FROM job").fetchone()[0] == (
        f"https://www.linkedin.com/jobs/view/{LI_ID}/"
    )


def board_group(conn):
    return capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=li(),
            title="Platform Engineer",
            employer="Acme Synthetic",
            description=LONG,
        ),
        PROFILE,
        now=NOW,
    )


def bc_of(board, minutes=3):
    return BoardCapture(
        group_id=board.group.group_id,
        board_key=f"linkedin:{LI_ID}",
        title="Platform Engineer",
        employer="Acme Synthetic",
        minutes_ago=minutes,
    )


def test_click_through_offer_and_not_the_same(conn):
    board = board_group(conn)
    ats = facts(posting(url=GH), url=GH)
    r = cap(conn, ats, bc=bc_of(board))
    # Same employer and title as the LinkedIn capture: a possible match, and the offer.
    assert r.outcome == "possible" and r.offer is not None
    assert "3 minutes ago" in r.offer.message and r.offer.group_id == board.group.group_id
    assert r.url == GH
    # A capture from an unrelated window carries no board capture: no offer.
    assert cap(conn, ats).offer is None
    # A board capture for another job (employer and title disagree): no offer.
    other = BoardCapture(group_id=board.group.group_id, board_key="linkedin:4999999999",
                         title="Office Manager", employer="Elsewhere Inc")  # fmt: skip
    assert cap(conn, ats, bc=other).offer is None
    # Not the same: nothing linked, and the offer is not made again for that pair.
    added = capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=ats,
            title="Platform Engineer",
            employer="Acme Synthetic",
            description=LONG,
            force_new=True,
        ),
        PROFILE,
        now=NOW,
    )
    out = capture.link(
        conn,
        LinkRequest(action_id=aid(), a=board.group.group_id, b=added.group.group_id, not_same=True),
        now=NOW,
    )
    assert out.outcome == "not_same" and count(conn, "job_group") == 2
    assert count(conn, "job_board_ref") == 1
    assert cap(conn, ats, bc=bc_of(board)).offer is None


def test_link_them_with_one_group_maps_the_board_id_to_the_ats_url(conn):
    board = board_group(conn)
    out = capture.link(
        conn,
        LinkRequest(
            action_id=aid(),
            a=board.group.group_id,
            board_key=f"linkedin:{LI_ID}",
            apply_url=GH + "?gh_src=x",
        ),
        now=NOW,
    )
    assert out.outcome == "linked_groups" and count(conn, "job_group") == 1
    assert conn.execute("SELECT apply_url FROM job_board_ref").fetchone()[0] == GH


def test_link_them_merges_two_captures_and_maps_the_board_id(conn):
    board = board_group(conn)
    ats = capture.add(
        conn,
        AddRequest(
            action_id=aid(),
            facts=facts(posting(url=GH), url=GH),
            title="Platform Engineer",
            employer="Acme Synthetic",
            description=LONG,
            force_new=True,
        ),
        PROFILE,
        now=NOW,
    )
    out = capture.link(
        conn,
        LinkRequest(
            action_id=aid(),
            a=board.group.group_id,
            b=ats.group.group_id,
            board_key=f"linkedin:{LI_ID}",
            apply_url=GH,
        ),
        now=NOW,
    )
    assert out.outcome == "linked_groups" and count(conn, "job_group") == 1
    assert out.group_id == ats.group.group_id  # neither ingested or scored: the ATS capture
    ref = conn.execute("SELECT apply_url, job_id FROM job_board_ref").fetchone()
    assert ref[0] == GH
    # the LinkedIn job moved with its group: the board id resolves to the kept group
    assert conn.execute("SELECT job_group_id FROM job WHERE id = ?", (ref[1],)).fetchone()[0] == (
        ats.group.group_id
    )
    assert conn.execute("SELECT score_on_request FROM job_group").fetchone()[0] == 1


# ─── link_same_job ─────────────────────────────────────────────────────────


def score(conn, gid):
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, created_at) "
        "VALUES (?, 'screen', 'm', 'p', 's', 'strong', 80, '{}', '[]', ?)",
        (gid, ISO),
    )


def pasted_group(conn, url, title="Platform Engineer"):
    r = cap(conn, facts(posting(url=url, title=title), url=url))
    assert r.outcome == "added", r
    return r.group.group_id


def test_link_keeps_the_ingested_group_and_moves_application_and_packet(conn):
    from jobhunter.apply import packets

    ing = ingested(conn, 1, url="https://other.example.net/jobs/7", title="Unrelated A")
    cap_gid = pasted_group(conn, PAGE)
    pid = packets.prepare(conn, cap_gid, NOW)
    with db.transaction(conn):
        kept = capture.link_same_job(conn, ing, cap_gid, NOW)
    assert kept == ing
    assert conn.execute("SELECT job_group_id FROM application").fetchone()[0] == ing
    assert packets.live_packet_id(conn, ing) == pid
    assert (
        conn.execute("SELECT score_on_request FROM job_group WHERE id = ?", (ing,)).fetchone()[0]
        == 0
    )
    assert count(conn, "fit_score") == 0  # no score redone


@pytest.mark.parametrize("which", ["capture", "ingested"])
def test_link_keeps_the_only_scored_group_with_its_flag(conn, which):
    ing = ingested(conn, 1, url="https://other.example.net/jobs/7", title="Unrelated A")
    cap_gid = pasted_group(conn, PAGE)
    scored = cap_gid if which == "capture" else ing
    score(conn, scored)
    with db.transaction(conn):
        kept = capture.link_same_job(conn, ing, cap_gid, NOW)
    assert kept == scored
    flag = conn.execute("SELECT score_on_request FROM job_group WHERE id = ?", (kept,)).fetchone()
    assert flag[0] == (1 if which == "capture" else 0)
    assert count(conn, "fit_score") == 1


def test_link_two_captures_keeps_the_one_with_an_application(conn):
    from jobhunter.apply import packets

    a = pasted_group(conn, "https://careers.acme.example/jobs/1", "Alpha role")
    b = pasted_group(conn, "https://careers.acme.example/jobs/2", "Beta role")
    packets.prepare(conn, b, NOW)
    with db.transaction(conn):
        assert capture.link_same_job(conn, a, b, NOW) == b
    assert conn.execute("SELECT score_on_request FROM job_group").fetchone()[0] == 1


def test_link_two_captures_otherwise_keeps_the_ats_capture_then_the_older(conn):
    a = pasted_group(conn, "https://careers.acme.example/jobs/1", "Alpha role")
    b = pasted_group(conn, GH, "Beta role")
    with db.transaction(conn):
        assert capture.link_same_job(conn, a, b, NOW) == b
    c = pasted_group(conn, "https://careers.acme.example/jobs/3", "Gamma role")
    d = pasted_group(conn, "https://careers.acme.example/jobs/4", "Delta role")
    with db.transaction(conn):
        assert capture.link_same_job(conn, d, c, NOW) == c


def test_link_is_refused_while_a_score_claim_is_held(conn):
    a = pasted_group(conn, "https://careers.acme.example/jobs/1", "Alpha role")
    b = pasted_group(conn, "https://careers.acme.example/jobs/2", "Beta role")
    job = conn.execute("SELECT canonical_job_id FROM job_group WHERE id = ?", (a,)).fetchone()[0]
    conn.execute(
        "INSERT INTO prefilter_result (job_id, passed, reasons, filter_version, evaluated_at) "
        "VALUES (?, 1, '[\"user-requested\"]', 'v', ?)",
        (job, (NOW - timedelta(minutes=1)).isoformat()),
    )
    with pytest.raises(capture.Conflict, match="scoring in progress"):
        capture.link(conn, LinkRequest(action_id=aid(), a=a, b=b), now=NOW)
    assert count(conn, "job_group") == 2


def test_repeated_link_action_returns_the_stored_answer(conn):
    a = pasted_group(conn, "https://careers.acme.example/jobs/1", "Alpha role")
    b = pasted_group(conn, "https://careers.acme.example/jobs/2", "Beta role")
    action = aid()
    first = capture.link(conn, LinkRequest(action_id=action, a=a, b=b), now=NOW)
    again = capture.link(conn, LinkRequest(action_id=action, a=a, b=b), now=NOW)
    assert again.replayed and again.group_id == first.group_id


def test_current_group_follows_a_merged_capture(conn):
    a = pasted_group(conn, "https://careers.acme.example/jobs/1", "Alpha role")
    b = pasted_group(conn, GH, "Beta role")
    job_a = conn.execute("SELECT canonical_job_id FROM job_group WHERE id = ?", (a,)).fetchone()[0]
    capture.link(conn, LinkRequest(action_id=aid(), a=a, b=b), now=NOW)
    assert not capture.group_exists(conn, a)
    assert capture.current_group(conn, a, None) == b
    assert capture.current_group(conn, a, job_a) == b


# ─── capture/fetch checks ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.acme.example/jobs/1",
        "http://boards.greenhouse.io/acme/jobs/1",
        "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/X_R-1",
        "ftp://boards.greenhouse.io/acme/jobs/1",
    ],
)
def test_fetch_refuses_anything_but_https_supported_ats(conn, url):
    calls: list[str] = []
    with pytest.raises(capture.CaptureError):
        capture.fetch(
            conn,
            capture.FetchRequest(action_id=aid(), url=url),
            lambda u: calls.append(u) or paste.Fetched("x"),
            now=NOW,
        )
    assert calls == []


def test_fetch_uses_the_direct_board_url_and_logs(conn):
    calls: list[str] = []
    embed = "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345"
    out = capture.fetch(
        conn,
        capture.FetchRequest(action_id=aid(), url=embed),
        lambda u: calls.append(u) or paste.Fetched(LONG, "T", "E"),
        now=NOW,
    )
    assert calls == [GH] and out.text == LONG
    assert conn.execute("SELECT outcome, method FROM capture_log").fetchone()[:] == (
        "fetched",
        "fetch",
    )


def test_embedded_greenhouse_iframe_offers_fetch():
    a = capture.analyse(
        Facts(
            url="https://acme.example/careers/sre",
            og_title="SRE at Acme",
            page_text="Short.",
            iframes=["https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345"],
        )
    )
    assert a.fetch is not None and a.fetch.kind == "fetch" and a.fetch.url == GH
    ashby = capture.analyse(
        Facts(
            url="https://acme.example/careers/sre",
            page_text="Short.",
            iframes=["https://jobs.ashbyhq.com/acme/0b6f0ae1-9d1b-4b55-9c4b-2f0f2f0f2f0f/embed"],
        )
    )
    assert ashby.fetch is not None and ashby.fetch.kind == "select"
