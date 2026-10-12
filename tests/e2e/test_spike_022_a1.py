"""Spec 022 ticket A1 (git-bug 9e2c202): offline extension spike.

Runs a throwaway extension (``spike_022_a1/ext``, not the product extension) against hand-written
fixture pages served by ``context.route`` on real ATS hostnames, with every non-loopback name
unresolvable. Each test answers one A1 question; the findings are written up in
``specs/022-extension-apply-assist.md`` ("A1 spike findings"). Fixture pages encode our own model
of an ATS, so none of this proves anything about a real ATS (that is the live check).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

from .spike_022_a1 import pages as P

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent / "spike_022_a1"
FLAGS = ["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]
TOP = f"{P.EMP}/careers/1"
STANDALONE = f"{P.GH}/boardsynthetic/jobs/7001"
EMBED = f"{P.GH}/embed/job_app?for=boardsynthetic&token=7001"
REACT = f"{P.JB}/boardsynthetic/jobs/7002"
WDURL = f"{P.WD}/en-US/careers/start"

SERVED = {
    TOP: P.EMPLOYER,
    EMBED: P.FORM,
    STANDALONE: P.FORM,
    REACT: P.REACT,
    f"{P.EMP}/steps": P.EMPLOYER_STEPS,
    f"{P.GH}/embed/step1": P.STEP1,
    f"{P.GH}/embed/step2": P.STEP2,
    WDURL: P.PLAIN,
}


def resume_bytes(n: int) -> bytes:
    """The worker's LCG, so the test knows the expected bytes without a round trip."""
    out = bytearray(n)
    x = 12345
    for i in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = x & 0xFF
    return bytes(out)


