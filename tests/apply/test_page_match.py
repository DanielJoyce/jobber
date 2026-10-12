"""Matching a form to a job (specs/022 "Matching the page to a job", "Testing" Matching).

Each rule decides on its own evidence and nothing else: a session's posting key (or, on Fill
this page, its tab and starting URL), your earlier pick, one group's stored URL, one recent
Open application click. Employer and title never match. The tab id is always the worker's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunter.apply import page_match
from jobhunter.apply.page_match import match, pick, posting_key
from jobhunter.core import db
from jobhunter.pipeline.dedupe import _refresh_group
from jobhunter.pipeline.dedupe_url import MergeResult, _absorb, normalize_apply_url

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
U1 = "0c0f2a5e-1b2c-4d3e-8f90-123456789abc"
U2 = "1d1f3b6f-2c3d-4e4f-9a01-23456789abcd"

GH_A = "https://boards.greenhouse.io/acme/jobs/4012345"
GH_B = "https://boards.greenhouse.io/acme/jobs/4012399"
GENERIC = "https://careers.acme.example/jobs/77/apply"


# ─── posting keys ──────────────────────────────────────────────────────────

SAME_POSTING = {
    "greenhouse:acme:4012345": [
        "https://boards.greenhouse.io/acme/jobs/4012345",
        "https://boards.greenhouse.io/acme/jobs/4012345#app",
        "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=abc&t=1",
        "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "https://job-boards.eu.greenhouse.io/Acme/jobs/4012345/",
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
        "https://job-boards.greenhouse.io/embed/job_app?token=4012345&for=acme&b=x",
        "https://boards.greenhouse.io/acme/jobs/4012345/confirmation",
    ],
    f"lever:acme:{U1}": [
        f"https://jobs.lever.co/acme/{U1}",
        f"https://jobs.lever.co/acme/{U1}/apply",
        f"https://jobs.eu.lever.co/Acme/{U1.upper()}/apply?lever-source=x",
        f"https://jobs.lever.co/acme/{U1}/thanks",
    ],
    f"ashby:acme:{U1}": [
        f"https://jobs.ashbyhq.com/acme/{U1}",
        f"https://jobs.ashbyhq.com/Acme/{U1}/application",
        f"https://jobs.ashbyhq.com/acme/{U1}/embed?version=2",
        f"https://jobs.ashbyhq.com/acme/embed?ashby_jid={U1}",
    ],
    "workable:acme:AB12CD34EF": [
        "https://apply.workable.com/acme/j/AB12CD34EF/",
        "https://apply.workable.com/acme/j/ab12cd34ef/apply/",
        "https://apply.workable.com/Acme/j/AB12CD34EF/apply/?utm_source=x",
    ],
    "workday:acme:external:r-12345": [
        "https://acme.wd5.myworkdayjobs.com/External/job/Denver-CO/Platform-Engineer_R-12345",
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Denver-CO/Platform-Engineer_R-12345/apply",
        "https://acme.wd1.myworkdayjobs.com/External/job/Remote/Platform-Engineer_R-12345/apply/applyManually",
        "https://acme.wd5.myworkdayjobs.com/External/details/Platform-Engineer_R-12345",
        "https://wd5.myworkdaysite.com/en-US/recruiting/acme/External/job/Platform-Engineer_R-12345",
    ],
}  # fmt: skip


@pytest.mark.parametrize(
    "key, url", [(k, u) for k, urls in SAME_POSTING.items() for u in urls], ids=lambda x: x[:60]
)
def test_posting_form_and_embed_urls_give_one_key(key, url):
    assert posting_key(url).key == key


def test_a_tracking_wrapper_is_unwrapped_first():
    wrapped = (
        "https://www.google.com/url?q=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F4012345"
    )
    assert posting_key(wrapped).key == "greenhouse:acme:4012345"


def test_different_postings_give_different_keys():
    keys = {posting_key(urls[0]).key for urls in SAME_POSTING.values()}
    keys |= {
        posting_key(GH_B).key,
        posting_key("https://boards.greenhouse.io/other/jobs/4012345").key,
        posting_key(f"https://jobs.lever.co/acme/{U2}").key,
        posting_key(f"https://jobs.ashbyhq.com/other/{U1}").key,
    }
    assert len(keys) == len(SAME_POSTING) + 4


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/embed/job_board?for=acme",
        "https://boards.greenhouse.io/embed/job_app?token=4012345",  # no board
        "https://jobs.lever.co/acme",
        "https://jobs.ashbyhq.com/acme",
        "https://apply.workable.com/acme/",
        "https://acme.wd5.myworkdayjobs.com/External",
        "https://acme.wd5.myworkdayjobs.com/External/login",
        "https://careers.acme.example/jobs/77?gh_jid=4012345",  # employer page: generic
        "https://acme.icims.com/jobs/1234/job",  # not a supported ATS
        "not a url",
        "",
        None,
    ],
)
def test_pages_that_name_no_posting_have_no_key(url):
    assert posting_key(url) is None


# ─── a database ────────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('co', 'A', 'Synthetic', 'x', 'http', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


_ext = iter(range(1, 10_000))


def group(conn, url="https://example.com/x", *, apply_url=None, page_url=None, title="Engineer",
          employer="Acme Synthetic") -> int:  # fmt: skip
    gid = conn.execute(
        "INSERT INTO job_group (canonical_job_id, member_count, method, created_at) "
        "VALUES (NULL, 1, 'exact_hash', ?)",
        (ISO,),
    ).lastrowid
    add_job(conn, gid, url, apply_url=apply_url, page_url=page_url, title=title, employer=employer)
    return gid


def add_job(conn, gid, url, *, apply_url=None, page_url=None, title="Engineer",
            employer="Acme Synthetic") -> int:  # fmt: skip
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, url, title, employer, apply_url, page_url, "
        "job_group_id, stage, description_text, first_seen_at, last_seen_at) "
        "VALUES ('co', ?, ?, ?, ?, ?, ?, ?, 'grouped', 'text', ?, ?)",
        (str(next(_ext)), url, title, employer, apply_url, page_url, gid, ISO, ISO),
    ).lastrowid
    _refresh_group(conn, gid)
    return jid


def application(conn, gid) -> int:
    return conn.execute(
        "INSERT INTO application (job_group_id, status, created_at, updated_at) "
        "VALUES (?, 'preparing', ?, ?)",
        (gid, ISO, ISO),
    ).lastrowid


def session(conn, gid, *, ats="greenhouse", key=None, start_url=None, tab_id=None,
            finished=False) -> int:  # fmt: skip
    app = conn.execute("SELECT id FROM application WHERE job_group_id = ?", (gid,)).fetchone()
    app_id = app[0] if app else application(conn, gid)
    return conn.execute(
        "INSERT INTO fill_session (application_id, ats, posting_key, start_url, tab_id, host, "
        "started_at, last_seen_at, finished_at) VALUES (?, ?, ?, ?, ?, 'h', ?, ?, ?)",
        (app_id, ats, key, start_url, tab_id, ISO, ISO, ISO if finished else None),
    ).lastrowid


def m(conn, url, tab_id=None, session_id=None, now=NOW):
    return match(conn, url, tab_id, session_id, now=now)


# ─── rule 1: the session ───────────────────────────────────────────────────


def test_rule1_ats_session_with_the_frames_posting_key_decides(conn):
    a = group(conn)
    b = group(conn, GH_A)  # rule 3 would say b
    sid = session(conn, a, key="greenhouse:acme:4012345", tab_id=5)
    r = m(conn, "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345", 5, sid)
    assert (r.decided, r.rule, r.group_id, r.session_id) == (True, 1, a, sid)
    assert r.application_id is not None
    assert b != a


def test_rule1_session_bound_to_job_a_on_job_bs_form_asks(conn):
    a = group(conn, GH_A)
    b = group(conn, GH_B)
    sid = session(conn, a, key="greenhouse:acme:4012345", tab_id=5)
    r = m(conn, GH_B, 5, sid)
    assert not r.decided and r.rule == 5
    assert {c.group_id for c in r.candidates} == {a, b}


def test_rule1_generic_session_binds_its_own_tab_and_start_url(conn):
    a = group(conn)
    start = normalize_apply_url(GENERIC)
    sid = session(conn, a, ats="generic", start_url=start, tab_id=7)
    # Same tab, same page (tracking parameters and a trailing slash do not matter).
    for url in (GENERIC, GENERIC + "/?utm_source=x"):
        r = m(conn, url, 7, sid)
        assert (r.decided, r.rule, r.group_id) == (True, 1, a), url
    # Found by the worker's tab alone, without the session id.
    assert m(conn, GENERIC, 7).rule == 1
    # Another tab, or another page in the same tab, is not bound.
    assert not m(conn, GENERIC, 8, sid).decided
    assert not m(conn, "https://careers.acme.example/jobs/78/apply", 7, sid).decided
    assert not m(conn, GENERIC, None, sid).decided


def test_rule1_two_null_posting_keys_never_match(conn):
    a = group(conn)
    sid = session(conn, a, ats="generic", start_url=None, tab_id=7)
    # An ATS page with no posting key: the generic session's NULL key is not equal to it.
    r = m(conn, "https://boards.greenhouse.io/acme", 7, sid)
    assert not r.decided and r.posting_key is None
    # A generic session with no start_url binds nothing either.
    assert not m(conn, GENERIC, 7, sid).decided
    # An ATS session with no posting key does not decide an ATS frame.
    sid2 = session(conn, a, key=None, tab_id=9)
    assert not m(conn, GH_A, 9, sid2).decided


def test_rule1_finished_session_is_not_found_by_tab(conn):
    a = group(conn)
    session(conn, a, ats="generic", start_url=normalize_apply_url(GENERIC), tab_id=7,
            finished=True)  # fmt: skip
    assert not m(conn, GENERIC, 7).decided


def test_the_tab_id_is_the_workers_never_the_pages(conn):
    """A page cannot name another tab: only the ``tab_id`` argument (the worker's) binds."""
    a = group(conn)
    sid = session(conn, a, ats="generic", start_url=normalize_apply_url(GENERIC), tab_id=7)
    lure = GENERIC + "?tab=7&tab_id=7&tabId=7"
    session(conn, a, ats="generic", start_url=normalize_apply_url(lure), tab_id=7)
    # The page claims tab 7 in its own URL; the worker says tab 3.
    assert not m(conn, lure, 3).decided
    assert not m(conn, lure, 3, sid).decided
    assert m(conn, lure, 7).decided  # the same page in the real tab 7


# ─── rule 2: your earlier pick ─────────────────────────────────────────────


def test_rule2_posting_ref_decides_before_stored_urls(conn):
    a = group(conn)
    b = group(conn, GH_A)
    pick(conn, GH_A, a, now=NOW)
    r = m(conn, "https://job-boards.greenhouse.io/acme/jobs/4012345", 1)
    assert (r.decided, r.rule, r.group_id) == (True, 2, a)
    assert b != a


def test_rule2_a_pick_survives_a_merge(conn):
    a = group(conn)
    s = group(conn)
    pick(conn, GH_A, a, now=NOW)
    _absorb(conn, s, a, NOW, MergeResult())
    r = m(conn, GH_A, 1)
    assert (r.rule, r.group_id) == (2, s)


def test_a_fill_session_follows_its_application_through_a_merge(conn):
    a = group(conn)
    s = group(conn)
    application(conn, s)
    sid = session(conn, a, key="greenhouse:acme:4012345", tab_id=4)
    _absorb(conn, s, a, NOW, MergeResult())
    r = m(conn, GH_A, 4, sid)
    assert (r.decided, r.rule, r.group_id) == (True, 1, s)


def test_pick_on_a_generic_page_remembers_nothing(conn):
    a = group(conn)
    p = pick(conn, GENERIC, a, now=NOW)
    assert (p.written, p.posting_key) == (False, None)
    assert conn.execute("SELECT COUNT(*) FROM ats_posting_ref").fetchone()[0] == 0


def test_the_newest_pick_wins_and_names_the_matching_job(conn):
    a = group(conn)
    b = group(conn)
    jb = add_job(conn, b, GH_A + "/apply")
    pick(conn, GH_A, a, now=NOW)
    p = pick(conn, GH_A, b, set_by="add", now=NOW)
    assert p.job_id == jb
    row = conn.execute("SELECT job_id, set_by FROM ats_posting_ref").fetchall()
    assert [tuple(r) for r in row] == [(jb, "add")]
    assert m(conn, GH_A, 1).group_id == b


def test_pick_refuses_an_unknown_group_or_source(conn):
    with pytest.raises(page_match.PickError):
        pick(conn, GH_A, 999, now=NOW)
    a = group(conn)
    with pytest.raises(page_match.PickError):
        pick(conn, GH_A, a, set_by="page", now=NOW)


# ─── rule 3: stored URLs ───────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["url", "apply_url", "page_url", "apply_link_final",
                                   "apply_link_start", "board_ref"])  # fmt: skip
