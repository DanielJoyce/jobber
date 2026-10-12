"""Apply-link ranking across a group's members, login walls, capture and user links (bug 334d9eb).

Modeled on a real group: seven aggregator copies of one posting (NLx state mirrors, a state job
bank, a state WorkSource site). One copy's apply link is the Utah job bank, which redirects to a
state single-sign-on page; two copies' apply links are aplitrak.com redirects that end on the
employer's Ashby posting. Synthetic employer and ids. httpx.MockTransport: no network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jobhunter.apply import capture
from jobhunter.config import Settings
from jobhunter.core.fetch import fetch_context_for_url, reset_shared_state
from jobhunter.core.models import ApplyStatus
from jobhunter.pipeline import applylink
from jobhunter.pipeline.apply_rank import HostClasses, HostTier, employer_keys
from jobhunter.pipeline.applylink import (
    Candidate,
    Resolution,
    clear_user_link,
    get_apply_link,
    rank_results,
    reresolve_login_walls,
    resolve_group,
    resolve_url,
    reverify,
    set_user_link,
)
from jobhunter.pipeline.ats_rules import ats_name, is_login_wall
from jobhunter.pipeline.dedupe_url import _identifies_job, merge_by_apply_url, normalize_apply_url

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "applylink"
LOGIN_HTML = (FIX / "state_login.html").read_text()
ASHBY_HTML = (FIX / "ashby_posting.html").read_text()
OPEN_PAGE = "<html><body><h1>Staff Software Engineer</h1><p>Apply today.</p></body></html>"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

# The chain of group 317's state-bank copy, as stored on the user's database (ids changed).
UT_JOB = "https://jobs.utah.gov/jsp/utjobs/single-job?j=10000001"
UT_LOGIN_DO = (
    "https://jobs.utah.gov/jsp/utjobs/login.do?loginReturnUrl=%2Fjsp%2Futjobs%2Fseeker%2Fhome"
    "%2Fsingle-job%3Fj%3D10000001%26virtual%3Dfalse"
)
UT_SSO = "https://jobs.utah.gov/sso/login.aspx?application=UWORKSSKR&returnurl=%2Fjsp"
DTS_AUTHORIZE = (
    "https://login.dts.utah.gov/sso/oauth2/authorize?response_type=code&client_id=synthetic"
    "&scope=openid%20email&redirect_uri=https%3a%2f%2fjobs.utah.gov%2fsso%2fLogin.aspx"
)
ID_LOGIN = (
    "https://id.utah.gov/login/?goto=https%3A%2F%2Flogin.dts.utah.gov%3A443%2Fsso%2Foauth2"
    "%2Fauthorize%3Fresponse_type%3Dcode%26client_id%3Dsynthetic&realm=/"
)
APLI_A = "https://www.aplitrak.com/?adid=c3ludGhldGljLjEuMUBhY21lLmFwbGl0cmFrLmNvbQ"
APLI_B = "https://www.aplitrak.com/?adid=c3ludGhldGljLjIuMkBhY21lLmFwbGl0cmFrLmNvbQ"
ASHBY = "https://jobs.ashbyhq.com/acme/2f6b8c1e-1d1a-4b7e-9a51-0c6d3f2e8a77"
MONSTER = "http://jobseeker.ohiomeansjobs.monster.com/jobview/GetJob.aspx?JobId=200000001"
MITALENT = "https://jobs.mitalent.org/job-seeker/job-details/400000001"
NLX = "https://usnlx.com/salt-lake-city-ut/staff-software-engineer/AAAA{}/job/"
KYJOBS = "https://kyjobs.usnlx.com/frankfort-ky/staff-software-engineer/AAAA5/job/"
WORKSOURCE = "https://worksource.my.site.com/worksourcewa/job-search/job-details?jobId=a0g1"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Server:
    """Route table over MockTransport, keyed by the full URL (query included, so two aplitrak
    links differ). Unrouted robots.txt is 404; anything else unrouted is 500."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    def redirect(self, src: str, dst: str, status: int = 302) -> None:
        self.routes[str(httpx.URL(src))] = httpx.Response(status, headers={"Location": dst})

    def page(self, url: str, html: str = OPEN_PAGE, status: int = 200) -> None:
        self.routes[str(httpx.URL(url))] = httpx.Response(status, html=html)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        target = self.routes.get(str(request.url))
        if target is None:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(500, text=f"no route for {request.url}")
        return target

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests if r.url.path != "/robots.txt"]

    def pages(self) -> list[str]:
        return [str(r.url) for r in self.requests if r.url.path != "/robots.txt"]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


