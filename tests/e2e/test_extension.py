"""The jobhunter extension end to end (specs/017 1e "Tests (1e)": Extension e2e).

Chromium loads the unpacked extension (a test copy of the manifest that adds only the fixture
pages' top-level hosts, standing in for the activeTab grant a toolbar click gives; never the
embedded iframe's host). ``--host-resolver-rules`` blocks every non-loopback name and
``context.route`` serves the synthetic fixture pages, so nothing leaves the machine. The console
runs in-process on a loopback port with fake scorers, and the extension is paired through its
options page. The popup is driven as ``popup/index.html?tab=<id>`` in an ordinary tab, because a
test cannot click the toolbar.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import uvicorn
from playwright.sync_api import expect

from jobhunter.apply import capture, ext_pairing
from jobhunter.config import Settings
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring.scorers import ScoreResult

from . import seed as seeding

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
PAGES = REPO / "tests" / "fixtures" / "capture" / "pages"
EMP = "https://jobs.example-employer.test"
BLOCKED = "https://blocked.example-employer.test"
LI = "https://www.linkedin.com"
TEST_HOSTS = [f"{EMP}/*", f"{BLOCKED}/*", f"{LI}/*", "https://www.indeed.com/*"]
GH = "https://boards.greenhouse.io/boardsynthetic/jobs/7001"
MUTATIONS = """
window.__mutations = 0;
window.__mo = new MutationObserver((records) => { window.__mutations += records.length; });
window.__mo.observe(document, {subtree: true, childList: true, attributes: true,
                               characterData: true});
