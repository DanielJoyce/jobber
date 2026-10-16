"""Apply links from the posting itself (bug 3bab68d and the review of PR 188): description
hints as start URLs, and the walk following a job board copy's Apply button. MockTransport
through FetchContext only; shapes from the user's real data (NLx, Florida, Maine, South Dakota,
Utah)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel
from test_applylink import GH, NOW, FakeClock, Server

from jobhunter.config import Settings
from jobhunter.console.detail import chain_text
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state
from jobhunter.core.models import ApplyStatus
from jobhunter.pipeline import apply_pass
from jobhunter.pipeline.applylink import (
    WALK_VERSION,
    Deferred,
    get_apply_link,
    resolve_group,
    resolve_pending,
    resolve_url,
)

FIX = Path(__file__).parent.parent / "fixtures" / "applylink"
GUID = "186B454139E84E9EB77922F3507BEF9A"
REDIRECTOR = f"https://nlx.jobsyn.org/{GUID}1"
NLX_PAGE = f"https://usnlx.com/san-francisco-ca/senior-software-engineer/{GUID}/job/"
DEJOBS_PAGE = f"https://mongodb.dejobs.org/san-francisco-ca/senior-software-engineer/{GUID}/job/"
DETAIL_JSON = f"https://microsites.dejobs.org/ALL_JOBS/{GUID}.json"
DE_LINK = f"https://de.jobsyn.org/{GUID}10"
EMPLOYER_POSTING = "https://www.mongodb.com/careers/job/"
WD_OWN = (
    "https://mongodb.wd5.myworkdayjobs.com/en-US/MongoDB/job/San-Francisco/"
    "Senior-Software-Engineer_R-8189"
)
UTAH = "https://jobs.utah.gov/jsp/utjobs/single-job"
SHELL = (FIX / "dejobs_shell.html").read_text()


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def factory(tmp_path, conn, server):
    settings = Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache")}, "fetch": {"max_retries": 0}}
    )
    clock = FakeClock()

    def make(url: str):
        return fetch_context_for_url(
            settings, conn, url, transport=server.transport, clock=clock, sleep=clock.sleep
        )

    return make


@pytest.fixture
def resolve(factory):
    def run(url: str):
        ctx = factory(url)
        try:
            return resolve_url(url, ctx)
        finally:
            ctx.close()

    return run


def add_member_group(conn, *members: dict, method: str = "exact_hash") -> int:
    """members: dicts with url, apply_url, description_raw, employer, title; first canonical."""
    gid = conn.execute(
        "INSERT INTO job_group (member_count, method, created_at) VALUES (?, ?, ?)",
        (len(members), method, NOW.isoformat()),
    ).lastrowid
    for i, m in enumerate(members):
        jid = conn.execute(
            "INSERT INTO job (source_key, external_id, job_group_id, url, title, apply_url, "
            "employer, description_raw, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.get("source", "wa"),
                m.get("external_id", f"g{gid}-{i}"),
                gid,
                m["url"],
                m.get("title", "Senior Software Engineer"),
                m.get("apply_url"),
                m.get("employer", "MongoDB"),
                m.get("description_raw"),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        ).lastrowid
        if i == 0:
            conn.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (jid, gid))
    return gid


def json_route(server, url: str, fixture: str = "dejobs_detail.json") -> None:
    server.route(
        url,
        httpx.Response(
            200,
            text=(FIX / fixture).read_text(),
            headers={"content-type": "application/json"},
        ),
    )


def serve_nlx_chain(server, fixture: str = "dejobs_detail.json") -> None:
    """The real NLx shape: redirector -> dejobs.org shell page; its Apply button is the
    detail JSON's ``link``, which redirects to the employer."""
    server.redirect(REDIRECTOR, DEJOBS_PAGE, 301)
    server.page(DEJOBS_PAGE, SHELL)
    server.page(NLX_PAGE, SHELL)
    json_route(server, DETAIL_JSON, fixture)
    server.redirect(DE_LINK, EMPLOYER_POSTING + "?gh_jid=8189170", 301)
    server.page(EMPLOYER_POSTING)


def nlx_group(conn, description: str | None = None, **kw) -> int:
    return add_member_group(
        conn,
        {"url": NLX_PAGE, "apply_url": REDIRECTOR, "description_raw": description, **kw},
    )


# ─── description hints as start URLs ───────────────────────────────────────


def test_hint_naming_the_job_wins_without_the_redirector_walk(conn, factory, server):
    server.page(WD_OWN)
    gid = nlx_group(conn, f"<p>Great job.</p><p>[Apply here]({WD_OWN})</p>")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url, link.start_url) == (
        ApplyStatus.live,
        WD_OWN + "/apply",
        WD_OWN,
    )
    assert "from a link in the posting's text" in (link.why or "")
    assert not link.from_hint
    assert "nlx.jobsyn.org" not in server.hosts() and "usnlx.com" not in server.hosts()