class Spike:
    def __init__(self, pw, tmp: Path, ext_dir: Path, name: str, cdp_load: bool = False):
        """``cdp_load`` loads the extension through CDP ``Extensions.loadUnpacked`` instead of
        ``--load-extension``: only an extension loaded that way survives ``runtime.reload()``
        (a command-line one is left unloaded), which the reload tests need."""
        self.blocked: list[str] = []
        self.posts: list[str] = []
        self.ext_dir = ext_dir
        args = [*FLAGS]
        if not cdp_load:
            args += [f"--disable-extensions-except={ext_dir}", f"--load-extension={ext_dir}"]
        self.ctx = pw.chromium.launch_persistent_context(
            str(tmp / name),
            channel="chromium",
            headless=True,
            args=args,
            ignore_default_args=["--disable-extensions"] if cdp_load else None,
        )
        if cdp_load:
            bc = self.ctx.browser.new_browser_cdp_session()
            bc.send("Extensions.loadUnpacked", {"path": str(ext_dir)})

        def router(route):
            url = route.request.url.split("#", 1)[0]
            host = urlparse(url).hostname or ""
            if host == "127.0.0.1" or url.startswith(("chrome-extension://", "data:")):
                return route.continue_()
            if "/status/" in url:
                status = 422 if url.endswith("/422") else 200
                return route.fulfill(status=status, content_type="text/plain", body="x")
            if url.endswith("/apply"):  # a form POST that escapes preventDefault lands here
                self.posts.append(url)
                return route.fulfill(status=200, content_type="text/html", body="<p>posted</p>")
            if url in SERVED:
                return route.fulfill(status=200, content_type="text/html", body=SERVED[url])
            self.blocked.append(url)
            return route.abort()

        self.ctx.route("**/*", router)
        sw = (
            self.ctx.service_workers[0]
            if self.ctx.service_workers
            else self.ctx.wait_for_event("serviceworker")
        )
        self.ext_id = urlparse(sw.url).hostname
        self._ctl = None

    def ctl(self):
        if self._ctl is None or self._ctl.is_closed():
            for attempt in range(20):  # right after a reload the extension needs a moment
                self._ctl = self.ctx.new_page()
                try:
                    self._ctl.goto(f"chrome-extension://{self.ext_id}/ctl.html")
                    break
                except Exception:
                    self._ctl.close()
                    time.sleep(0.5)
                    assert attempt < 19, "the extension never came back"
        return self._ctl

    def js(self, expr: str, arg=None):
        return self.ctl().evaluate(expr, arg)

    def session(self, key: str):
        return self.js("(k) => chrome.storage.session.get(k).then((x) => x[k] || [])", key)

    def open(self, url: str):
        page = self.ctx.new_page()
        page.goto(url)
        page.wait_for_timeout(600)  # document_idle injection plus first placement
        return page

    def tab_id(self, url: str) -> int:
        ids = self.js(
            "(u) => chrome.tabs.query({}).then("
            "(ts) => ts.filter((t) => t.url === u).map((t) => t.id))",
            url,
        )
        assert ids, f"no tab for {url}"
        return ids[-1]

    def frame_id(self, tab_url: str, frame_url_prefix: str | None) -> int:
        if frame_url_prefix is None:
            return 0
        tab = self.tab_id(tab_url)
        hs = [
            h
            for h in self.session("hellos")
            if h["tab"] == tab and h["url"].startswith(frame_url_prefix)
        ]
        assert hs, f"no frame hello for {frame_url_prefix}"
        return hs[-1]["frameId"]

    def msg(self, tab_url: str, frame_prefix: str | None, message: dict):
        tab = self.tab_id(tab_url)
        fid = self.frame_id(tab_url, frame_prefix)
        return self.js(
            "([t, f, m]) => chrome.tabs.sendMessage(t, m, {frameId: f})", [tab, fid, message]
        )

    def worker_msg(self, message: dict):
        return self.js("(m) => chrome.runtime.sendMessage(m)", message)

    def click_overlay(self, page, tab_url: str, frame_prefix: str | None, key: str):
        p = self.msg(tab_url, frame_prefix, {"type": "probe"})
        r = p["rects"][key]
        ox = oy = 0
        if frame_prefix is not None:
            box = page.locator("#ats").bounding_box()
            ox, oy = box["x"], box["y"]
        page.mouse.click(ox + r["x"] + r["w"] / 2, oy + r["y"] + r["h"] / 2)
        page.wait_for_timeout(500)

    def reload_extension(self, page, load_back: bool = True):
        """What the Reload button does: unload, then load the same unpacked path, through the
        browser CDP session. ``runtime.reload()`` itself is not usable here: in this headless
        Chromium it unloads the extension and never brings it back (even when loaded by CDP)."""
        bc = self.ctx.browser.new_browser_cdp_session()
        bc.send("Extensions.uninstall", {"id": self.ext_id})
        page.wait_for_timeout(500)
        self._ctl = None
        if load_back:
            got = bc.send("Extensions.loadUnpacked", {"path": str(self.ext_dir)})
            assert got["id"] == self.ext_id  # the pinned key keeps the ID

    def close(self):
        self.ctx.close()


def make_ext(tmp: Path, name: str, extra_hosts: list[str] | None = None) -> Path:
    ext = tmp / name
    shutil.copytree(HERE / "ext", ext)
    # A pinned key (the product's public key) keeps the extension ID across a reload.
    key = json.loads((HERE.parents[2] / "extension" / "manifest.json").read_text())["key"]
    m = json.loads((ext / "manifest.json").read_text())
    m["key"] = key
    m["host_permissions"] += extra_hosts or []
    (ext / "manifest.json").write_text(json.dumps(m))
    return ext


@pytest.fixture(scope="module")
def sp(tmp_path_factory, pw):
    tmp = tmp_path_factory.mktemp("spike022")
    s = Spike(pw, tmp, make_ext(tmp, "ext"), "profile")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _offline(request):
    yield
    if "sp" in request.fixturenames:
        sp = request.getfixturevalue("sp")
        assert not sp.blocked, f"requests outside the fixtures: {sp.blocked}"


def note(label: str, value) -> None:
    print(f"SPIKE {label}: {json.dumps(value, default=str)}")


# ---- 1. content scripts on ATS hosts, an embedded iframe, frameId, responseStatus ----


