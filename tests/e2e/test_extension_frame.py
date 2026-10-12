"""The extension skeleton for ATS frames end to end (spec 022 ticket A2, git-bug 69849da).

Chromium loads the real ``extension/`` folder (a copy whose manifest adds one generic test host,
standing in for the activeTab grant). The extension is loaded through CDP
``Extensions.loadUnpacked`` so a test can unload and reload it (``--load-extension`` ones cannot
be uninstalled). ``--host-resolver-rules`` blocks every non-loopback name, ``context.route``
serves hand-written fixture pages on real ATS host names, and the console runs in process on a
loopback port. Nothing here uses the network or a scorer.

Content scripts are driven from the extension's own page with ``scripting.executeScript`` into
the frame's isolated world: that is exactly the world, ``sender`` and storage access of the real
content script, so a message sent that way is what a hijacked content script could send.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
import uvicorn
from playwright.sync_api import expect

from jobhunter.apply import ext_pairing
from jobhunter.config import Settings
from jobhunter.console.app import create_app
from jobhunter.core import db

from . import seed as seeding

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
EMP = "https://jobs.example-employer.test"
GENERIC = "https://careers.generic-test.test"
GH_EMBED = "https://boards.greenhouse.io/embed/job_app?for=boardsynthetic&token=7001"
EMPLOYER = f"{EMP}/careers/1"
GENERIC_PAGE = f"{GENERIC}/apply"
OVERLAY = "[data-jobhunter-overlay]"

FORM = """<!doctype html><meta charset="utf-8"><title>Apply</title>
<style>body{margin:0;padding:8px;font:14px sans-serif}
label,input,textarea,button,div{display:block;margin:6px 0}</style>
<form id="application_form" action="/apply" method="post" target="sink">
 <input id="first_name" name="first_name" aria-label="First name">
 <div id="s3_upload_for_resume"><input id="resume" type="file"></div>
 <div id="s3_upload_for_cover_letter"><input id="letter" type="file"></div>
 <textarea id="cover_letter_text"></textarea>
 <button id="submit_app" type="submit">Submit application</button>