@pytest.fixture(autouse=True)
def _fresh_shared_state():
    reset_shared_state()
    yield
    reset_shared_state()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.model_validate(
        {"paths": {"cache_dir": str(tmp_path / "cache")}, "fetch": {"max_retries": 0}}
    )


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def factory(settings, conn, server):
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


def add_group(conn, *jobs, employer: str = "Acme", gid: int | None = None) -> int:
    """jobs: (url, apply_url) or (url, apply_url, last_seen_at); the first is canonical."""
    gid = conn.execute(
        "INSERT INTO job_group (id, member_count, method, created_at) "
        "VALUES (?, ?, 'near_dupe', ?)",
        (gid, len(jobs), NOW.isoformat()),
    ).lastrowid
    for i, job in enumerate(jobs):
        url, apply_url = job[0], job[1]
        seen = job[2] if len(job) > 2 else NOW.isoformat()
        jid = conn.execute(
            "INSERT INTO job (source_key, external_id, job_group_id, url, title, employer, "
            "apply_url, first_seen_at, last_seen_at) "
            "VALUES ('wa', ?, ?, ?, 'Staff Software Engineer', ?, ?, ?, ?)",
            (f"{gid}-{i}", gid, url, employer, apply_url, seen, seen),
        ).lastrowid
        if i == 0:
            conn.execute("UPDATE job_group SET canonical_job_id = ? WHERE id = ?", (jid, gid))
    return gid


def utah_chain(server: Server) -> None:
    server.redirect(UT_JOB, UT_LOGIN_DO)
    server.redirect(UT_LOGIN_DO, UT_SSO)
    server.redirect(UT_SSO, DTS_AUTHORIZE)
    server.redirect(DTS_AUTHORIZE, ID_LOGIN)
    server.page(ID_LOGIN, LOGIN_HTML)


def group_317(conn) -> int:
    """Seven copies, canonical first, as on the user's database (URLs made synthetic)."""
    return add_group(
        conn,
        (NLX.format(1), UT_JOB),
        (NLX.format(2), MONSTER),
        (NLX.format(3), MITALENT),
        (NLX.format(4), APLI_A),
        (NLX.format(5), APLI_B),
        (KYJOBS, APLI_B),
        (WORKSOURCE, None),
        employer="Acme, Inc.",
    )


# ─── login walls ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [UT_LOGIN_DO, UT_SSO, DTS_AUTHORIZE, ID_LOGIN,
     "https://accounts.google.com/v3/signin/identifier?continue=x",
     "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x",
     "https://acme.okta.com/app/acme/sso/saml",
     "https://careers.acme.example/signin?returnTo=/jobs/1",
     "https://acme.wd5.myworkdayjobs.com/en-US/Acme/login",
     "https://auth.acme.example/realms/x/protocol/openid-connect/auth?client_id=y"],
)  # fmt: skip
def test_login_wall_urls(url):
    assert is_login_wall(url)