def test_content_scripts_run_in_top_and_embedded_frames(sp):
    page = sp.open(TOP)
    hellos = [h for h in sp.session("hellos") if h["tab"] == sp.tab_id(TOP)]
    urls = {h["url"]: h["frameId"] for h in hellos}
    note("frames", urls)
    assert urls[TOP] == 0
    assert urls[EMBED] != 0  # the ATS iframe on another host reports its own URL and frame id
    top = sp.msg(TOP, None, {"type": "probe"})
    emb = sp.msg(TOP, EMBED, {"type": "probe"})
    assert top["hostCount"] == 1 and emb["hostCount"] == 1  # one overlay host per frame
    assert "resume" in emb["rects"] and "cover_letter" in emb["rects"]
    assert top["rects"] == {}  # nothing to place on the employer page itself
    page.close()


def test_frame_id_is_stable_across_a_frame_navigation(sp):
    page = sp.open(f"{P.EMP}/steps")
    before = sp.frame_id(f"{P.EMP}/steps", f"{P.GH}/embed/step1")
    page.evaluate(f"document.getElementById('ats').src = '{P.GH}/embed/step2'")
    page.wait_for_timeout(800)
    after = sp.frame_id(f"{P.EMP}/steps", f"{P.GH}/embed/step2")
    note("frameId", {"before": before, "after": after})
    assert before == after
    docs = {
        h["documentId"]
        for h in sp.session("hellos")
        if h["url"] in (f"{P.GH}/embed/step1", f"{P.GH}/embed/step2")
    }
    assert len(docs) >= 2  # same frameId, new documentId: key sessions on the frame, not the doc
    page.close()


def test_response_status_visible_to_a_content_script_in_the_frame(sp):
    page = sp.open(TOP)
    frame = next(f for f in page.frames if f.url == EMBED)
    ok_url, bad_url, cross = (f"{P.GH}/status/ok", f"{P.GH}/status/422", f"{P.JB}/status/ok")
    for u in (ok_url, bad_url):  # the ATS page's own requests, as its form submit would be
        frame.evaluate("(u) => fetch(u)", u)
    frame.evaluate("(u) => fetch(u, {mode: 'no-cors'})", cross)
    page.wait_for_timeout(500)
    got = {
        k: sp.msg(TOP, EMBED, {"type": "resource-status", "url": u})
        for k, u in (("ok", ok_url), ("422", bad_url), ("cross-origin", cross))
    }
    # a fetch the content script makes itself does not appear in the page's timeline at all
    own = sp.msg(TOP, EMBED, {"type": "resource-status", "url": f"{P.GH}/status/own", "own": True})
    note("responseStatus", {"page-made": got, "content-script-made": own})
    assert got["ok"]["status"] == 200
    assert got["422"]["status"] == 422
    assert got["cross-origin"]["status"] == 0  # no Timing-Allow-Origin: hidden
    page.close()


# ---- 2. side panel ----


def clear_logs(sp):
    sp.js(
        "() => chrome.storage.session.remove(['panel-open', 'panel', 'window-open', 'openPopup'])"
    )


@pytest.mark.parametrize("mode", ["panel/open-sync", "panel/open-after-await"])
def test_side_panel_open_from_a_relayed_real_click(sp, mode):
    page = sp.open(STANDALONE)
    clear_logs(sp)
    sp.msg(STANDALONE, None, {"type": "set-mode", "mode": mode})
    sp.click_overlay(page, STANDALONE, None, "resume")
    res = sp.session("panel-open")
    note(f"sidePanel.open {mode}", res)
    ctxs = sp.js(
        "() => chrome.runtime.getContexts({contextTypes: ['SIDE_PANEL']})"
        ".then((c) => c.map((x) => x.documentUrl))"
    )
    note(f"SIDE_PANEL contexts after {mode}", ctxs)
    note(f"panel page loaded after {mode}", sp.session("panel"))
    pages = [p.url for p in sp.ctx.pages]
    note("pages in context", pages)
    assert res, "the worker never reached sidePanel.open"
    if mode == "panel/open-sync":
        # a click relayed by a content script keeps the gesture when open() is called first
        assert res == [{"mode": "sync", "ok": True}]
        assert sp.session("panel"), "the panel page loaded and said hello"
    else:
        # one await first and the gesture is gone
        assert res[0]["ok"] is False and "user gesture" in res[0]["err"]
        assert not sp.session("panel")
    # Playwright never lists the panel as a page, headless or not: observe it through
    # runtime.getContexts and the panel page's own message to the worker instead
    assert not any("panel.html" in u and "?tab=" not in u for u in pages)
    page.close()