</form><iframe name="sink" style="display:none"></iframe>"""
EMPLOYER_PAGE = f"""<!doctype html><meta charset="utf-8"><title>Careers</title>
<p>Employer page</p><iframe id="ats" width="500" height="560" src="{GH_EMBED}"></iframe>"""
GENERIC_BODY = (
    '<!doctype html><meta charset="utf-8"><title>Generic</title><input id="resume" type="file">'
)
# A styled-away file input in a full-width box, inside the page's own scroll container.
GH_HIDDEN = "https://boards.greenhouse.io/embed/job_app?for=boardsynthetic&token=7002"
HIDDEN_FORM = """<!doctype html><meta charset="utf-8"><title>Apply</title>
<style>body{margin:0;font:14px sans-serif}
#scroller{height:180px;overflow:auto;border:1px solid #888}
#s3_upload_for_resume{display:block;width:100%;height:40px;background:#eee}
#resume{position:absolute;width:0;height:0;opacity:0}</style>
<div id="scroller"><div style="height:300px">spacer</div>
 <div id="s3_upload_for_resume"><input id="resume" type="file"></div>
 <div style="height:300px">more</div></div>"""
SERVED = {
    GH_EMBED: FORM,
    GH_HIDDEN: HIDDEN_FORM,
    EMPLOYER: EMPLOYER_PAGE,
    GENERIC_PAGE: GENERIC_BODY,
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


RIGS: list[Rig] = []  # every browser started here, for the offline check


class Rig:
    def __init__(self, pw, tmp: Path):
        port = free_port()
        db_path = tmp / "e2e.db"
        conn = db.connect(db_path)
        db.migrate(conn)
        conn.close()
        settings = Settings.model_validate(
            {
                "paths": {
                    "profile_dir": str(seeding.write_profile(tmp)),
                    "db_path": str(db_path),
                    "cache_dir": str(tmp / "cache"),
                    "data_dir": str(tmp / "data"),
                },
                "scoring": {"screen_scorer": "openrouter:test/model"},
                "console": {"port": port},
            }
        )
        self.app = create_app(
            settings, lambda: db.connect(db_path), clock=lambda: datetime.now(UTC), ext_port=port
        )
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)

        self.ext_dir = tmp / "extension"
        shutil.copytree(REPO / "extension", self.ext_dir)
        manifest = json.loads((self.ext_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["host_permissions"] = [*manifest["host_permissions"], f"{GENERIC}/*"]
        (self.ext_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        self.blocked: list[str] = []
        RIGS.append(self)
        self.ctx = pw.chromium.launch_persistent_context(
            str(tmp / "chrome-profile"),
            channel="chromium",
            headless=True,
            args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
            ignore_default_args=["--disable-extensions"],
        )
        self.cdp = self.ctx.browser.new_browser_cdp_session()
        self.ext_id = self.cdp.send("Extensions.loadUnpacked", {"path": str(self.ext_dir)})["id"]
        self.ctx.route("**/*", self._route)
        self._ext_page = None

    def _route(self, route):
        url = route.request.url.split("#", 1)[0]
        host = urlparse(url).hostname or ""
        if host == "127.0.0.1" or url.startswith(("chrome-extension://", "data:")):
            return route.continue_()
        if url.endswith("/apply") and route.request.method == "POST":
            return route.fulfill(status=200, content_type="text/html", body="<p>posted</p>")
        for served, body in SERVED.items():
            if url == served:
                return route.fulfill(status=200, content_type="text/html", body=body)
        self.blocked.append(url)
        return route.abort()

    # ── the extension's own page, for chrome.* calls ──

    def ext(self):
        if self._ext_page is None or self._ext_page.is_closed():
            for attempt in range(40):  # right after a (re)load the extension needs a moment
                self._ext_page = self.ctx.new_page()
                try:
                    self._ext_page.goto(f"chrome-extension://{self.ext_id}/options/index.html")
                    break
                except Exception:
                    self._ext_page.close()
                    time.sleep(0.25)
                    assert attempt < 39, "the extension never came back"
        return self._ext_page

    def send(self, message: dict):
        """A message from an extension page (the options page)."""
        return self.ext().evaluate("(m) => chrome.runtime.sendMessage(m)", message)

    def pair(self):
        code, _ = ext_pairing.new_code(
            ext_pairing.state_path(self.app.state.settings.paths.data_dir), datetime.now(UTC)
        )
        got = self.send({"type": "pair", "code": code, "port": self.port})
        assert got == {"paired": True}, got

    def token(self):
        return self.ext().evaluate("() => chrome.storage.local.get('token')")

    def tab_id(self, url: str) -> int:
        ids = self.ext().evaluate(
            "(u) => chrome.tabs.query({}).then((ts) => ts.filter((t) => t.url === u)"
            ".map((t) => t.id))",
            url,
        )
        assert ids, f"no tab for {url}"
        return ids[-1]

    def announced_tabs(self) -> list[int]:
        reg = self.ext().evaluate(
            "() => chrome.storage.session.get('frames').then((x) => x.frames || {})"
        )
        return sorted({int(k.split(":")[0]) for k in reg})

    def frames(self, tab: int) -> dict:
        """The worker's frame registry for a tab: {frameId: entry}."""
        reg = self.ext().evaluate(
            "() => chrome.storage.session.get('frames').then((x) => x.frames || {})"
        )
        return {int(k.split(":")[1]): v for k, v in reg.items() if k.startswith(f"{tab}:")}

    def in_frame(self, tab: int, frame_id: int, func_src: str, arg=None):
        """Run ``func_src`` (an arrow function's source) in the content-script world of a frame."""
        expr = (
            "async ([tab, frame, arg]) => { const [r] = await chrome.scripting.executeScript("
            "{target: {tabId: tab, frameIds: [frame]}, func: " + func_src + ", args: [arg]}); "
            "return r.result; }"
        )
        return self.ext().evaluate(expr, [tab, frame_id, arg])

    def send_from_frame(self, tab: int, frame_id: int, message: dict):
        """What a (hijacked) content script in that frame could send to the worker."""
        return self.in_frame(
            tab,
            frame_id,
            "(m) => chrome.runtime.sendMessage(m).catch((e) => ({threw: String(e)}))",
            message,
        )

    def open(self, url: str):
        page = self.ctx.new_page()
        page.goto(url)
        return page

    def wait_for(self, cond, what: str, timeout_s: float = 15.0):
        deadline = time.monotonic() + timeout_s
        while True:
            got = cond()
            if got:
                return got
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            self.ctx.pages[0].wait_for_timeout(100)

    def frame_state(self, tab: int, frame_id: int):
        return self.in_frame(
            tab,
            frame_id,
            "() => globalThis.__jhFrame ? globalThis.__jhFrame.state() : null",
        )

    def buttons(self, tab: int, frame_id: int):
        return (
            self.in_frame(
                tab,
                frame_id,
                "() => globalThis.__jhFrame ? globalThis.__jhFrame.buttons() : []",
            )
            or []
        )

    def close(self):
        self.ctx.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="module")