"""
SCREEN = json.dumps(
    {
        "verdict": "strong",
        "dimensions": {
            k: {"score": 90, "why": "match: ok"} for k in ("skills", "seniority", "domain")
        },
        "raw_skills": 90,
        "recency_weighted_skills": 90,
        "stale_skills": [],
        "current_focus_overlap": 50,
        "done_with_hits": [],
        "evidence": [{"claim": "ops", "quote": "Run the synthetic"}],
        "blockers": [],
        "missing_info": [],
        "shape_flags": [],
        "tailoring_hints": [],
    }
)


def html(name: str, **subs: str) -> str:
    text = (PAGES / name).read_text(encoding="utf-8")
    for old, new in subs.items():
        text = text.replace(old, new)
    return text


class BlockingScorer:
    name = "test:model"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def submit(self, requests):
        self.calls += 1
        self.started.set()
        assert self.release.wait(30)
        return [
            ScoreResult(custom_id=r.custom_id, status="succeeded", text=SCREEN) for r in requests
        ]

    def cost(self, usage, batch):
        return 0.001


@dataclass
class Harness:
    ctx: Any
    ext_id: str
    app: Any
    db_path: Path
    served: dict[str, str]
    blocked: list[str]
    board_requests: list[str]
    captures: list[str] = field(default_factory=list)
    gate: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    _ext_page: Any = None

    def worker(self):
        workers = [w for w in self.ctx.service_workers if self.ext_id in w.url]
        return workers[0] if workers else self.ctx.wait_for_event("serviceworker")

    def ext_page(self):
        """An extension page for chrome.* queries: unlike a worker handle, it survives the
        worker being stopped."""
        if self._ext_page is None or self._ext_page.is_closed():
            self._ext_page = self.ctx.new_page()
            self._ext_page.goto(f"chrome-extension://{self.ext_id}/options/index.html")
        return self._ext_page

    def open(self, url: str, body: str):
        self.served[url] = body
        page = self.ctx.new_page()
        page.goto(url)
        page.evaluate("() => { window.__mo.takeRecords(); window.__mutations = 0; }")
        return page

    def tab_id(self, url: str) -> int:
        ids = self.ext_page().evaluate(
            "(u) => chrome.tabs.query({}).then((ts) => ts.filter((t) => t.url === u)"
            ".map((t) => t.id))",
            url,
        )
        assert ids, f"no tab for {url}"
        return ids[-1]

    def popup(self, url: str):
        p = self.ctx.new_page()
        p.goto(f"chrome-extension://{self.ext_id}/popup/index.html?tab={self.tab_id(url)}")
        expect(p.locator("#status")).not_to_have_text("Reading this page…", timeout=15000)
        return p

    def rows(self, sql: str, params=()):
        return seeding.query(self.db_path, sql, params)

    def entries(self) -> dict:
        return self.ext_page().evaluate(
            "() => chrome.storage.session.get('entries').then((x) => x.entries || {})"
        )


def pump_until(event: threading.Event, page, timeout_s: float = 15) -> None:
    """Wait for ``event`` while letting Playwright run its route handlers on this thread (a
    plain Event.wait would block them, and the worker's console request with them)."""
    deadline = time.monotonic() + timeout_s
    while not event.is_set():
        assert time.monotonic() < deadline, "timed out"
        page.wait_for_timeout(50)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def h(tmp_path_factory, pw):
    tmp = tmp_path_factory.mktemp("ext-e2e")
    port = free_port()
    db_path = tmp / "e2e.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.close()
    profile_dir = seeding.write_profile(tmp)
    settings = Settings.model_validate(
        {
            "paths": {
                "profile_dir": str(profile_dir),
                "db_path": str(db_path),
                "cache_dir": str(tmp / "cache"),
                "data_dir": str(tmp / "data"),
            },
            "scoring": {"screen_scorer": "test:model"},
            "console": {"port": port},
            "capture": {"disabled_hosts": ["blocked.example-employer.test"]},
        }
    )
    app = create_app(
        settings, lambda: db.connect(db_path), clock=lambda: datetime.now(UTC), ext_port=port
    )

    def no_scorer(spec):
        raise AssertionError("no scorer was expected")

    app.state.packet_scorer_factory = no_scorer
    app.state.ext_fetcher = lambda url: (_ for _ in ()).throw(AssertionError("no fetch"))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)

    # A copy of the extension whose manifest grants only the fixture pages' top-level hosts.
    ext_dir = tmp / "extension"
    shutil.copytree(REPO / "extension", ext_dir)
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = manifest["host_permissions"] + TEST_HOSTS
    (ext_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    served: dict[str, str] = {}
    blocked: list[str] = []
    board_requests: list[str] = []
    ctx = pw.chromium.launch_persistent_context(
        str(tmp / "chrome-profile"),
        channel="chromium",
        headless=True,
        args=[
            f"--disable-extensions-except={ext_dir}",
            f"--load-extension={ext_dir}",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        ],
    )
    ctx.add_init_script(MUTATIONS)

    def router(route):
        url = route.request.url.split("#", 1)[0]
        host = urlparse(url).hostname or ""
        if host == "127.0.0.1" or url.startswith("chrome-extension://"):
            route.continue_()
            return
        if host.endswith(("linkedin.com", "indeed.com")):
            board_requests.append(url)
        if url in served:
            route.fulfill(status=200, content_type="text/html", body=served[url])
        else:
            blocked.append(url)
            route.abort()

    ctx.route("**/*", router)
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
    ext_id = urlparse(sw.url).hostname
    harness = Harness(ctx, ext_id, app, db_path, served, blocked, board_requests)

    real_capture = capture.capture

    def counting(conn, req, *a, **kw):
        harness.captures.append(req.facts.url)
        harness.entered.set()
        if harness.gate is not None:
            assert harness.gate.wait(30)
        return real_capture(conn, req, *a, **kw)

    capture.capture = counting
    try:
        yield harness
    finally:
        capture.capture = real_capture
        ctx.close()
        srv.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def paired(h):
    assert h.ext_id == ext_pairing.EXTENSION_ID  # the pinned key gives the pinned ID
    data_dir = h.app.state.settings.paths.data_dir
    code, _ = ext_pairing.new_code(ext_pairing.state_path(data_dir), datetime.now(UTC))
    opt = h.ctx.new_page()
    opt.goto(f"chrome-extension://{h.ext_id}/options/index.html")
    expect(opt.locator("#pair-status")).to_contain_text("Not paired")
    opt.fill("#port", str(h.app.state.ext_port))
    opt.fill("#code", code)
    opt.click("#pair")
    expect(opt.locator("#pair-status")).to_contain_text("Paired")
    opt.close()
    return h


@pytest.fixture(autouse=True)
def _no_stray_requests(request):
    yield
    if "h" in request.fixturenames:
        hh = request.getfixturevalue("h")
        assert not hh.blocked, f"requests outside the fixtures: {hh.blocked}"


def jsonld_page(n: int, title: str = "Platform Engineer") -> tuple[str, str]:
    url = f"{EMP}/careers/{n}"
    body = html(
        "jsonld.html",
        **{"/careers/1001": f"/careers/{n}", "Platform Engineer": title},
    )
    return url, body


# ─── capture ───────────────────────────────────────────────────────────────


def test_unpaired_extension_injects_nothing(h):
    url, body = jsonld_page(901)
    h.open(url, body)
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Not paired")
    assert h.captures == [] and h.entries() == {}
    p.close()


def test_capture_added_then_already_in_jobhunter_on_the_same_document(paired):
    h = paired
    url, body = jsonld_page(1001)
    page = h.open(url, body)
    before = page.evaluate("document.documentElement.outerHTML")
    n = len(h.captures)
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Added")
    expect(p.locator("#card-title")).to_have_text("Platform Engineer")
    expect(p.locator("#card-employer")).to_contain_text("Synthetic Widgets")
    assert len(h.captures) == n + 1
    assert page.evaluate("document.documentElement.outerHTML") == before
    assert page.evaluate("() => window.__mutations + window.__mo.takeRecords().length") == 0
    row = h.rows(
        "SELECT g.score_on_request, j.source_key, j.stage FROM job_group g "
        "JOIN job j ON j.id = g.canonical_job_id WHERE j.url = ?",
        (url,),
    )
    assert [tuple(r) for r in row] == [(1, "paste-manual", "normalized")]
    assert h.rows("SELECT COUNT(*) FROM fit_score")[0][0] == 0
    p.close()
    # A second click on the same document: the stored result, no new capture.
    again = h.popup(url)
    expect(again.locator("#status")).to_have_text("Already in jobhunter")
    assert len(h.captures) == n + 1
    # Capture again re-reads the page without a reload: a second injection, no collision.
    again.click("#again")
    expect(again.locator("#status")).to_have_text("Already in jobhunter")
    assert len(h.captures) == n + 2
    assert page.evaluate("() => window.__mutations + window.__mo.takeRecords().length") == 0
    again.close()


def test_menu_capture_then_popup_makes_one_request(paired):
    h = paired
    url, body = jsonld_page(1002, "Menu Engineer")
    h.open(url, body)
    n = len(h.captures)
    tid = h.tab_id(url)
    res = h.worker().evaluate(
        "([id, u]) => self.jobhunterCapture({id, url: u, windowId: 0}, "
        "{trigger: 'menu-page', again: true})",
        [tid, url],
    )
    assert res["state"] == "result" and res["response"]["outcome"] == "added"
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Added")
    assert len(h.captures) == n + 1
    p.close()


def test_a_pushstate_job_change_is_a_new_capture(paired):
    h = paired
    url = f"{EMP}/search?jobId=501"
    page = h.open(url, html("spa.html"))
    n = len(h.captures)
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Added")
    p.close()
    page.click("#next")  # the user's own click: pushState, same document
    assert page.url == f"{EMP}/search?jobId=502"
    p2 = h.popup(page.url)
    # The JSON-LD still names job 501: stale, so previewed, not added.
    expect(p2.locator("#status")).to_have_text("Check this, then Add")
    assert len(h.captures) == n + 2
    p2.close()


def test_preview_with_an_edited_description(paired):
    h = paired
    url = f"{EMP}/plain/sre-2"
    page = h.open(url, html("plain.html"))
    page.fill("#typed", "typed-value@example.com")
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Check this, then Add")
    text = p.locator("#pv-description").input_value()
    assert "Keep the synthetic services" in text
    for absent in ("Hi, Pat", "HIDDEN-", "typed-value", "Sam Synthetic"):
        assert absent not in text
    expect(p.locator("#pv-title")).to_have_value("Site Reliability Engineer")
    expect(p.locator("#pv-employer")).to_have_value("Plain Synthetic Co")
    edited = text.replace("Our stack is Linux, Postgres and a little Go.", "").strip()
    p.fill("#pv-description", edited)
    p.click("#add")
    expect(p.locator("#status")).to_have_text("Added")
    stored = h.rows("SELECT description_text FROM job WHERE url = ?", (url,))[0][0]
    assert "a little Go" not in stored and "Keep the synthetic services" in stored
    p.close()


def test_score_it_with_confirm_202_and_status(paired):
    h = paired
    url, body = jsonld_page(1003, "Scored Engineer")
    h.open(url, body)
    scorer = BlockingScorer()
    h.app.state.packet_scorer_factory = lambda spec: scorer
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Added")
    p.click("#score-it")
    expect(p.locator("#prefilter-line")).to_contain_text("skips your prefilter rules")
    expect(p.locator("#notice-line")).to_contain_text("resume-derived profile")
    expect(p.locator("#confirm-score")).to_contain_text("Confirm, spend")
    p.click("#confirm-score")
    expect(p.locator("#score-status")).to_have_text("Scoring")
    pump_until(scorer.started, p)
    scorer.release.set()
    expect(p.locator("#score-status")).to_contain_text("Scored:", timeout=15000)
    assert scorer.calls == 1
    p.close()


def test_prepare_packet_opens_the_packet_page(paired):
    h = paired
    url, body = jsonld_page(1004, "Packet Engineer")
    h.open(url, body)
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Added")
    with h.ctx.expect_page() as opened:
        p.click("#prepare")
    packet = opened.value
    packet.wait_for_load_state()
    assert "/packet/" in packet.url and "127.0.0.1" in packet.url
    expect(packet.locator("h1")).to_have_text("Packet Engineer")
    packet.close()
    p.close()


def test_popup_closed_mid_request_still_gets_its_result(paired):
    h = paired
    url, body = jsonld_page(1005, "Closed Popup Engineer")
    h.open(url, body)
    n = len(h.captures)
    h.gate = threading.Event()
    h.entered.clear()
    try:
        p = h.ctx.new_page()
        p.goto(f"chrome-extension://{h.ext_id}/popup/index.html?tab={h.tab_id(url)}")
        pump_until(h.entered, p)
        p.close()  # the worker finishes the request
        h.gate.set()
    finally:
        h.gate = None
    deadline = time.monotonic() + 10
    while not h.rows("SELECT 1 FROM job WHERE url = ?", (url,)):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    time.sleep(0.5)  # the worker stores the entry after the answer
    again = h.popup(url)
    expect(again.locator("#status")).to_have_text("Already in jobhunter")
    assert len(h.captures) == n + 1
    again.close()


def test_worker_stopped_mid_score_status_still_reaches_the_bucket(paired):
    h = paired
    url, body = jsonld_page(1006, "Restart Engineer")
    h.open(url, body)
    scorer = BlockingScorer()
    h.app.state.packet_scorer_factory = lambda spec: scorer
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Added")
    p.click("#score-it")
    p.click("#confirm-score")
    pump_until(scorer.started, p)
    p.close()
    cdp = h.ctx.new_cdp_session(h.ctx.pages[0])
    cdp.send("ServiceWorker.enable")
    cdp.send("ServiceWorker.stopAllWorkers")
    scorer.release.set()
    for t in h.app.state.ext_score_threads:
        t.join(15)
    again = h.popup(url)
    expect(again.locator("#score-status")).to_contain_text("Scored:", timeout=15000)
    again.close()


def test_off_list_host_injects_nothing(paired):
    h = paired
    url = f"{BLOCKED}/careers/1"
    h.open(url, html("jsonld.html"))
    n = len(h.captures)
    p = h.popup(url)
    expect(p.locator("#status")).to_have_text("Capture is off here")
    assert len(h.captures) == n
    assert not [k for k in h.entries() if "blocked.example-employer.test" in k]
    p.close()


def test_linkedin_notice_then_apply_control_read_without_touching_it(paired):
    h = paired
    url = f"{LI}/jobs/view/4012345678/"
    apply = f'<a data-apply href="{GH}?gh_src=li">Apply</a>'
    page = h.open(url, html("board.html", **{"<!--APPLY-->": apply}))
    before = page.evaluate("document.documentElement.outerHTML")
    loaded = len(h.board_requests)
    n = len(h.captures)
    p = h.popup(url)
    expect(p.locator("#notice")).to_contain_text("LinkedIn")
    assert len(h.captures) == n and not [k for k in h.entries() if "linkedin" in k]
    p.click("#accept-notice")
    expect(p.locator("#status")).to_have_text("Check this, then Add")
    assert len(h.captures) == n + 1
    assert page.evaluate("window.__applyEvents") == 0
    assert page.evaluate("() => window.__mutations + window.__mo.takeRecords().length") == 0
    assert page.evaluate("document.documentElement.outerHTML") == before
    assert h.board_requests[loaded:] == []  # nothing requested from linkedin.com
    p.fill("#pv-employer", "Board Synthetic Co")
    p.click("#add")
    expect(p.locator("#status")).to_have_text("Added")
    ref = h.rows("SELECT board_id, apply_mode, apply_url FROM job_board_ref")
    assert ("4012345678", "offsite", GH) in [tuple(r) for r in ref]
    p.close()


def test_a_page_chrome_will_not_let_us_read_offers_new_packet(paired):
    h = paired
    url = f"{EMP}/broken/posting.pdf"  # not served: Chrome shows its error page
    page = h.ctx.new_page()
    with contextlib.suppress(Exception):
        page.goto(url)
    h.blocked.clear()
    n = len(h.captures)
    p = h.popup(url)
    expect(p.locator("#restricted")).to_be_visible()
    assert len(h.captures) == n
    with h.ctx.expect_page() as opened:
        p.click("#open-new-packet")
    new = opened.value
    new.wait_for_load_state()
    assert "/apply/new?url=" in new.url
    expect(new.locator("#new-url")).to_have_value(url)
    new.close()
    p.close()