def test_window_fallback_opens_a_popup_window(sp):
    page = sp.open(STANDALONE)
    clear_logs(sp)
    sp.msg(STANDALONE, None, {"type": "set-mode", "mode": "window/open"})
    sp.click_overlay(page, STANDALONE, None, "resume")
    res = sp.session("window-open")
    note("windows.create", res)
    assert res and res[-1]["ok"]
    popup = [p for p in sp.ctx.pages if "panel.html?tab=" in p.url]
    assert popup, "the fallback window's page is observable to Playwright"
    for p in popup:
        p.close()
    page.close()


def test_action_open_popup_without_a_gesture(sp):
    clear_logs(sp)
    sp.worker_msg({"type": "popup/open-nogesture"})
    sp.ctl().wait_for_timeout(500)
    res = sp.session("openPopup")
    note("action.openPopup without gesture", res)
    # Chrome 127+ allows it with no gesture; headless resolves without any observable popup
    assert res == [{"ok": True}]


# ---- 3. overlay ----


def test_overlay_survives_react_style_rerenders_and_layout_shift(sp):
    page = sp.open(REACT)
    p0 = sp.msg(REACT, None, {"type": "probe"})
    assert p0["hostCount"] == 1 and p0["hostIsLastChild"]
    y0 = p0["rects"]["resume"]["y"]
    for _ in range(5):
        page.evaluate("() => window.rerender()")
    page.wait_for_timeout(300)
    p1 = sp.msg(REACT, None, {"type": "probe"})
    assert p1["hostCount"] == 1  # no duplicates
    assert set(p1["rects"]) == {"resume", "cover_letter"}
    assert p1["rects"]["resume"] == p0["rects"]["resume"]
    page.evaluate("() => window.rerender(true)")  # a banner shifts everything down
    page.wait_for_timeout(300)
    p2 = sp.msg(REACT, None, {"type": "probe"})
    note("layout shift", {"before": y0, "after": p2["rects"]["resume"]["y"]})
    assert p2["rects"]["resume"]["y"] == pytest.approx(y0 + 86, abs=2)  # 80px banner + margin
    # the page wiping the whole body removes the host; it is re-added once
    page.evaluate("() => { document.body.innerHTML = ''; }")
    page.wait_for_timeout(300)
    page.evaluate(
        "() => { document.body.innerHTML = '<div id=\"root\"></div>'; window.rerender(); }"
    )
    page.wait_for_timeout(400)
    p3 = sp.msg(REACT, None, {"type": "probe"})
    note("after body wipe", {"hosts": p3["hostCount"], "buttons": list(p3["rects"])})
    assert p3["hostCount"] == 1 and "resume" in p3["rects"]
    page.close()


# ---- 4. setAccessLevel on storage.local ----


def test_set_access_level_hides_storage_local_from_content_scripts(sp):
    page = sp.open(STANDALONE)
    before = sp.msg(STANDALONE, None, {"type": "token-read"})
    assert sp.worker_msg({"type": "spike/set-access", "level": "TRUSTED_CONTEXTS"})["ok"]
    after = sp.msg(STANDALONE, None, {"type": "token-read"})
    write = sp.msg(STANDALONE, None, {"type": "token-set-attempt"})
    ext_page = sp.js("() => chrome.storage.local.get('token')")  # a trusted extension page
    note("storage.local", {"default": before, "trusted": after, "write": write, "ext": ext_page})
    sp.worker_msg({"type": "spike/set-access", "level": "TRUSTED_AND_UNTRUSTED_CONTEXTS"})
    assert before == {"value": "SECRET-TOKEN"}  # the default level lets content scripts read it
    assert after.get("error") or after.get("value") is None
    assert "value" not in after or after["value"] is None
    assert ext_page == {"token": "SECRET-TOKEN"}  # trusted contexts still read it
    page.close()