@pytest.mark.parametrize(
    "url",
    [UT_JOB, ASHBY, APLI_A, MONSTER, MITALENT, KYJOBS, WORKSOURCE,
     "https://careers.acme.example/jobs/123/login-systems-engineer",
     "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/Albany-NY/Analyst_R-1234/apply",
     "https://boards.greenhouse.io/acme/jobs/567#app",
     "https://identity.acme.example/careers/42"],
)  # fmt: skip
def test_not_login_walls(url):
    assert not is_login_wall(url)


def test_state_bank_redirect_to_sso_is_blocked_before_the_wall(resolve, server):
    utah_chain(server)
    res = resolve(UT_JOB)
    assert res.status == ApplyStatus.blocked and res.login
    assert res.final_url == UT_JOB  # the page before the wall: what the button can open
    assert (res.error or "").startswith("login wall at jobs.utah.gov")
    assert [h.status for h in res.chain] == [None]  # the wall itself is never requested
    assert server.pages() == [UT_JOB]
    assert not {"login.dts.utah.gov", "id.utah.gov"} & set(server.hosts())


def test_login_form_page_without_login_url_is_a_wall(resolve, server):
    server.redirect("https://jobs.state.example/job/7", "https://portal.state.example/start")
    server.page("https://portal.state.example/start", LOGIN_HTML)
    res = resolve("https://jobs.state.example/job/7")
    assert res.status == ApplyStatus.blocked and res.login
    assert res.final_url == "https://jobs.state.example/job/7"


def test_posting_with_a_sign_in_box_is_not_a_wall(resolve, server):
    body = "<p>" + "Responsibilities include building agent tools. " * 80 + "</p>"
    html = f'<html><body><form><input type="password"></form>{body}</body></html>'
    server.page("https://careers.acme.example/jobs/9", html)
    res = resolve("https://careers.acme.example/jobs/9")
    assert res.status == ApplyStatus.live and not res.login


def test_start_url_that_is_a_login_page_is_never_requested(resolve, server):
    res = resolve(ID_LOGIN)
    assert res.status == ApplyStatus.blocked and res.login and server.requests == []


# ─── the group 317 shape ───────────────────────────────────────────────────


def test_group_317_picks_the_ashby_posting_over_the_state_login(conn, factory, server):
    utah_chain(server)
    server.redirect(APLI_A, ASHBY)
    server.redirect(APLI_B, ASHBY)
    server.page(ASHBY, ASHBY_HTML)
    gid = group_317(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.status, link.ats) == (ApplyStatus.live, "ashby")
    assert link.final_url == ASHBY + "/application"
    assert link.start_url == APLI_A and link.chosen_by is None
    assert link.why is not None
    assert link.why.startswith("via www.aplitrak.com → jobs.ashbyhq.com")
    assert "applicant tracking system" in link.why
    # The tracker was tried first (aggregators last), and a live ATS settles it: the state
    # bank, its SSO and the board copies were never requested.
    assert server.pages() == [APLI_A, ASHBY]
    stored = get_apply_link(conn, gid)
    assert stored is not None and stored.why == link.why


def test_group_317_with_the_tracker_down_still_avoids_the_login_wall(conn, factory, server):
    utah_chain(server)
    for u in (MONSTER, MITALENT, KYJOBS, WORKSOURCE, *(NLX.format(i) for i in range(1, 6))):
        server.page(u)
    gid = group_317(conn)  # aplitrak unrouted: 500, unresolved
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory, max_fetches=20)
    assert link.status != ApplyStatus.blocked and "login" not in (link.error or "")
    assert link.final_url not in (ID_LOGIN, UT_JOB)
    assert not {"login.dts.utah.gov", "id.utah.gov"} & set(server.hosts())