def rig(tmp_path_factory, pw):
    r = Rig(pw, tmp_path_factory.mktemp("ext-frame"))
    r.pair()
    yield r
    r.close()


@pytest.fixture(autouse=True)
def _offline():
    yield
    for r in RIGS:
        assert not r.blocked, f"requests outside the fixtures: {r.blocked}"


def switches(r: Rig, **kw):
    got = r.send({"type": "switches/set", "ats": "greenhouse", **kw})
    assert got["state"] == "ok"


def learned(r: Rig, tab: int, frame: int = 0):
    return r.wait_for(
        lambda: (r.frame_state(tab, frame) or {}).get("hasForms"), "the frame to get its form data"
    )


# ─── the overlay and the switches ──────────────────────────────────────────


def test_overlay_draws_only_when_show_buttons_is_on(rig):
    switches(rig, show_buttons=False, record_submits=True)
    page = rig.open(GH_EMBED)
    tab = rig.tab_id(GH_EMBED)
    learned(rig, tab)
    state = rig.frame_state(tab, 0)
    assert state["switches"] == {"show_buttons": False, "record_submits": True}
    # off: no element of ours in the page at all
    assert page.locator(OVERLAY).count() == 0 and state["hasHost"] is False

    switches(rig, show_buttons=True)
    rig.wait_for(lambda: page.locator(OVERLAY).count() == 1, "the overlay to appear")
    buttons = rig.buttons(tab, 0)
    assert {b["key"]: b["text"] for b in buttons} == {
        "resume": "jobhunter resume",
        "cover_letter": "jobhunter letter",
        "cover_letter_text": "jobhunter letter",
    }
    assert page.evaluate(f"document.body.lastElementChild.matches('{OVERLAY}')")
    # each button sits beside its field
    box = (
        page.locator("#resume").bounding_box()
        or page.locator("#s3_upload_for_resume").bounding_box()
    )
    resume = next(b for b in buttons if b["key"] == "resume")
    assert resume["x"] >= box["x"] + box["width"] - 1 and abs(resume["y"] - box["y"]) < 40

    # Record submits is independent: turning it off leaves the buttons
    switches(rig, record_submits=False)
    rig.wait_for(
        lambda: rig.frame_state(tab, 0)["switches"]["record_submits"] is False, "the switch"
    )
    assert page.locator(OVERLAY).count() == 1

    # Show buttons off again removes the host entirely
    switches(rig, show_buttons=False)
    rig.wait_for(lambda: page.locator(OVERLAY).count() == 0, "the overlay to go away")
    assert rig.buttons(tab, 0) == []
    switches(rig, show_buttons=True, record_submits=True)
    page.close()


def test_page_cannot_see_or_script_the_overlay(rig):
    switches(rig, show_buttons=True)
    page = rig.open(GH_EMBED)
    rig.wait_for(lambda: page.locator(OVERLAY).count() == 1, "the overlay")
    # a closed shadow root: the page's own script cannot reach the buttons
    assert page.evaluate(f"document.querySelector('{OVERLAY}').shadowRoot") is None
    page.close()


def test_a_real_click_on_a_button_opens_the_panel_bound_to_the_tab(rig):
    switches(rig, show_buttons=True)
    page = rig.open(GH_EMBED)
    tab = rig.tab_id(GH_EMBED)
    rig.wait_for(lambda: rig.buttons(tab, 0), "the buttons")
    resume = next(b for b in rig.buttons(tab, 0) if b["key"] == "resume")
    page.mouse.click(resume["x"] + resume["w"] / 2, resume["y"] + resume["h"] / 2)

    # getContexts lists the panel document before it has navigated (documentUrl is ''), so wait
    # for the bound URL itself rather than for any context.
    def panel_urls():
        return rig.ext().evaluate(
            "() => chrome.runtime.getContexts({contextTypes: ['SIDE_PANEL']})"
            ".then((c) => c.map((x) => x.documentUrl))"
        )

    rig.wait_for(
        lambda: any(f"panel/index.html?tab={tab}" in u for u in panel_urls()),
        "the side panel to load bound to the tab",
    )
    got = panel_urls()
    assert any(f"panel/index.html?tab={tab}" in u for u in got), got
    st = rig.send({"type": "panel/state", "tab": tab})
    assert st["ats"] == "greenhouse" and st["host"] == "boards.greenhouse.io"
    assert st["field"] == "resume"
    # the shell, loaded as an ordinary page the way a test can see it, is bound by ?tab=
    shell = rig.ctx.new_page()
    shell.goto(f"chrome-extension://{rig.ext_id}/panel/index.html?tab={tab}")
    expect(shell.locator("#bound")).to_have_text("Bound to this greenhouse tab.")
    expect(shell.locator("#site")).to_have_text("boards.greenhouse.io")
    expect(shell.locator("#field")).to_have_text("resume")
    shell.close()
    gone = rig.ctx.new_page()
    gone.goto(f"chrome-extension://{rig.ext_id}/panel/index.html?tab=987654321")
    expect(gone.locator("#bound")).to_have_text("That tab no longer exists.")
    gone.close()
    page.close()