def test_ats_posting_for_another_job_is_not_a_start(conn, factory, server):
    """iHeartMedia's footer names a talent-pool requisition: never this job's link, and the
    job's own walk reaches its real posting."""
    other = (
        "https://mongodb.wd5.myworkdayjobs.com/en-US/MongoDB/job/Austin/"
        "Data-Platform-Engineer_R-7777"
    )
    serve_nlx_chain(server)
    gid = nlx_group(conn, f"<p>Also hiring: {other}</p><p>Apply for that one at {other}</p>")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
    assert "mongodb.wd5.myworkdayjobs.com" not in server.hosts()


def test_sibling_never_hides_the_jobs_own_closed_posting(conn, factory, server):
    """Florida 'Attorney': its own posting closed, a stale template paragraph names another
    posting. The link says closed, not the other job."""
    own = "https://www.governmentjobs.com/jobs/5449533-0/attorney"
    other = "https://www.governmentjobs.com/careers/fleg/jobs/4754958/journal-writer-editor"
    desc = (
        f'<p>Do not click the Apply button. Apply at <a href="{own}">GovernmentJobs.com</a></p>'
        "<p>Submission of Application</p><p>Interested parties may apply with cover letter and "
        f'resume via <a href="{other}">GovernmentJobs.com</a></p>'
    )
    page = "https://jobs.myflorida.com/job/TALLAHASSEE-Attorney-FL-32301/1419779900/"
    server.page(own, status=404)
    server.page(other)
    server.page(page)
    gid = add_member_group(
        conn, {"url": page, "title": "Attorney", "employer": "Florida", "description_raw": desc}
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.expired, own)
    assert "4754958" not in " ".join(server.hosts() + [r.url.path for r in server.requests])