def test_only_a_login_wall_left_is_blocked_with_the_page_before_it(conn, factory, server):
    utah_chain(server)
    gid = add_group(conn, ("https://www.linkedin.com/jobs/view/4100000001/", UT_JOB))
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    # The LinkedIn copy is never fetched and stays unresolved; it still beats the wall.
    assert link.status == ApplyStatus.unresolved
    assert "linkedin.com" in link.final_url
    assert "www.linkedin.com" not in server.hosts()
    gid2 = add_group(conn, (UT_JOB, None))
    link2 = resolve_group(conn, gid2, now=NOW, ctx_factory=factory)
    assert (link2.status, link2.final_url) == (ApplyStatus.blocked, UT_JOB)
    assert link2.why is not None and "login wall" in link2.why


def test_legacy_login_row_is_resolved_again(conn, factory, server):
    """The user's row: status live, final URL the id.utah.gov login page (stored before
    login walls were recognized). The nightly pass replaces it; other rows are untouched."""
    utah_chain(server)
    server.redirect(APLI_A, ASHBY)
    server.page(ASHBY, ASHBY_HTML)
    gid = group_317(conn)
    chain = [
        {"url": UT_LOGIN_DO, "host": "jobs.utah.gov", "method": "3xx", "status": 302},
        {"url": ID_LOGIN, "host": "id.utah.gov", "method": "3xx", "status": 200},
    ]
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, employer_host, "
        "status, resolved_at, verified_at) VALUES (?, ?, ?, ?, 'unknown', 'id.utah.gov', 'live', "
        "?, ?)",
        (gid, UT_JOB, ID_LOGIN, json.dumps(chain), NOW.isoformat(), NOW.isoformat()),
    )
    ok = add_group(conn, (ASHBY, None))
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, ats, status, "
        "resolved_at) VALUES (?, ?, ?, '[]', 'ashby', 'live', ?)",
        (ok, ASHBY, ASHBY + "/application", NOW.isoformat()),
    )
    assert applylink.needs_reresolve(get_apply_link(conn, gid))
    assert applylink.login_wall_groups(conn, 10) == [gid]
    done = reresolve_login_walls(conn, now=NOW, ctx_factory=factory)
    assert [d.job_group_id for d in done] == [gid]
    link = get_apply_link(conn, gid)
    assert link.final_url == ASHBY + "/application" and link.status == ApplyStatus.live
    assert applylink.login_wall_groups(conn, 10) == []  # nothing left to do tomorrow


def test_blocked_login_rows_are_not_fetched_every_night(conn, factory, server):
    utah_chain(server)
    gid = add_group(conn, (UT_JOB, None))
    resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    n = len(server.requests)
    assert reresolve_login_walls(conn, now=NOW, ctx_factory=factory) == []
    assert len(server.requests) == n


def test_login_urls_never_merge_groups(conn):
    """Every job behind one sign-in page shares its URL; it must not make them one group."""
    assert not _identifies_job(normalize_apply_url(ID_LOGIN) or "")
    a = add_group(conn, (NLX.format(1), None))
    b = add_group(conn, (NLX.format(2), None))
    for gid in (a, b):
        conn.execute(
            "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, "
            "resolved_at) VALUES (?, ?, ?, '[]', 'live', ?)",
            (gid, UT_JOB, ID_LOGIN, NOW.isoformat()),
        )
    assert merge_by_apply_url(conn, now=NOW).groups_merged == 0


# ─── ranking ───────────────────────────────────────────────────────────────


def _res(url: str, status=ApplyStatus.live, *, login=False, start=None) -> Resolution:
    return Resolution(start or url, status, url, login=login)


def _cand(url: str, order: int, members=(1,), seen="", capture=False) -> Candidate:
    return Candidate(url, order, set(members), seen, capture)