def test_a_button_for_a_hidden_input_stays_in_the_viewport_and_follows_inner_scrolling(rig):
    switches(rig, show_buttons=True)
    page = rig.open(GH_HIDDEN)
    tab = rig.tab_id(GH_HIDDEN)
    rig.wait_for(lambda: rig.buttons(tab, 0), "the button")
    page.evaluate("document.getElementById('scroller').scrollTop = 250")
    width = page.evaluate("document.documentElement.clientWidth")
    rig.wait_for(lambda: rig.buttons(tab, 0)[0]["y"] < 250, "the button to follow the scroll")
    b = rig.buttons(tab, 0)[0]
    assert b["x"] + b["w"] <= width  # inside the viewport, not hanging past the edge
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    before = b["y"]
    page.evaluate("document.getElementById('scroller').scrollTop = 0")
    rig.wait_for(lambda: rig.buttons(tab, 0)[0]["y"] > before + 100, "the button to follow back")
    page.close()


# ─── the sender policy ─────────────────────────────────────────────────────

PAGE_ONLY_TYPES = [
    "draft/generate", "draft/estimate", "fill/write", "file/attach", "text/insert", "ats/mode",
    "session/start", "match/pick", "packet/ready", "pair", "unpair", "pairStatus", "open",
    "again", "add", "link", "fetch", "score", "openConsole", "switches/get", "switches/set",
    "panel/state",
]  # fmt: skip


def refused(got) -> bool:
    return isinstance(got, dict) and got.get("state") == "refused"


def test_a_content_script_cannot_send_spend_write_mode_or_pairing_messages(rig):
    page = rig.open(GH_EMBED)
    tab = rig.tab_id(GH_EMBED)
    learned(rig, tab)
    for type_ in [*PAGE_ONLY_TYPES, "bogus", "constructor", "__proto__", "toString"]:
        got = rig.send_from_frame(tab, 0, {"type": type_, "ats": "greenhouse", "tab": tab})
        assert refused(got), (type_, got)
    # nothing leaked through: still paired, no switch changed by the refused switches/set
    assert rig.send({"type": "pairStatus"})["paired"] is True
    # what the policy does allow gets through: forms/get answers, and the ATS comes from
    # sender.url, never from the payload
    got = rig.send_from_frame(
        tab, 0, {"type": "forms/get", "ats": "lever", "url": "https://jobs.lever.co/x"}
    )
    assert got["state"] == "ok" and got["ats"] == "greenhouse"
    assert got["forms"]["ats"] == "greenhouse"
    # an allowed-but-not-yet-built content type is answered, not refused
    got = rig.send_from_frame(tab, 0, {"type": "ask/answer"})
    assert got["state"] == "unavailable"
    page.close()


def test_the_worker_refuses_unknown_types_from_extension_pages_too(rig):
    assert refused(rig.send({"type": "bogus"}))
    assert refused(rig.send({"nope": 1}))


def test_a_frame_that_never_announced_itself_is_not_trusted(rig):
    page = rig.open(GH_EMBED)
    tab = rig.tab_id(GH_EMBED)
    learned(rig, tab)
    assert rig.send_from_frame(tab, 0, {"type": "frame/bye"})["state"] == "ok"
    # a page-only type from an unannounced frame is refused, not "unannounced"
    assert refused(rig.send_from_frame(tab, 0, {"type": "draft/generate"}))
    assert rig.send_from_frame(tab, 0, {"type": "forms/get"})["state"] == "unannounced"
    assert rig.send_from_frame(tab, 0, {"type": "ask/answer"})["state"] == "unannounced"
    assert rig.send_from_frame(tab, 0, {"type": "frame/hello"})["state"] == "ok"
    assert rig.send_from_frame(tab, 0, {"type": "forms/get"})["state"] == "ok"
    page.close()