def test_rule3_one_group_with_the_posting_key_decides(conn, where):
    embed = "https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345"
    other = group(conn)
    if where in ("url", "apply_url", "page_url"):
        gid = group(conn, **({"url": embed} if where == "url" else {where: embed}))
    else:
        gid = group(conn)
        if where.startswith("apply_link"):
            start, final = (
                (embed, None) if where == "apply_link_start" else ("https://x.example/r", embed)
            )
            conn.execute(
                "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, "
                "resolved_at) VALUES (?, ?, ?, '[]', 'live', ?)",
                (gid, start, final, ISO),
            )
        else:
            jid = conn.execute(
                "SELECT canonical_job_id FROM job_group WHERE id = ?", (gid,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO job_board_ref (board, board_id, job_id, apply_url, seen_at) "
                "VALUES ('linkedin', '4099999999', ?, ?, ?)",
                (jid, embed, ISO),
            )
    r = m(conn, "https://job-boards.greenhouse.io/acme/jobs/4012345", 1)
    assert (r.decided, r.rule, r.group_id) == (True, 3, gid)
    assert other != gid


def test_rule3_two_groups_ask(conn):
    a = group(conn, GH_A)
    b = group(conn, "https://example.com/y", apply_url=GH_A + "#app")
    r = m(conn, GH_A, 1)
    assert not r.decided and r.rule == 5
    assert {c.group_id for c in r.candidates} == {a, b}


def test_rule3_a_needle_hit_with_another_key_does_not_match(conn):
    # The job id appears in another board's URL and in an unrelated page: no match.
    group(conn, "https://boards.greenhouse.io/other/jobs/4012345")
    group(conn, "https://careers.acme.example/jobs?id=4012345")
    assert not m(conn, GH_A, 1).decided


def test_employer_and_title_never_match(conn):
    group(conn, "https://careers.acme.example/jobs/1", title="Synthetic Engineer",
          employer="Acme Synthetic")  # fmt: skip
    r = m(conn, GH_A, 1)
    assert not r.decided and r.candidates == []


def test_rule3_fill_this_page_matches_page_url_through_normalize(conn):
    gid = group(conn, "https://board.example/job/1",
                page_url="https://careers.acme.example/jobs/77/apply/")  # fmt: skip
    r = m(conn, GENERIC + "?utm_campaign=x", 1)
    assert (r.decided, r.rule, r.group_id, r.kind) == (True, 3, gid, "generic")
    assert r.identifies_posting and r.posting_key is None


def test_rule3_fill_this_page_on_a_careers_home_asks(conn):
    home = "https://careers.acme.example/"
    gid = group(conn, "https://board.example/job/1", page_url=home)
    r = m(conn, home, 1)
    assert not r.decided and not r.identifies_posting
    assert [c.group_id for c in r.candidates] == [gid]


def test_rule3_fill_this_page_with_two_groups_asks(conn):
    group(conn, GENERIC)
    group(conn, "https://board.example/job/2", apply_url=GENERIC)
    r = m(conn, GENERIC, 1)
    assert not r.decided and len(r.candidates) == 2


# ─── rule 4: a recent Open application click ───────────────────────────────


def click(conn, gid, url, at):
    conn.execute(
        "INSERT INTO apply_click (job_group_id, at, final_url, outcome) VALUES (?, ?, ?, NULL)",
        (gid, at.isoformat(), url),
    )


def test_rule4_one_click_in_24_hours_decides(conn):
    gid = group(conn)
    click(conn, gid, GH_A + "?gh_src=x", NOW - timedelta(hours=23))
    r = m(conn, GH_A + "#app", 1)
    assert (r.decided, r.rule, r.group_id) == (True, 4, gid)


def test_rule4_an_old_click_or_two_groups_do_not_decide(conn):
    a = group(conn)
    click(conn, a, GH_A, NOW - timedelta(hours=25))
    assert not m(conn, GH_A, 1).decided
    b = group(conn)
    click(conn, a, GH_A, NOW - timedelta(hours=1))
    click(conn, b, GH_A, NOW - timedelta(hours=2))
    r = m(conn, GH_A, 1)
    assert not r.decided and {c.group_id for c in r.candidates} == {a, b}


def test_result_names_ats_variant_and_application(conn):
    gid = group(conn, f"https://jobs.lever.co/acme/{U1}")
    app = application(conn, gid)
    r = m(conn, f"https://jobs.lever.co/acme/{U1}/apply", 2)
    assert (r.ats, r.variant, r.posting_key) == ("lever", "lever", f"lever:acme:{U1}")
    assert r.application_id == app
    assert [c.title for c in r.candidates] == ["Engineer"]