def test_host_tiers(conn):
    gid = add_group(conn, (NLX.format(1), None), employer="Acme Widgets, Inc.")
    conn.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) VALUES "
        "('ut-bank', 'A', 'UT', 'x', 'http', 'https://www.statebank.example/index.html', "
        "'enabled'), ('nj-labor', 'A', 'NJ', 'x', 'http', 'https://nj.gov/labor/', 'enabled'), "
        "('ut-employer', 'B', 'UT', 'x', 'http', 'https://careers.employer.example/', 'enabled')"
    )
    c = HostClasses.load(conn, gid)
    assert c.tier(ASHBY) == HostTier.ats
    assert c.tier("https://acme.bamboohr.com/careers/12") == HostTier.ats
    assert c.tier("https://careers.acmewidgets.com/jobs/1") == HostTier.employer
    assert c.tier("https://www.acme.com/careers/1") == HostTier.employer
    assert c.tier("https://other.example/jobs/1") == HostTier.other
    assert c.tier(APLI_A) == HostTier.tracker
    for url in (MONSTER, MITALENT, KYJOBS, WORKSOURCE, UT_JOB, NLX.format(1),
                "https://www.linkedin.com/jobs/view/1/", "https://jobs.statebank.example/j/1",
                "https://nj.gov/labor/jobs/1"):  # fmt: skip
        assert c.tier(url) == HostTier.aggregator, url
    # a bare state domain covers only itself: a state agency's own site is not a job bank
    assert c.tier("https://info.csc.nj.gov/job/1") == HostTier.other
    assert c.tier("https://careers.employer.example/j/1") == HostTier.other  # class B


def test_employer_keys():
    assert employer_keys("Acme, Inc.") == {"acme"}
    assert employer_keys("Acme Widget Co") == {"acmewidget", "acme"}
    assert "state" not in employer_keys("State of Utah")
    assert employer_keys(None) == frozenset()


def test_rank_order_ats_employer_other_tracker_board(conn):
    gid = add_group(conn, (NLX.format(1), None))
    classes = HostClasses.load(conn, gid)
    results = [
        (_cand("b", 0), _res(MONSTER)),
        (_cand("t", 1), _res(APLI_A, ApplyStatus.unresolved)),
        (_cand("o", 2), _res("https://other.example/j/1")),
        (_cand("e", 3), _res("https://careers.acme.example/j/1")),
        (_cand("a", 4), _res(ASHBY + "/application")),
        (_cand("l", 5), _res(UT_JOB, ApplyStatus.blocked, login=True)),
    ]
    order = [c.url for c, _ in rank_results(results, classes)]
    assert order == ["a", "e", "o", "t", "b", "l"]


def test_rank_status_within_a_tier_and_closed_ats_beats_board(conn):
    gid = add_group(conn, (NLX.format(1), None))
    classes = HostClasses.load(conn, gid)
    gh = "https://boards.greenhouse.io/acme/jobs/5"
    results = [
        (_cand("board-live", 0), _res(MONSTER)),
        (_cand("ats-unresolved", 1), _res(gh, ApplyStatus.unresolved)),
        (_cand("ats-expired", 2), _res(ASHBY, ApplyStatus.expired)),
    ]
    order = [c.url for c, _ in rank_results(results, classes)]
    assert order == ["ats-expired", "ats-unresolved", "board-live"]


def test_rank_ties_agreement_then_freshness(conn):
    gid = add_group(conn, (NLX.format(1), None))
    classes = HostClasses.load(conn, gid)
    old = (NOW - timedelta(days=3)).isoformat()
    x, y = "https://other.example/x", "https://other.example/y"
    results = [
        (_cand("x1", 0, members=(1,), seen=NOW.isoformat()), _res(x)),
        (_cand("y1", 1, members=(2,), seen=old), _res(y)),
        (_cand("y2", 2, members=(3,), seen=old), _res(y)),
    ]
    assert rank_results(results, classes)[0][0].url == "y1"  # two members agree on y
    fresh = [
        (_cand("old", 0, seen=old), _res(x)),
        (_cand("new", 1, members=(2,), seen=NOW.isoformat()), _res(y)),
    ]
    assert rank_results(fresh, classes)[0][0].url == "new"