def test_an_injected_generic_frame_may_only_say_hello_ask_for_forms_and_bye(rig):
    page = rig.open(GENERIC_PAGE)
    tab = rig.tab_id(GENERIC_PAGE)
    # no content script is declared for this host: nothing runs, and an uninjected frame
    # (here, driven by hand) is nobody
    assert refused(rig.send_from_frame(tab, 0, {"type": "frame/hello"}))
    worker = rig.ctx.service_workers[0]
    worker.evaluate("(t) => self.jobhunterInjectGeneric({id: t})", tab)
    assert rig.send_from_frame(tab, 0, {"type": "frame/hello"})["state"] == "ok"
    got = rig.send_from_frame(tab, 0, {"type": "forms/get"})
    assert got["state"] == "ok" and got["ats"] is None
    assert got["switches"] == {"show_buttons": False, "record_submits": False}
    assert got["forms"] is None
    for type_ in [*PAGE_ONLY_TYPES, "panel/open", "ask/answer", "submit/attempt", "applied/undo"]:
        assert refused(rig.send_from_frame(tab, 0, {"type": type_})), type_
    assert rig.send_from_frame(tab, 0, {"type": "frame/bye"})["state"] == "ok"
    # the generic frame draws nothing
    assert page.locator(OVERLAY).count() == 0
    page.close()


# ─── the token ─────────────────────────────────────────────────────────────


def test_the_token_is_hidden_from_content_scripts(rig):
    page = rig.open(GH_EMBED)
    tab = rig.tab_id(GH_EMBED)
    learned(rig, tab)
    secret = rig.token()["token"]
    assert secret  # extension pages (trusted contexts) still read it
    probe = """async () => {
      const out = {};
      for (const [name, fn] of Object.entries({
        get: () => chrome.storage.local.get('token'),
        all: () => chrome.storage.local.get(null),
        set: () => chrome.storage.local.set({planted: 'x'}),
      })) {
        try { out[name] = {ok: await fn()}; } catch (e) { out[name] = {error: String(e)}; }
      }
      return out;
    }"""
    got = rig.in_frame(tab, 0, probe)
    for name in ("get", "all", "set"):
        assert "error" in got[name] and "not allowed" in got[name]["error"], got
    assert secret not in json.dumps(got)
    assert "planted" not in rig.ext().evaluate("() => chrome.storage.local.get(null)")
    # and no message gives it away
    assert refused(rig.send_from_frame(tab, 0, {"type": "pairStatus"}))
    page.close()


# ─── re-injection after an extension reload ────────────────────────────────


@pytest.fixture
def reload_rig(tmp_path, pw):
    r = Rig(pw, tmp_path)
    r.pair()
    yield r
    r.close()


def test_reinjection_after_an_extension_reload_replaces_the_orphans_overlay(reload_rig):
    r = reload_rig
    framed = r.open(EMPLOYER)  # the ATS form in an iframe on an employer page
    inner = next(f for f in framed.frames if f.url == GH_EMBED)
    top = r.open(GH_EMBED)  # the same form as a tab of its own
    tab = r.tab_id(GH_EMBED)
    r.wait_for(lambda: inner.locator(OVERLAY).count() == 1, "the overlay in the iframe")
    r.wait_for(lambda: top.locator(OVERLAY).count() == 1, "the overlay in the tab")
    assert framed.locator(OVERLAY).count() == 0  # nothing on the employer page itself

    # Unload the extension: each content script's port disconnects, runtime.id clears, and
    # about 250 ms later the orphan removes its overlay (the check is deferred on purpose).
    r.cdp.send("Extensions.uninstall", {"id": r.ext_id})
    r.ctx.pages[0].wait_for_timeout(1500)
    assert inner.locator(OVERLAY).count() == 0 and top.locator(OVERLAY).count() == 0
    r._ext_page = None

    # Load it back: onInstalled re-injects into the open ATS tab. It is unpaired now
    # (uninstalling clears storage), so it draws nothing until you pair again.
    got = r.cdp.send("Extensions.loadUnpacked", {"path": str(r.ext_dir)})
    assert got["id"] == r.ext_id  # the pinned key keeps the ID
    r.wait_for(lambda: r.frames(tab), "the re-injected frame to announce itself")
    frames = r.frames(tab)
    assert list(frames) == [0] and frames[0]["ats"] == "greenhouse"
    assert top.locator(OVERLAY).count() == 0
    r.pair()
    r.wait_for(lambda: top.locator(OVERLAY).count() == 1, "the new script's overlay")
    r.ctx.pages[0].wait_for_timeout(600)  # time for a second, unwanted host to show up
    assert top.locator(OVERLAY).count() == 1  # exactly one: the orphan's is gone, not doubled
    assert framed.locator(OVERLAY).count() == 0
    framed.close()
    top.close()