# ---- 5. base64 transfer and DataTransfer attach ----


@pytest.mark.parametrize("size", [64 * 1024, 3 * 1024 * 1024])
def test_resume_arrives_byte_identical_through_base64_messaging(sp, size):
    page = sp.open(TOP)
    frame = next(f for f in page.frames if f.url == EMBED)
    res = sp.msg(TOP, EMBED, {"type": "attach", "selector": "#resume", "size": size})
    frame.wait_for_function("window.__fileHash !== null")
    got = frame.evaluate("() => [window.__fileHash, window.__fileSize, window.__submits]")
    want = hashlib.sha256(resume_bytes(size)).hexdigest()
    note("attach", {"size": size, "res": res, "hash_ok": got[0] == want})
    assert res == {"attached": 1, "size": size, "name": "resume.pdf"}
    assert got == [want, size, 0]  # the page's own change handler saw identical bytes; no submit
    # other file fields in the frame are untouched
    assert frame.evaluate("() => document.getElementById('resume').files.length") == 1
    page.close()


# ---- 6. safeDispatch ----


def test_safe_dispatch_refuses_every_submit_like_target(sp):
    page = sp.open(STANDALONE)
    clicks = sp.msg(
        STANDALONE,
        None,
        {"type": "safe-dispatch", "event": "click", "selectors": P.SUBMIT_LIKE + P.SAFE},
    )
    note("safeDispatch", clicks)
    for sel in P.SUBMIT_LIKE:
        assert "refused" in clicks[sel], sel
    for sel in P.SAFE:
        assert clicks[sel] == {"dispatched": True}, sel
    sub = sp.msg(
        STANDALONE, None, {"type": "safe-dispatch", "event": "submit", "selectors": ["#f"]}
    )
    enter = sp.msg(
        STANDALONE, None, {"type": "safe-dispatch", "event": "keydown-enter", "selectors": ["#t2"]}
    )
    assert sub["#f"]["refused"] and enter["#t2"]["refused"]
    counters = page.evaluate("() => [window.__submits, window.__events.length]")
    assert counters == [0, 0]  # nothing reached the page's submit or watched-click listeners
    page.close()


# ---- 7. isTrusted and userActivation on submits ----


def submit_case(sp, how: str):
    page = sp.open(STANDALONE)
    if how == "real-click":
        page.click("#submit_btn")
    elif how == "real-click-outside":
        page.click("#outside")
    elif how == "real-enter":
        page.click("#t2")
        page.wait_for_timeout(6500)  # let the click's activation lapse (about 5 s)
        page.keyboard.press("Enter")
    elif how.startswith("page-"):
        # Playwright's evaluate carries a user gesture and a browser-initiated goto activates the
        # frame, so the script action is deferred until both have lapsed (about 5 s).
        js = {
            "page-click": "document.getElementById('submit_btn').click()",
            "page-request-submit": "document.getElementById('f').requestSubmit()",
            "page-request-submit-with-submitter": "document.getElementById('f').requestSubmit("
            "document.getElementById('submit_btn'))",
            "page-form-submit": "document.getElementById('f').submit()",
        }[how]
        page.evaluate("(js) => { setTimeout(() => new Function(js)(), 6000); }", js)
        page.wait_for_timeout(6500)
    elif how.startswith("content-script-"):
        page.wait_for_timeout(6000)
        # a runtime message sent from a gesture-carrying driver propagates that gesture to the
        # receiver, so the content script waits it out before acting
        if how == "content-script-click":
            m = {"type": "raw-click", "selector": "#submit_btn", "delay": 6000}
        else:
            m = {"type": "raw-request-submit", "selector": "#f", "delay": 6000}
        sp.msg(STANDALONE, None, m)
    elif how == "request-submit-after-real-click":
        page.wait_for_timeout(6000)
        page.click("#cb")  # a genuine gesture, then a script submit inside the 5 s window
        page.evaluate(
            "() => { setTimeout(() => document.getElementById('f').requestSubmit(), 800); }"
        )
        page.wait_for_timeout(1200)
    page.wait_for_timeout(300)
    events = page.evaluate("() => window.__events")
    cs = sp.msg(STANDALONE, None, {"type": "probe"})["submits"]
    page.close()
    return events, cs