def test_login_wall_is_last_even_on_an_ats(conn):
    gid = add_group(conn, (NLX.format(1), None))
    classes = HostClasses.load(conn, gid)
    results = [
        (_cand("wall", 0), _res(ASHBY, ApplyStatus.blocked, login=True)),
        (_cand("board", 1), _res(MONSTER)),
    ]
    assert rank_results(results, classes)[0][0].url == "board"


# ─── politeness: fetch cap, unique hosts first, boards, robots ──────────────


def test_fetch_cap_and_board_starts_cost_nothing(conn, factory, server):
    urls = [f"https://site{i}.example/jobs/{i}" for i in range(10)]
    for u in urls:
        server.page(u)
    jobs = [(u, None) for u in urls] + [("https://www.linkedin.com/jobs/view/4100000002/", None)]
    gid = add_group(conn, *jobs)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.status == ApplyStatus.live
    assert len(set(server.hosts())) == applylink.MAX_FETCH_STARTS == 6
    assert "www.linkedin.com" not in [r.url.host for r in server.requests]  # not even robots


def test_one_start_per_host_before_a_second(conn, factory, server):
    same = [f"https://mirror.example/jobs/{i}" for i in range(3)]
    other = "https://elsewhere.example/jobs/1"
    for u in [*same, other]:
        server.page(u)
    gid = add_group(conn, *[(u, None) for u in same], (other, None))
    resolve_group(conn, gid, now=NOW, ctx_factory=factory, max_fetches=2)
    assert sorted(set(server.hosts())) == ["elsewhere.example", "mirror.example"]


def test_robots_disallow_on_the_tracker_is_respected(conn, factory, server):
    server.routes["https://www.aplitrak.com/robots.txt"] = httpx.Response(
        200, text="User-agent: *\nDisallow: /\n"
    )
    utah_chain(server)
    for u in (MONSTER, MITALENT, KYJOBS, WORKSOURCE, *(NLX.format(i) for i in range(1, 6))):
        server.page(u)
    gid = group_317(conn)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory, max_fetches=20)
    assert "www.aplitrak.com" not in server.hosts()  # only robots.txt was asked
    assert any(r.url.path == "/robots.txt" for r in server.requests if "aplitrak" in str(r.url))
    assert link.status == ApplyStatus.blocked and link.final_url in (APLI_A, APLI_B)
    assert not link.error.startswith("login wall")  # robots, not a wall: still the best guess


# ─── a capture and the user's own link win ─────────────────────────────────


def _capture_job(conn, gid: int, url: str) -> int:
    jid = conn.execute(
        "INSERT INTO job (source_key, external_id, job_group_id, url, title, employer, "
        "first_seen_at, last_seen_at) VALUES ('wa', ?, ?, ?, 'Staff Software Engineer', 'Acme', "
        "?, ?)",
        (f"cap-{gid}", gid, url, NOW.isoformat(), NOW.isoformat()),
    ).lastrowid
    conn.execute(
        "INSERT INTO capture_log (action_id, route, job_group_id, job_id, captured_at, host, "
        "method, outcome) VALUES (?, 'capture', ?, ?, ?, ?, 'jsonld', 'added')",
        (f"a-{gid}", gid, jid, NOW.isoformat(), url.split("/")[2]),
    )
    return jid


def test_linked_capture_of_the_employer_page_wins(conn, factory, server):
    page = "https://www.example-employer.com/careers/staff-software-engineer"
    server.page(page)
    server.redirect(APLI_A, ASHBY)
    server.page(ASHBY, ASHBY_HTML)
    gid = add_group(conn, (NLX.format(4), APLI_A))
    _capture_job(conn, gid, page)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert (link.final_url, link.chosen_by) == (page, "capture")
    assert "capture" in (link.why or "")
    assert server.pages() == [page]  # the capture settles it: nothing else is requested


