// jobhunter content script for the supported ATS frames (specs/022 "Architecture", "Where the
// buttons appear"). Declared in the manifest for the required ATS hosts (all frames, document
// idle), re-injected by the worker after an extension reload.
//
// What it does on load: announce this frame to the worker, ask the worker for this ATS's form
// data and switches, and draw the resume and letter buttons in one body-level overlay. It writes
// no field, reads no field value, uses no network API and sends nothing that carries page data.
// Every spend or write message belongs to extension pages (the worker's MESSAGE_POLICY).

(() => {
  if (globalThis.__jhFrameCleanup) {
    globalThis.__jhFrameCleanup(); // a second injection into the same world replaces this one
  }
  const HOST_ATTR = "data-jobhunter-overlay";
  const BUTTON_LABEL = {resume: "jobhunter resume", cover_letter_file: "jobhunter letter",
    cover_letter_text: "jobhunter letter"};
  const ORPHAN_CHECK_MS = 250; // runtime.id is still set inside onDisconnect after a reload

  const listeners = []; // [target, type, fn, options]: everything we add outside our own host
  const observers = [];
  const timers = [];
  const state = {
    orphaned: false,
    forms: null, // this ATS's form data from the worker
    switches: {show_buttons: false, record_submits: false},
    port: null,
    portConnects: 0,
    announced: false,
  };
  let host = null;
  let root = null;
  let queued = false;

  const alive = () => Boolean(chrome.runtime && chrome.runtime.id);

  function on(target, type, fn, options) {
    target.addEventListener(type, fn, options);
    listeners.push([target, type, fn, options]);
  }

  // ── the worker channel ──────────────────────────────────────────────────────

  // Opened at start; a disconnect is either an extension reload (the id clears within about
  // 50 ms) or an idle worker stop (the id stays; reconnect lazily when we next need the worker).
  function ensurePort() {
    if (state.port || !alive()) {
      return;
    }
    let port;
    try {
      port = chrome.runtime.connect({name: "frame"});
    } catch (e) {
      return;
    }
    state.port = port;
    state.portConnects += 1;
    port.onDisconnect.addListener(() => {
      state.port = null;
      timers.push(setTimeout(() => {
        if (!alive()) {
          cleanup();
        }
      }, ORPHAN_CHECK_MS));
    });
  }

  async function send(msg) {
    if (!alive() || state.orphaned) {
      return {state: "orphan"};
    }
    ensurePort(); // the lazy reconnect point: only when we need the worker
    try {
      const got = await chrome.runtime.sendMessage(msg);
      if (got && got.state === "unannounced" && msg.type !== "frame/hello") {
        await chrome.runtime.sendMessage({type: "frame/hello"});
        return await chrome.runtime.sendMessage(msg);
      }
      return got || {state: "error"};
    } catch (e) {
      return {state: "error"};
    }
  }

  // ── the overlay ─────────────────────────────────────────────────────────────

  const CSS = `
    :host { all: initial; }
    button {
      position: absolute; pointer-events: auto; display: inline-flex; align-items: center;
      gap: 4px; margin: 0; padding: 2px 8px; font: 12px/1.4 system-ui, sans-serif;
      color: #1b3a2a; background: #e8f3ec; border: 1px solid #5a9a73; border-radius: 4px;
      cursor: pointer; z-index: 2147483647; white-space: nowrap;
    }
    button:hover { background: #d6ebdd; }
    button:focus-visible { outline: 2px solid #1a73e8; outline-offset: 1px; }
    svg { width: 12px; height: 12px; fill: none; stroke: currentColor; stroke-width: 2; }
    @media (prefers-color-scheme: dark) {
      button { color: #d4eadc; background: #1f3a2b; border-color: #5a9a73; }
      button:hover { background: #2a4d39; }
    }
  `;

  function icon() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", "M3 8.5 6.5 12 13 4");
    svg.appendChild(path);
    return svg;
  }

  function removeHost() {
    if (host) {
      host.remove();
    }
    host = null;
    root = null;
  }

  function ensureHost() {
    if (host && host.isConnected) {
      if (document.body.lastElementChild !== host) {
        document.body.appendChild(host); // stay the last child of body
      }
      return;
    }
    // A re-injected script replaces an overlay a previous (orphaned or earlier) script left.
    for (const old of document.querySelectorAll("[" + HOST_ATTR + "]")) {
      old.remove();
    }
    host = document.createElement("div");
    host.setAttribute(HOST_ATTR, "1");
    host.style.cssText =
      "position:absolute;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483647";
    root = host.attachShadow({mode: "closed"});
    try {
      const sheet = new CSSStyleSheet(); // adopted sheets are not blocked by the page's CSP
      sheet.replaceSync(CSS);
      root.adoptedStyleSheets = [sheet];
    } catch (e) {
      // no styling is better than inline style elements a page CSP might block
    }
    document.body.appendChild(host);
  }

  function hostMatches(name, pattern) {
    const h = String(name).toLowerCase();
    if (String(pattern).startsWith("*.")) {
      return h.endsWith(String(pattern).slice(1));
    }
    return h === pattern;
  }

  function pickVariant() {
    const variants = (state.forms && state.forms.variants) || [];
    for (const v of variants) {
      try {
        if ((v.hosts || []).some((p) => hostMatches(location.hostname, p))
            && new RegExp(v.path).test(location.pathname)) {
          return v;
        }
      } catch (e) {
        // a bad pattern in the data names nothing
      }
    }
    return null;
  }

  function rectOf(el) {
    let r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0 && el.matches("input[type=file]") && el.parentElement) {
      const p = el.parentElement; // a styled-away file input: the visible box around it
      if (p.checkVisibility && p.checkVisibility()) {
        r = p.getBoundingClientRect();
      }
    }
    return r.width === 0 && r.height === 0 ? null : r;
  }

  // The fields that get a button: named by the data, found, and visible.
  function wanted() {
    const out = new Map();
    const variant = pickVariant();
    if (!variant || !state.switches.show_buttons) {
      return out;
    }
    for (const f of variant.fields || []) {
      if (!BUTTON_LABEL[f.role]) {
        continue;
      }
      let el = null;
      try {
        el = document.querySelector(f.selector);
      } catch (e) {
        el = null;
      }
      const r = el && rectOf(el);
      if (r) {
        out.set(f.key, {role: f.role, rect: r});
      }
    }
    return out;
  }

  function makeButton(key, role) {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.key = key;
    b.dataset.role = role;
    b.setAttribute("aria-label", BUTTON_LABEL[role]);
    b.appendChild(icon());
    b.appendChild(document.createTextNode(BUTTON_LABEL[role]));
    b.addEventListener("click", (ev) => {
      if (!ev.isTrusted || !alive() || state.orphaned) {
        return; // a page that clicks our button, or a stale button after a reload: nothing
      }
      send({type: "panel/open", field: key, role});
    });
    return b;
  }

  // Builds, removes and re-places the buttons. The host exists only while it has something to
  // show: with Show buttons off (and nothing else to draw) the page carries no element of ours.
  function sync() {
    queued = false;
    if (state.orphaned || !document.body) {
      return;
    }
    const want = wanted();
    if (want.size === 0) {
      removeHost();
      return;
    }
    ensureHost();
    for (const b of [...root.querySelectorAll("button")]) {
      if (!want.has(b.dataset.key)) {
        b.remove();
      }
    }
    const dx = window.scrollX;
    const dy = window.scrollY;
    for (const [key, {role, rect}] of want) {
      let b = root.querySelector('button[data-key="' + cssEsc(key) + '"]');
      if (!b) {
        b = makeButton(key, role);
        root.appendChild(b);
      }
      b.style.left = Math.max(0, rect.right + dx + 6) + "px";
      b.style.top = Math.max(0, rect.top + dy) + "px";
    }
  }

  function cssEsc(s) {
    return String(s).replace(/["\\]/g, "\\$&");
  }

  function schedule() {
    if (queued || state.orphaned) {
      return;
    }
    queued = true;
    requestAnimationFrame(sync);
  }

  // ── start, switches, cleanup ────────────────────────────────────────────────

  async function learn() {
    const got = await send({type: "forms/get"});
    if (state.orphaned) {
      return;
    }
    state.forms = got && got.state === "ok" ? got.forms : null;
    const sw = (got && got.switches) || {};
    state.switches = {show_buttons: sw.show_buttons === true, record_submits: sw.record_submits === true};
    schedule();
  }

  function cleanup() {
    if (state.orphaned) {
      return;
    }
    state.orphaned = true;
    for (const [target, type, fn, options] of listeners) {
      target.removeEventListener(type, fn, options);
    }
    listeners.length = 0;
    for (const o of observers) {
      o.disconnect();
    }
    observers.length = 0;
    for (const t of timers) {
      clearTimeout(t);
    }
    timers.length = 0;
    removeHost();
    if (globalThis.__jhFrameCleanup === cleanup) {
      globalThis.__jhFrameCleanup = null;
    }
  }
  globalThis.__jhFrameCleanup = cleanup;

  // Read-only view for the e2e harness; it lives in this extension's isolated world, which
  // page scripts cannot reach.
  globalThis.__jhFrame = {
    buttons() {
      if (!root) {
        return [];
      }
      return [...root.querySelectorAll("button")].map((b) => {
        const r = b.getBoundingClientRect();
        return {key: b.dataset.key, role: b.dataset.role, text: b.textContent,
          x: r.x, y: r.y, w: r.width, h: r.height};
      });
    },
    state() {
      return {orphaned: state.orphaned, switches: state.switches, port: Boolean(state.port),
        portConnects: state.portConnects, hasHost: Boolean(host), hasForms: Boolean(state.forms)};
    },
  };

  // The worker tells us when the switches or the pairing changed.
  chrome.runtime.onMessage.addListener((msg, sender) => {
    if (state.orphaned || sender.id !== chrome.runtime.id || sender.tab) {
      return false; // only the worker; a page cannot reach a content script anyway
    }
    if (msg && msg.type === "forms/changed") {
      learn();
    }
    return false;
  });

  function start() {
    if (!document.body) {
      return;
    }
    ensurePort();
    for (const old of document.querySelectorAll("[" + HOST_ATTR + "]")) {
      old.remove(); // an orphan's overlay from before an extension reload
    }
    send({type: "frame/hello"}).then((got) => {
      state.announced = Boolean(got && got.state === "ok");
      return learn();
    });
    const mo = new MutationObserver(schedule);
    mo.observe(document.body, {childList: true, subtree: true, attributes: true,
      attributeFilter: ["style", "class", "hidden", "disabled", "type"]});
    observers.push(mo);
    const ro = new ResizeObserver(schedule);
    ro.observe(document.documentElement);
    observers.push(ro);
    on(window, "scroll", schedule, {passive: true});
    on(window, "resize", schedule);
    on(window, "pagehide", () => {
      if (alive() && !state.orphaned) {
        chrome.runtime.sendMessage({type: "frame/bye"}).catch(() => {});
      }
    });
  }

  if (document.body) {
    start();
  } else {
    on(document, "DOMContentLoaded", start);
  }
})();