def test_submit_signals_separate_user_submits_from_script_submits(sp):
    out = {
        how: submit_case(sp, how)
        for how in (
            "real-click",
            "real-click-outside",
            "real-enter",
            "page-click",
            "page-request-submit",
            "page-request-submit-with-submitter",
            "page-form-submit",
            "content-script-click",
            "content-script-request-submit",
            "request-submit-after-real-click",
        )
    }
    for how, (events, cs) in out.items():
        note(f"submit {how}", {"page": events, "content-script": cs})
    sub = {h: [e for e in ev if e["type"] == "submit"] for h, (ev, _) in out.items()}
    clk = {h: [e for e in ev if e["type"] == "click"] for h, (ev, _) in out.items()}
    # a real click: trusted click, activation active, submitter is the clicked control
    assert clk["real-click"][0]["isTrusted"] and clk["real-click"][0]["active"]
    assert sub["real-click"][0]["submitter"] == "submit_btn" and sub["real-click"][0]["active"]
    assert sub["real-click-outside"][0]["submitter"] == "outside"
    # script paths: the click is untrusted and there is no activation
    assert not clk["page-click"][0]["isTrusted"] and not clk["page-click"][0]["active"]
    assert not sub["page-click"][0]["active"]
    assert sub["page-request-submit"][0]["submitter"] is None
    assert not sub["page-request-submit"][0]["active"]
    assert sub["page-request-submit-with-submitter"][0]["submitter"] == "submit_btn"
    assert sub["page-form-submit"] == []  # form.submit() fires no submit event at all
    assert not clk["content-script-click"][0]["isTrusted"]
    assert not sub["content-script-click"][0]["active"]
    # a script submit shortly after any real click still sees activation: not enough alone
    assert sub["request-submit-after-real-click"][0]["active"]
    assert sub["request-submit-after-real-click"][0]["submitter"] is None
    # Enter in a text field after the activation lapsed
    assert sub["real-enter"][0]["submitter"] == "submit_btn"
    # the content script (isolated world) sees the same values as the page
    for how in ("real-click", "page-request-submit", "content-script-click"):
        cs_sub = next(e for e in out[how][1] if e["kind"] == "submit")
        assert cs_sub["active"] == sub[how][0]["active"]
        assert cs_sub["submitter"] == sub[how][0]["submitter"]


# ---- 8. registerContentScripts for an optional host ----


def test_register_content_scripts_needs_the_host_granted(sp, tmp_path_factory, pw):
    reg = sp.worker_msg({"type": "spike/register", "matches": [f"{P.WD}/*"]})
    granted = sp.worker_msg({"type": "spike/has-permission", "origins": [f"{P.WD}/*"]})
    err = sp.js(
        "() => Promise.race([chrome.permissions.request({origins: ['https://*.myworkdayjobs.com/*']})"
        ".then((r) => ({r}), (e) => ({err: String(e)})),"
        " new Promise((r) => setTimeout(() => r({hung: 'no answer in 4 s'}), 4000))])"
    )
    page = sp.open(WDURL)
    ungranted_hellos = [h for h in sp.session("hellos") if h["url"] == WDURL]
    page.close()
    note(
        "optional host",
        {"register": reg, "contains": granted, "request": err, "hellos": ungranted_hellos},
    )
    assert not ungranted_hellos  # registered, but nothing injects until the host is granted
    tmp = tmp_path_factory.mktemp("spike022b")
    s2 = Spike(pw, tmp, make_ext(tmp, "ext2", [f"{P.WD}/*"]), "profile2")  # stands in for a grant
    try:
        reg2 = s2.worker_msg({"type": "spike/register", "matches": [f"{P.WD}/*"]})
        page2 = s2.open(WDURL)
        hellos = [h for h in s2.session("hellos") if h["url"] == WDURL]
        probe = s2.msg(WDURL, None, {"type": "probe"})
        note(
            "granted host", {"register": reg2, "hellos": len(hellos), "rects": list(probe["rects"])}
        )
        assert reg2["ok"] and hellos and "resume" in probe["rects"]
        page2.close()
    finally:
        s2.close()