def test_sibling_requisition_on_the_same_ats_never_replaces_the_own_one(conn, factory, server):
    """Maine 'Revenue Agent' R26-03941 is closed; its text lists the other Revenue Agent
    requisitions. The stored link is the closed own one, never R26-03937."""
    base = "https://maine.wd5.myworkdayjobs.com/en-US/Executive/details/Revenue-Agent_"
    own, sibling = base + "R26-03941", base + "R26-03937"
    server.page(own, status=404)
    server.page(sibling)
    gid = add_member_group(
        conn,
        {
            "url": own,
            "apply_url": own,
            "title": "Revenue Agent",
            "employer": "State of Maine",
            "description_raw": f"<p>To apply for the other Revenue Agent position: {sibling}</p>",
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.expired, own)
    assert all("03937" not in r.url.path for r in server.requests)


INFOR = "https://gen-stateofsouthdako-trn.inforcloudsuite.com/hcm/xmlhttp/shorturl.do?key=4XQ"
SD_DESC = (
    "Class A retirement under SDRS.<br> <br>Must apply online: <b>"
    + INFOR
    + "</b> <br> You must apply online."
)


def test_cued_ats_posting_is_a_fallback_when_nothing_else_reached_the_employer(
    conn, factory, server
):
    """South Dakota: the board page names its Infor application. The job's own start reaches
    no ATS, so the Infor posting wins, marked from_hint."""
    page = "https://statesdgovt.hireclick.com/jb/dci-special-agent/view/242857"
    server.page(page)
    server.page(INFOR.split("?")[0])
    gid = add_member_group(
        conn,
        {
            "url": page,
            "title": "DCI Special Agent",
            "employer": "State of South Dakota",
            "description_raw": SD_DESC,
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url, link.ats) == (ApplyStatus.live, INFOR, "infor")
    assert link.from_hint
    assert get_apply_link(conn, gid).from_hint
    assert "from a link in the posting's text" in (link.why or "")
    assert server.hosts()[0] == "statesdgovt.hireclick.com"  # own start first


def test_fallback_finals_never_merge_groups(conn, factory, server):
    """Two postings whose text names the same cued posting both fall back to it; the nightly
    merge must not fold the two jobs together."""
    server.page(INFOR.split("?")[0])
    gids = []
    for i, title in enumerate(("Welder", "Accountant")):
        page = f"https://statesdgovt.hireclick.com/jb/{title.lower()}/view/{900 + i}"
        server.page(page.rsplit("/", 1)[0] + f"/{900 + i}")
        gids.append(
            add_member_group(
                conn,
                {
                    "url": page,
                    "title": title,
                    "employer": "State of South Dakota",
                    "description_raw": SD_DESC,
                },
            )
        )
    done = resolve_pending(conn, now=NOW, ctx_factory=factory, groups=gids)
    assert [lk.final_url for lk in done] == [INFOR, INFOR]
    groups = {r[0] for r in conn.execute("SELECT job_group_id FROM job")}
    assert groups == set(gids)


def test_fallback_is_not_tried_when_the_own_start_failed_transiently(conn, factory, server):
    page = "https://statesdgovt.hireclick.com/jb/dci-special-agent/view/242857"
    server.route(page, httpx.Response(503))
    server.page(INFOR.split("?")[0])
    gid = add_member_group(
        conn, {"url": page, "title": "DCI Special Agent", "description_raw": SD_DESC}
    )
    with pytest.raises(Deferred):
        resolve_group(conn, gid, now=NOW, ctx_factory=factory, defer_transient=True)
    assert get_apply_link(conn, gid) is None
    assert "gen-stateofsouthdako-trn.inforcloudsuite.com" not in server.hosts()


def test_ats_list_page_named_in_text_is_never_a_start(conn, factory, server):
    """'To apply, visit <ATS careers home>': the job's own walk reaches the real posting."""
    home = "https://acme.bamboohr.com/careers"
    own = "https://acme.bamboohr.com/careers/100"
    server.redirect(REDIRECTOR, own)
    server.page(own)
    server.page(home)
    gid = nlx_group(conn, f"<p>To apply, visit {home}</p>", employer="Acme", title="Welder")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == own
    assert server.hosts().count("acme.bamboohr.com") == 1


def test_employer_careers_home_is_not_a_start(conn, factory, server):
    """OCLC: 'Apply online at www.oclc.org'. A home page that does not show the job never
    replaces the board copy."""
    serve_nlx_chain(server)
    server.route(DETAIL_JSON, httpx.Response(404))
    server.page("https://www.mongodb.com/")
    gid = nlx_group(conn, "<p>Apply online at www.mongodb.com</p>")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == DEJOBS_PAGE
    assert "www.mongodb.com" not in server.hosts()


def test_employer_page_naming_the_job_is_a_start(conn, factory, server):
    page = "https://www.mongodb.com/careers/senior-software-engineer"
    serve_nlx_chain(server)
    server.route(DETAIL_JSON, httpx.Response(404))
    server.page(page)
    gid = nlx_group(conn, f"<p>Apply online at {page}</p>")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.final_url, link.status) == (page, ApplyStatus.live)
    assert "employer's site" in (link.why or "")


def test_other_hosts_and_trackers_in_text_are_not_starts(conn, factory, server):
    """Only an ATS or the employer's own site: a staffing agency's page or a tracker named
    for the employer never stands in for the job."""
    serve_nlx_chain(server)
    server.route(DETAIL_JSON, httpx.Response(404))
    desc = (
        "<p>Apply at https://www.acmestaffing.com/jobs/senior-software-engineer</p>"
        "<p>Apply online: https://mongodb.jobs2web.com/job/senior-software-engineer</p>"
    )
    gid = nlx_group(conn, desc)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == DEJOBS_PAGE
    assert not {"www.acmestaffing.com", "mongodb.jobs2web.com"} & set(server.hosts())


def test_wrapped_hint_is_graded_and_stored_unwrapped(conn, factory, server):
    """Florida: 'Apply at' an Outlook safelink around the GovernmentJobs posting. The start is
    the posting itself; the wrapper (whose data= names a sender) is never stored."""
    posting = "https://www.governmentjobs.com/jobs/5438496-0/network-administrator"
    wrapped = str(
        httpx.URL(
            "https://gcc02.safelinks.protection.outlook.com/",
            params={"url": posting, "data": "05|02|<you>@example.com|x"},
        )
    )
    page = "https://jobs.myflorida.com/job/TALLAHASSEE-Network-Administrator/1/"
    server.page(page)
    server.page(posting)
    gid = add_member_group(
        conn,
        {
            "url": page,
            "title": "Network Administrator",
            "employer": "Florida",
            "description_raw": f'<p>Apply at <a href="{wrapped}">GovernmentJobs.com</a></p>',
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.start_url == posting
    stored = conn.execute("SELECT start_url, chain FROM apply_link").fetchone()
    assert "example.com" not in stored[0] + stored[1]


def test_malformed_href_in_a_description_does_not_lose_the_group(conn, factory, server):
    server.page(GH)
    gid = add_member_group(
        conn,
        {
            "url": GH,
            "apply_url": GH,
            "description_raw": '<p>Great job.</p><p><a href="http://[object Object]">Share</a></p>',
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.live, GH + "#app")


def test_three_hints_kept_after_filtering(conn, factory, server):
    """Unusable links (boards, other hosts) do not use up the three hint slots."""
    serve_nlx_chain(server)
    server.route(DETAIL_JSON, httpx.Response(404))
    server.page(WD_OWN)
    desc = (
        "<p>Apply at https://www.monster.com/job/1</p>"
        "<p>Apply at https://www.ziprecruiter.com/job/1</p>"
        "<p>Apply at https://www.acmestaffing.com/job/1</p>"
        f"<p>Apply at {WD_OWN}</p>"
    )
    gid = nlx_group(conn, desc)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.start_url == WD_OWN


def test_hint_never_outranks_the_jobs_own_ats_result(conn, factory, server):
    """The job's own Workday posting is closed; its text names the same requisition at another
    URL that answers live. The job's own (closed) result stands."""
    own = WD_OWN
    hint = WD_OWN.replace("/job/San-Francisco/", "/details/")
    server.page(own, status=404)
    server.page(hint)
    gid = add_member_group(
        conn, {"url": own, "apply_url": own, "description_raw": f"<p>[Apply]({hint})</p>"}
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.expired, own)


def test_two_links_both_naming_the_job_are_only_fallbacks(conn, factory, server):
    """Florida template text names two 'attorney' postings: neither is trusted as this job's,
    so both wait until the job's own start is tried."""
    a = "https://www.governmentjobs.com/jobs/5445693-0/attorney"
    b = "https://www.governmentjobs.com/jobs/5513477-0/attorney"
    page = "https://jobs.myflorida.com/job/TALLAHASSEE-Attorney/77/"
    server.page(page)
    server.page(a)
    server.page(b)
    gid = add_member_group(
        conn,
        {
            "url": page,
            "title": "Attorney",
            "employer": "Florida",
            "description_raw": f"<p>Apply at {a}</p><p>Or apply at {b}</p>",
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert server.hosts()[0] == "jobs.myflorida.com"
    assert link.from_hint and link.final_url == a + "/apply"


def test_uncued_posting_for_another_job_is_never_a_fallback(conn, factory, server):
    page = "https://jobs.myflorida.com/job/TALLAHASSEE-Attorney/78/"
    other = "https://www.governmentjobs.com/jobs/4754958/journal-writer-editor"
    server.page(page)
    server.page(other)
    gid = add_member_group(
        conn,
        {
            "url": page,
            "title": "Attorney",
            "employer": "Florida",
            "description_raw": f"<p>See also {other}</p>",
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == page
    assert server.hosts() == ["jobs.myflorida.com"]


# ─── the walk follows a board copy's Apply button ──────────────────────────


def test_nlx_redirector_continues_through_the_board_copy_apply_button(resolve, server):
    serve_nlx_chain(server)
    res = resolve(REDIRECTOR)
    assert res.status == ApplyStatus.live
    assert res.final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
    assert [(h.method, h.via, h.host, h.status) for h in res.chain] == [
        ("3xx", None, "mongodb.dejobs.org", 200),
        ("meta", "button", "de.jobsyn.org", 301),
        ("3xx", None, "www.mongodb.com", 200),
    ]
    assert "microsites.dejobs.org" in server.hosts()


def test_state_bank_application_link_is_skipped_for_the_jobsyn_link(conn, factory, server):
    """NLx copies of Utah job bank postings carry other.application_link on jobs.utah.gov (a
    board): the button walk takes the de.jobsyn.org link to the employer instead."""
    serve_nlx_chain(server, "dejobs_detail_state_bank.json")
    gid = nlx_group(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
    assert "jobs.utah.gov" not in server.hosts()


def test_button_into_a_login_wall_keeps_the_live_board_copy(conn, factory, server):
    """de.jobsyn.org sends a Utah posting to jobs.utah.gov, which 302s to its sign-in: the
    stored link stays the live board copy, never a blocked row that is never retried."""
    serve_nlx_chain(server, "dejobs_detail_state_bank.json")
    server.redirect(DE_LINK, UTAH + "?j=11129904", 301)
    server.redirect(UTAH, "https://jobs.utah.gov/jsp/utjobs/login.do?loginReturnUrl=x")
    gid = nlx_group(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.live, DEJOBS_PAGE)
    assert not link.chain[-1].via


@pytest.mark.parametrize(
    "setup",
    [
        lambda s: s.page(DE_LINK, "gone", status=404),
        lambda s: s.route(
            "https://de.jobsyn.org/robots.txt",
            httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
        ),
        # de.jobsyn.org sends the button to another job board: still a board copy
        lambda s: (
            s.redirect(DE_LINK, "https://www.monster.com/job/8189170", 301),
            s.page("https://www.monster.com/job/8189170"),
        ),
    ],
    ids=["target-404", "target-robots-disallowed", "target-another-board"],
)
def test_button_walk_that_fails_keeps_the_live_board_copy(conn, factory, server, setup):
    serve_nlx_chain(server)
    setup(server)
    gid = nlx_group(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.live, DEJOBS_PAGE)


@pytest.mark.parametrize(
    "setup",
    [
        lambda s: s.route(DETAIL_JSON, httpx.Response(429)),
        lambda s: s.route(DETAIL_JSON, httpx.Response(503)),
        lambda s: s.route("https://microsites.dejobs.org/robots.txt", httpx.Response(503)),
        lambda s: s.route(DE_LINK, httpx.Response(503)),
    ],
    ids=["json-429", "json-503", "json-robots-503", "target-503"],
)
def test_passing_failure_after_the_board_defers_the_nightly_pass(conn, factory, server, setup):
    serve_nlx_chain(server)
    setup(server)
    gid = nlx_group(conn)
    with pytest.raises(Deferred):
        resolve_group(conn, gid, now=NOW, ctx_factory=factory, defer_transient=True)
    assert get_apply_link(conn, gid) is None
    # On the job page (no deferral) the board copy is stored.
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.live, DEJOBS_PAGE)


def test_one_button_walk_per_group(conn, factory, server):
    """The redirector start and the usnlx.com page start share the posting's JSON and its
    button walk: each is requested once."""
    serve_nlx_chain(server)
    server.page(EMPLOYER_POSTING)
    gid = nlx_group(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
    hosts = server.hosts()
    assert hosts.count("microsites.dejobs.org") == 1
    assert hosts.count("de.jobsyn.org") == 1
    assert hosts.count("www.mongodb.com") == 1


def test_usajobs_posting_is_not_walked_past(conn, factory, server):
    """A USAJOBS posting is the federal apply entry point; its questionnaire preview link is
    never taken for the Apply button."""
    page = "https://www.usajobs.gov/job/888019500"
    q = "https://apply.usastaffing.gov/ViewQuestionnaire/13069503"
    server.page(
        page,
        "<html><body><h1>IT Specialist</h1>"
        f'<p>Preview application questionnaire before you apply: <a href="{q}">{q}</a></p>'
        f'<p><a class="usajobs-apply" href="{GH}">Apply</a></p></body></html>',
    )
    server.page(GH)
    gid = add_member_group(conn, {"url": page, "apply_url": page, "employer": "Department of X"})
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.final_url, link.chain) == (page, [])
    assert server.hosts() == ["www.usajobs.gov"]


def test_board_page_with_off_site_apply_anchor(resolve, server):
    board = "https://jobs.utah.gov/jsp/utjobs/single-job?j=7"
    server.page(
        board.split("?")[0],  # the mock routes ignore the query
        '<html><body><h1>Engineer</h1><a href="/login">Sign in</a>'
        f'<a class="btn-apply" href="{GH}">Apply for this job</a></body></html>',
    )
    server.page(GH)
    res = resolve(board)
    assert [(h.via, h.url) for h in res.chain] == [("button", GH)]
    assert (res.status, res.final_url, res.ats) == (ApplyStatus.live, GH + "#app", "greenhouse")


def test_qualifying_button_after_three_others_is_found(resolve, server):
    board = "https://jobs.utah.gov/jobs/3"
    server.page(
        board,
        '<a href="https://www.monster.com/job/3">Apply on Monster</a>'
        '<a href="https://www.ziprecruiter.com/job/3">Apply on ZipRecruiter</a>'
        '<a href="https://snap.example.gov/apply">Apply for food assistance</a>'
        f'<a href="{GH}">Apply on employer site</a>',
    )
    server.page(GH)
    res = resolve(board)
    assert res.final_url == GH + "#app"


def test_board_links_that_are_not_the_posting_are_not_followed(resolve, server):
    board = "https://jobs.utah.gov/jobs/2"
    server.page(
        board,
        '<a href="https://jobs.utah.gov/ui/apply">Apply for this job</a>'
        '<a href="https://www.monster.com/job/2">Apply on Monster</a>'
        '<a href="https://snap.example.gov/apply">Apply for food assistance</a>'
        '<a href="https://www.acme.com/accommodations">Request an accommodation to apply</a>',
    )
    res = resolve(board)
    assert (res.status, res.final_url, res.chain) == (ApplyStatus.live, board, [])
    assert server.hosts() == ["jobs.utah.gov"]


def test_login_wall_button_is_not_followed(resolve, server):
    """A button straight to an ATS's sign-in page, or to a tracker that 302s to one: the board
    copy stays live."""
    board = "https://jobs.utah.gov/jobs/4"
    login = "https://acme.wd5.myworkdayjobs.com/en-US/acme/login"
    server.page(board, f'<a href="{login}">Apply now</a>')
    res = resolve(board)
    assert (res.status, res.final_url, res.chain) == (ApplyStatus.live, board, [])
    assert "acme.wd5.myworkdayjobs.com" not in server.hosts()

    board2 = "https://jobs.utah.gov/jobs/5"
    tracker = "https://www.aplitrak.com/t/7"
    server.page(board2, f'<a href="{tracker}">Apply now</a>')
    server.redirect(tracker, "https://sso.acme.com/login")
    res = resolve(board2)
    assert (res.status, res.final_url, res.login) == (ApplyStatus.live, board2, False)


def test_malformed_anchor_on_a_board_page_does_not_raise(resolve, server):
    board = "https://jobs.utah.gov/jobs/9"
    server.page(board, f'<a href="http://[object Object]">Print</a><a href="{GH}">Apply</a>')
    server.page(GH)
    assert resolve(board).final_url == GH + "#app"


def test_button_back_to_a_page_already_seen_is_not_followed(resolve, server):
    tracker = "https://www.aplitrak.com/t/1"
    board = "https://jobs.utah.gov/jobs/1"
    server.redirect(tracker, board)
    server.page(board, f'<a href="{tracker}">Apply</a>')
    res = resolve(tracker)
    assert [h.url for h in res.chain] == [board]
    assert (res.status, res.final_url) == (ApplyStatus.live, board)


def test_employer_page_apply_link_is_not_followed(resolve, server):
    page = "https://careers.acme.com/jobs/1"
    server.page(page, f'<a href="{GH}">Apply</a>')
    res = resolve(page)
    assert (res.chain, res.final_url) == ([], page)
    assert "boards.greenhouse.io" not in server.hosts()


def test_class_a_source_host_counts_as_a_board(conn, factory, server):
    """A state job bank known only from the source table (not AGGREGATOR_HOSTS) is a board:
    its Apply button is followed when the group's host classes are used."""
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('wy', 'A', 'WY', 'htmlconfig', 'http', 'https://hire.example-wy.gov/', 'enabled')"
    )
    page = "https://hire.example-wy.gov/jobs/1"
    server.page(page, f'<a href="{GH}">Apply</a>')
    server.page(GH)
    gid = add_member_group(conn, {"url": page, "source": "wy", "employer": "Acme"})
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == GH + "#app"


def test_chain_text_names_the_apply_button(conn, factory, server):
    serve_nlx_chain(server)
    gid = nlx_group(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
    assert "de.jobsyn.org (Apply button)" in chain_text(link)


class _OldApplyHop(BaseModel):
    """ApplyHop as a console built before bug 3bab68d had it."""

    url: str
    host: str
    method: Literal["unwrap", "3xx", "meta", "js", "browser"]
    status: int | None = None


def test_an_older_console_can_read_a_button_hop(conn, factory, server):
    serve_nlx_chain(server)
    gid = nlx_group(conn)
    resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    chain = json.loads(conn.execute("SELECT chain FROM apply_link").fetchone()[0])
    hops = [_OldApplyHop.model_validate(h) for h in chain]
    assert [h.host for h in hops][-2:] == ["de.jobsyn.org", "www.mongodb.com"]
    assert [h.get("via") for h in chain] == [None, "button", None]


# ─── review round 2 of PR 188 ──────────────────────────────────────────────

OTHER_OPENING = (
    "https://mongodb.wd5.myworkdayjobs.com/en-US/MongoDB/job/Austin/Product-Analyst_R-1300"
)


def test_own_posting_closed_through_the_button_makes_the_job_closed(conn, factory, server):
    """Decision: the button walk reaching the job's own posting closed on the employer's site
    is a real answer. The job is stored closed, and a cued link to another opening in its
    text is never tried."""
    serve_nlx_chain(server)
    server.page(EMPLOYER_POSTING, "<p>This job is no longer available.</p>")
    server.page(OTHER_OPENING)
    gid = nlx_group(conn, f"<p>To apply for our Product Analyst opening: {OTHER_OPENING}</p>")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.status == ApplyStatus.expired
    assert link.final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
    assert link.chain[-2].via == "button"
    assert "mongodb.wd5.myworkdayjobs.com" not in server.hosts()


def test_fallback_never_replaces_a_live_board_copy(conn, factory, server):
    """The button walk fails (the de.jobsyn.org link is gone): the board copy of the right
    posting beats a cued link to another opening."""
    serve_nlx_chain(server)
    server.page(DE_LINK, "gone", status=404)
    server.page(OTHER_OPENING)
    gid = nlx_group(conn, f"<p>To apply for our Product Analyst opening: {OTHER_OPENING}</p>")
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url, link.from_hint) == (ApplyStatus.live, DEJOBS_PAGE, False)
    assert "mongodb.wd5.myworkdayjobs.com" not in server.hosts()


def test_own_posting_behind_a_sign_in_still_blocks_the_fallback(conn, factory, server):
    """Florida District 35: the job's own posting 302s to an off-ATS sign-in. Its District 36
    sibling (a cued link in the same text) is never stored instead."""
    own = "https://www.governmentjobs.com/jobs/5445206-0/district-legislative-aide-district-35"
    sib = "https://www.governmentjobs.com/careers/fleg/jobs/4910438/district-legislative-aide-district-36"
    page = "https://jobs.myflorida.com/job/HOLLYWOOD-District-Legislative-Aide/9/"
    server.page(page)
    server.redirect(own, "https://login.example-idp.com/sso/login")
    server.page(sib)
    desc = f"<p>Apply at {own}</p><p>Interested parties may apply via {sib}</p>"
    gid = add_member_group(
        conn,
        {
            "url": page,
            "title": "District Legislative Aide (District 35)",
            "employer": "Florida",
            "description_raw": desc,
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert "4910438" not in (link.final_url or "")
    assert all("4910438" not in r.url.path for r in server.requests)


def test_fallback_landing_on_an_ats_list_page_is_not_kept(conn, factory, server):
    """South Dakota: an Infor short link that lands on the Infor job board (not a posting)."""
    page = "https://statesdgovt.hireclick.com/jb/dci-special-agent/view/242857"
    board = "https://gen-stateofsouthdako-trn.inforcloudsuite.com/hcm/Jobs/form/JobBoard"
    server.page(page)
    server.redirect(INFOR.split("?")[0], board)
    server.page(board)
    gid = add_member_group(
        conn, {"url": page, "title": "DCI Special Agent", "description_raw": SD_DESC}
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.final_url, link.from_hint) == (page, False)


def test_closed_fallback_never_marks_the_job_closed(conn, factory, server):
    page = "https://statesdgovt.hireclick.com/jb/dci-special-agent/view/242857"
    server.page(page)
    server.page(INFOR.split("?")[0], status=404)
    gid = add_member_group(
        conn, {"url": page, "title": "DCI Special Agent", "description_raw": SD_DESC}
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.final_url) == (ApplyStatus.live, page)


def test_fallback_failing_transiently_defers_the_nightly_pass(conn, factory, server):
    page = "https://statesdgovt.hireclick.com/jb/dci-special-agent/view/242857"
    server.page(page)
    server.route(INFOR.split("?")[0], httpx.Response(503))
    gid = add_member_group(
        conn, {"url": page, "title": "DCI Special Agent", "description_raw": SD_DESC}
    )
    with pytest.raises(Deferred):
        resolve_group(conn, gid, now=NOW, ctx_factory=factory, defer_transient=True)
    assert get_apply_link(conn, gid) is None


def test_ats_list_page_in_text_is_not_a_start_even_when_nothing_else_reaches_the_ats(
    conn, factory, server
):
    page = "https://jobs.myflorida.com/job/TALLAHASSEE-Welder/1/"
    server.page(page)
    server.page("https://acme.bamboohr.com/careers")
    gid = add_member_group(
        conn,
        {
            "url": page,
            "title": "Welder",
            "employer": "Acme",
            "description_raw": "<p>To apply, visit https://acme.bamboohr.com/careers</p>",
        },
    )
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == page
    assert server.hosts() == ["jobs.myflorida.com"]


def test_usa_staffing_link_in_description_is_never_a_start(conn):
    """The real USAJOBS sentence: a questionnaire preview, not the application."""
    from jobhunter.pipeline.apply_rank import HostClasses
    from jobhunter.pipeline.applylink import _candidates

    q = "https://apply.usastaffing.gov/ViewQuestionnaire/13069503"
    gid = add_member_group(
        conn,
        {
            "url": "https://jobs.myflorida.com/job/X/1/",
            "title": "IT Specialist",
            "description_raw": f"<p>Preview application questionnaire before you apply: {q}</p>",
        },
    )
    cands = _candidates(conn, gid, HostClasses.load(conn, gid))
    assert [c.url for c in cands] == ["https://jobs.myflorida.com/job/X/1/"]


def test_usa_staffing_button_on_another_board_is_not_followed(resolve, server):
    board = "https://jobs.utah.gov/jobs/12"
    server.page(board, '<a href="https://apply.usastaffing.gov/Application/Apply?x=1">Apply</a>')
    res = resolve(board)
    assert (res.final_url, res.chain) == (board, [])


def test_fallback_finals_never_merge_across_states(conn, factory, server):
    """Two 'Welder' postings of one employer whose text names the same cued posting: the
    cross-state merge must not join them on that fallback link."""
    from jobhunter.pipeline.dedupe_xstate import merge_cross_state

    server.page(INFOR.split("?")[0])
    gids = []
    for i, text in enumerate(("Welding in Pierre.", "Pipe welding on night shift in Rapid City.")):
        page = f"https://statesdgovt.hireclick.com/jb/welder/view/{700 + i}"
        server.page(page)
        gids.append(
            add_member_group(
                conn,
                {
                    "url": page,
                    "title": "Welder",
                    "employer": "State of South Dakota",
                    "description_raw": f"<p>{text}</p>" + SD_DESC,
                },
            )
        )
    unrelated = (
        "Weld structural steel for bridge repair crews across the eastern district.",
        "Maintain pipelines at the state prison power plant; overnight on-call rotation.",
    )
    for i, gid in enumerate(gids):
        conn.execute(
            "UPDATE job SET description_text = ? WHERE job_group_id = ?", (unrelated[i], gid)
        )
        conn.execute(
            "INSERT INTO job_locations (job_id, state, city, is_primary) "
            "SELECT id, ?, 'Town', 1 FROM job WHERE job_group_id = ?",
            (("SD", "NE")[i], gid),
        )
    assert merge_cross_state(conn, now=NOW, dry_run=True).would_merge == 0  # control
    done = resolve_pending(conn, now=NOW, ctx_factory=factory, groups=gids)
    assert [lk.from_hint for lk in done] == [True, True]
    merge_cross_state(conn, now=NOW)
    assert {r[0] for r in conn.execute("SELECT job_group_id FROM job")} == set(gids)


def test_fallback_finals_are_not_the_jobs_url_for_page_matching(conn):
    from jobhunter.apply import page_match

    gid = add_member_group(conn, {"url": "https://statesdgovt.hireclick.com/jb/w/view/1"})
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at, "
        "from_hint) VALUES (?, ?, ?, '[]', 'live', ?, 1)",
        (gid, INFOR, INFOR, NOW.isoformat()),
    )
    assert page_match._url_rows(conn, "inforcloudsuite.com") == []
    conn.execute("UPDATE apply_link SET from_hint = 0")
    assert page_match._url_rows(conn, "inforcloudsuite.com")


@pytest.mark.parametrize(
    "where",
    ["description", "board", "json"],
)
def test_malformed_wrapped_urls_never_lose_the_group(conn, factory, server, where):
    bad = "https://www.google.com/url?q=http://[object%20Object]"
    if where == "description":
        server.page(GH)
        gid = add_member_group(
            conn,
            {
                "url": GH,
                "apply_url": GH,
                "description_raw": f'<p><a href="{bad}">Apply here</a></p>',
            },
        )
        assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).final_url == GH + "#app"
    elif where == "board":
        board = "https://jobs.utah.gov/jobs/13"
        server.page(board, f'<a href="{bad}">Apply now</a>')
        gid = add_member_group(conn, {"url": board})
        assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).final_url == board
    else:
        serve_nlx_chain(server)
        item = json.loads((FIX / "dejobs_detail.json").read_text())
        item["link"] = "http://[object Object]"
        item["other"] = json.dumps({"application_link": "https://[x"})
        server.route(DETAIL_JSON, httpx.Response(200, json=item))
        gid = nlx_group(conn)
        assert resolve_group(conn, gid, now=NOW, ctx_factory=factory).final_url == DEJOBS_PAGE


@pytest.mark.parametrize("old_version", [1, None])
def test_board_copies_from_an_older_walk_get_fresh_tries(conn, factory, server, old_version):
    """WALK_VERSION 2 (the Apply-button step): a stored NLx board copy whose tries ran out
    under walk 1 is taken again by the nightly pass and reaches the employer. Tries counted by
    the current walk stay used up."""
    from test_apply_pass import PROFILE, add

    assert WALK_VERSION >= 2
    gid = add(conn, 7, skills=90, url=NLX_PAGE)
    conn.execute("UPDATE job SET apply_url = ? WHERE id = ?", (REDIRECTOR, gid))
    at = (NOW - timedelta(days=3)).isoformat()
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at, "
        "verified_at) VALUES (?, ?, ?, '[]', 'live', ?, ?)",
        (gid, REDIRECTOR, DEJOBS_PAGE, at, at),
    )
    conn.execute(
        "INSERT INTO apply_link_attempt (job_group_id, attempts, last_at, walk_version) "
        "VALUES (?, 5, ?, ?)",
        (gid, at, old_version or WALK_VERSION),
    )
    selected = [c.group_id for c in apply_pass.select_groups(conn, PROFILE, now=NOW)]
    if old_version is None:
        assert selected == []
        return
    assert selected == [gid]
    serve_nlx_chain(server)
    settings = Settings.model_validate({"fetch": {"max_retries": 0}})
    apply_pass.resolve_likely(conn, settings, PROFILE, now=NOW, limit=5, ctx_factory=factory)
    assert get_apply_link(conn, gid).final_url == EMPLOYER_POSTING + "?gh_jid=8189170"