def test_closed_capture_does_not_win(conn, factory, server):
    page = "https://www.example-employer.com/careers/gone"
    server.page(page, "<p>Not found</p>", status=404)
    server.redirect(APLI_A, ASHBY)
    server.page(ASHBY, ASHBY_HTML)
    gid = add_group(conn, (NLX.format(4), APLI_A))
    _capture_job(conn, gid, page)
    link = resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    assert link.final_url == ASHBY + "/application" and link.chosen_by is None


def test_capture_host_counts_as_the_employer_domain(conn):
    gid = add_group(conn, (NLX.format(1), None), employer="Someone Else")
    conn.execute(
        "INSERT INTO capture_log (action_id, route, job_group_id, captured_at, host, outcome) "
        "VALUES ('x1', 'capture', ?, ?, 'www.example-employer.com', 'existing')",
        (gid, NOW.isoformat()),
    )
    c = HostClasses.load(conn, gid)
    assert c.tier("https://jobs.example-employer.com/1") == HostTier.employer


def test_user_link_wins_and_is_never_replaced(conn, factory, server):
    utah_chain(server)
    gid = add_group(conn, (UT_JOB, None))
    resolve_group(conn, gid, now=NOW, ctx_factory=factory)
    mine = "https://www.example-employer.com/careers/42?src=me"
    link = set_user_link(conn, gid, mine, NOW)
    assert (link.chosen_by, link.why, link.final_url) == ("user", "set by you", mine)
    n = len(server.requests)
    again = resolve_group(conn, gid, now=NOW + timedelta(days=2), ctx_factory=factory)
    assert again.final_url == mine and again.chosen_by == "user"
    assert reverify(conn, gid, now=NOW + timedelta(days=5), ctx_factory=factory).final_url == mine
    assert reresolve_login_walls(conn, now=NOW, ctx_factory=factory) == []
    assert len(server.requests) == n  # no request for the user's link, ever
    assert capture.offer_apply_link(conn, gid, ASHBY, NOW) is False
    assert get_apply_link(conn, gid).final_url == mine
    assert clear_user_link(conn, gid) and get_apply_link(conn, gid) is None


@pytest.mark.parametrize(
    "bad", ["", "javascript:alert(1)", "ftp://x.example/a", "not a url", "https://", "https://a b"]
)
def test_user_link_must_be_http(conn, bad):
    gid = add_group(conn, (NLX.format(1), None))
    with pytest.raises(ValueError):
        set_user_link(conn, gid, bad, NOW)
    assert get_apply_link(conn, gid) is None


def test_offer_from_a_board_capture_replaces_a_board_or_login_link(conn):
    gid = add_group(conn, ("https://www.linkedin.com/jobs/view/4100000003/", None))
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at) "
        "VALUES (?, ?, ?, '[]', 'live', ?)",
        (gid, MONSTER, MONSTER, NOW.isoformat()),
    )
    assert capture.offer_apply_link(conn, gid, ASHBY, NOW)
    link = get_apply_link(conn, gid)
    assert (link.start_url, link.chosen_by) == (ASHBY, "capture")


def test_merge_keeps_the_user_link_then_the_capture_link(conn):
    from jobhunter.pipeline.dedupe_url import _merge_apply_link

    s = add_group(conn, (NLX.format(1), None))
    a = add_group(conn, (NLX.format(2), None))
    conn.execute(
        "INSERT INTO apply_link (job_group_id, start_url, final_url, chain, status, resolved_at) "
        "VALUES (?, ?, ?, '[]', 'live', ?)",
        (s, ASHBY, ASHBY, NOW.isoformat()),
    )
    set_user_link(conn, a, "https://www.example-employer.com/careers/42", NOW)
    _merge_apply_link(conn, s, a)
    assert get_apply_link(conn, s).chosen_by == "user"


def test_other_ats_hosts_are_named():
    assert ats_name("https://acme.bamboohr.com/careers/12") == "bamboohr"
    assert ats_name(ASHBY) == "ashby"
    assert ats_name("https://other.example/") is None