# ---- 9. reload, orphan removal, idle worker stop ----


@pytest.fixture
def rl(tmp_path_factory, pw):
    tmp = tmp_path_factory.mktemp("spike022r")
    s = Spike(pw, tmp, make_ext(tmp, "ext"), "profile", cdp_load=True)
    yield s
    s.close()


def host_state(page_or_frame):
    return page_or_frame.evaluate(
        "() => [...document.querySelectorAll('[data-jh-host]')]"
        ".map((h) => h.getAttribute('data-jh-inst'))"
    )


def test_reload_orphans_remove_themselves_without_reinjection(rl):
    page = rl.open(TOP)
    frame = next(f for f in page.frames if f.url == EMBED)
    before = (host_state(page), host_state(frame))
    rl.worker_msg({"type": "spike/set-flag", "key": "noreinject", "value": True})
    rl.reload_extension(page, load_back=False)
    page.wait_for_timeout(2000)
    after = (host_state(page), host_state(frame))
    trace = page.evaluate("() => document.documentElement.getAttribute('data-jh-disc')")
    note("reload without reinjection", {"before": before, "after": after, "disconnect": trace})
    assert len(before[0]) == 1 and len(before[1]) == 1
    # onDisconnect fires with runtime.id still set; it clears within ~50 ms, so the deferred
    # check removes the host. (A check inside the handler itself would have kept it.)
    assert trace.split(":")[2].startswith("true,")
    assert after == ([], [])
    page.close()


def test_reload_reinjects_into_open_tabs_and_frames_and_replaces_the_overlay(rl):
    page = rl.open(TOP)
    frame = next(f for f in page.frames if f.url == EMBED)
    before = (host_state(page), host_state(frame))
    rl.reload_extension(page)
    page.wait_for_timeout(3000)
    after = (host_state(page), host_state(frame))
    installed = rl.js("() => chrome.storage.local.get('installed')")
    note("reload with reinjection", {"before": before, "after": after, "installed": installed})
    assert len(after[0]) == 1 and len(after[1]) == 1  # exactly one host per frame, never two
    assert after[1][0] != before[1][0]  # and it is the new script's, not the orphan's
    # the fresh script works: its button sits on the field and a real click opens the panel
    rl.js("() => chrome.storage.session.remove('panel-open')")
    rl.click_overlay(page, TOP, EMBED, "resume")
    assert rl.session("panel-open")
    page.close()


def test_idle_worker_stop_disconnects_ports_and_the_script_reconnects_lazily(rl):
    page = rl.open(STANDALONE)
    p0 = rl.msg(STANDALONE, None, {"type": "probe"})
    cdp = rl.ctx.new_cdp_session(page)
    cdp.send("ServiceWorker.enable")
    cdp.send("ServiceWorker.stopAllWorkers")
    page.wait_for_timeout(1500)
    p1 = rl.msg(STANDALONE, None, {"type": "probe"})
    note(
        "worker stop",
        {
            "connected before": p0["port"],
            "after": p1["port"],
            "disconnects": p1["portDisconnects"],
            "runtimeId": p1["runtimeId"],
            "hostCount": p1["hostCount"],
            "connects": p1["portConnects"],
            "workers_alive": len(rl.ctx.service_workers),
        },
    )
    assert p0["port"] and not p1["port"] and p1["portDisconnects"] == 1
    assert p1["runtimeId"] and p1["hostCount"] == 1  # runtime.id still set: keep the overlay
    assert p1["portConnects"] == 1  # idle: no reconnect, so the worker is not woken by it
    rl.js("() => chrome.storage.session.remove('panel-open')")
    rl.click_overlay(page, STANDALONE, None, "resume")  # the next need reconnects
    p2 = rl.msg(STANDALONE, None, {"type": "probe"})
    res = rl.session("panel-open")
    note("after click on cold worker", {"connects": p2["portConnects"], "panel-open": res})
    assert p2["portConnects"] == 2 and p2["port"]
    page.close()
