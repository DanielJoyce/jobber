// Spike content script (spec 022 A1). Throwaway; models the A2 overlay, orphan removal,
// lazy reconnect, safeDispatch and the submit signals just enough to measure Chromium.
(() => {
  if (globalThis.__jhCleanup) globalThis.__jhCleanup(); // same world re-injection
  const INST = Math.random().toString(36).slice(2);
  const listeners = []; // [target, type, fn, opts]
  const on = (t, type, fn, opts) => {
    t.addEventListener(type, fn, opts);
    listeners.push([t, type, fn, opts]);
  };
  const state = {
    gen: (globalThis.__jhGen = (globalThis.__jhGen || 0) + 1),
    url: location.href,
    isTop: window.top === window,
    frameId: null,
    port: null,
    portConnects: 0,
    portDisconnects: 0,
    orphaned: false,
    mode: "panel/open-sync",
    clicks: [],
    submits: [],
    placements: 0,
  };
  let host = null;
  let root = null;
  let observers = [];
  let timers = [];

  function cleanup() {
    state.orphaned = true;
    for (const [t, type, fn, opts] of listeners) t.removeEventListener(type, fn, opts);
    listeners.length = 0;
    for (const o of observers) o.disconnect();
    observers = [];
    for (const t of timers) clearTimeout(t);
    if (host) host.remove();
    host = null;
    root = null;
  }
  globalThis.__jhCleanup = cleanup;

  // ---- worker channel ----
  function alive() {
    return !!(chrome.runtime && chrome.runtime.id);
  }
  function ensurePort() {
    if (state.port || !alive()) return;
    const port = chrome.runtime.connect({name: "frame"});
    state.portConnects++;
    state.port = port;
    port.onDisconnect.addListener(() => {
      state.portDisconnects++;
      state.port = null;
      // visible to the test from the page's main world (an orphan cannot be messaged)
      const trace = [`${alive()}`];
      const mark = () => document.documentElement.setAttribute("data-jh-disc", `${INST}:${state.portDisconnects}:${trace.join(",")}`);
      mark();
      for (const ms of [50, 500, 2000]) {
        setTimeout(() => {
          trace.push(`${ms}ms=${alive()}`);
          mark();
        }, ms);
      }
      // runtime.id is still set inside onDisconnect after a reload and clears within ~50 ms,
      // so the check is deferred. Idle worker stop: id stays set, keep the overlay.
      timers.push(setTimeout(() => { if (!alive()) cleanup(); }, 250));
      // otherwise (idle worker stop): keep the overlay, reconnect lazily in ensurePort()
    });
  }
  function send(msg) {
    if (!alive()) return Promise.resolve({orphan: true});
    ensurePort(); // lazy reconnect point: only when we need the worker
    return chrome.runtime.sendMessage(msg);
  }

  // ---- overlay ----
  const TARGETS = "input[type=file], textarea#cover_letter";
  function placeAll() {
    if (!root) return;
    const fields = [...document.querySelectorAll(TARGETS)];
    const wanted = new Map();
    for (const f of fields) {
      const r = f.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const key = f.id || f.name;
      wanted.set(key, r);
    }
    for (const b of [...root.querySelectorAll("button")]) {
      if (!wanted.has(b.dataset.key)) b.remove();
    }
    for (const [key, r] of wanted) {
      let b = root.querySelector(`button[data-key="${key}"]`);
      if (!b) {
        b = document.createElement("button");
        b.type = "button";
        b.dataset.key = key;
        b.textContent = "jobhunter";
        b.style.cssText = "position:absolute;pointer-events:auto;font:12px sans-serif;z-index:2147483647";
        b.addEventListener("click", (ev) => {
          if (!ev.isTrusted || !alive()) {
            state.clicks.push({key, ignored: true, trusted: ev.isTrusted});
            return;
          }
          state.clicks.push({key, trusted: true, active: navigator.userActivation.isActive});
          send({type: state.mode});
        });
        root.appendChild(b);
      }
      b.style.left = `${r.right + window.scrollX + 4}px`;
      b.style.top = `${r.top + window.scrollY}px`;
      state.placements++;
    }
    host.style.cssText = "position:absolute;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483647";
  }
  function ensureHost() {
    for (const old of document.querySelectorAll("[data-jh-host]")) {
      if (old !== host) old.remove(); // a re-injected script replaces an existing host
    }
    if (host && host.isConnected) return;
    host = document.createElement("div");
    host.setAttribute("data-jh-host", String(state.gen));
    host.setAttribute("data-jh-inst", INST);
    root = host.attachShadow({mode: "closed"});
    document.body.appendChild(host);
  }
  function start() {
    if (!document.body) return;
    ensureHost();
    placeAll();
    let queued = false;
    const replace = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        if (state.orphaned) return;
        if (!host || !host.isConnected) {
          host = null;
          ensureHost();
        }
        placeAll();
      });
    };
    const mo = new MutationObserver(replace);
    mo.observe(document.body, {childList: true, subtree: true, attributes: true});
    observers.push(mo);
    const ro = new ResizeObserver(replace);
    ro.observe(document.documentElement);
    observers.push(ro);
    on(window, "scroll", replace, {passive: true});
    on(window, "resize", replace);
  }

  // ---- safeDispatch ----
  const SUBMITISH = /(^|\b)(submit|apply|send application|finish)\b/i;
  function submitLike(el) {
    if (!(el instanceof Element)) return "not-an-element";
    if (el.matches("form")) return "form";
    if (el.matches("button:not([type=button]):not([type=reset]), input[type=submit], input[type=image]")) return "submit-control";
    if (el.hasAttribute("formaction") || el.hasAttribute("formmethod")) return "formaction";
    if (el.matches("[type=submit]")) return "type-submit";
    if (el.matches("label") && el.control) return submitLike(el.control) && "label-for-submit";
    if (el.matches("a, [role=button], div, span, button")) {
      const text = (el.textContent || "").trim();
      if (SUBMITISH.test(text) || SUBMITISH.test(el.getAttribute("aria-label") || "")) return "submit-text";
    }
    if (el.querySelector("button:not([type=button]):not([type=reset]), input[type=submit], input[type=image], [type=submit]")) return "contains-submit";
    return "";
  }
  function safeDispatch(el, event) {
    if (event.type === "submit") return {refused: "event-submit"};
    if (event instanceof KeyboardEvent && event.key === "Enter") return {refused: "enter-key"};
    const why = submitLike(el);
    if (why) return {refused: why};
    el.dispatchEvent(event);
    return {dispatched: true};
  }

  // ---- submit signals (modelled on submit.js: observe only, never prevent) ----
  on(document, "submit", (ev) => {
    state.submits.push({
      kind: "submit",
      trusted: ev.isTrusted,
      submitter: ev.submitter ? ev.submitter.id || ev.submitter.tagName : null,
      active: navigator.userActivation.isActive,
      everActive: navigator.userActivation.hasBeenActive,
    });
  }, true);
  on(document, "click", (ev) => {
    if (ev.target instanceof Element && ev.target.closest("[data-watch]")) {
      state.submits.push({kind: "click", id: ev.target.id, trusted: ev.isTrusted, active: navigator.userActivation.isActive});
    }
  }, true);

  // ---- test hooks (messages from the extension page) ----
  function b64ToFile(r) {
    const bin = atob(r.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new File([bytes], r.name, {type: r.type});
  }
  chrome.runtime.onMessage.addListener((msg, sender, respond) => {
    if (state.orphaned) return false;
    switch (msg.type) {
      case "probe": {
        const rects = {};
        if (root) {
          for (const b of root.querySelectorAll("button")) {
            const r = b.getBoundingClientRect();
            rects[b.dataset.key] = {x: r.x, y: r.y, w: r.width, h: r.height};
          }
        }
        respond({
          ...state,
          port: !!state.port,
          hostCount: document.querySelectorAll("[data-jh-host]").length,
          hostAttr: (document.querySelector("[data-jh-host]") || {getAttribute() {}}).getAttribute("data-jh-host"),
          hostIsLastChild: !!host && document.body.lastElementChild === host,
          rects,
          runtimeId: alive(),
          scrollX: window.scrollX,
          scrollY: window.scrollY,
        });
        return false;
      }
      case "set-mode":
        state.mode = msg.mode;
        respond({ok: true});
        return false;
      case "ensure-port":
        ensurePort();
        respond({connects: state.portConnects});
        return false;
      case "token-read":
        chrome.storage.local.get("token").then(
          (v) => respond({value: v.token === undefined ? null : v.token}),
          (e) => respond({error: String(e)}),
        );
        return true;
      case "token-set-attempt":
        chrome.storage.local.set({planted: "x"}).then(
          () => respond({ok: true}),
          (e) => respond({error: String(e)}),
        );
        return true;
      case "attach":
        send({type: "spike/resume", size: msg.size}).then((r) => {
          const file = b64ToFile(r);
          const input = document.querySelector(msg.selector);
          const dt = new DataTransfer();
          dt.items.add(file);
          input.files = dt.files;
          input.dispatchEvent(new Event("input", {bubbles: true}));
          input.dispatchEvent(new Event("change", {bubbles: true}));
          respond({attached: input.files.length, size: input.files[0].size, name: input.files[0].name});
        });
        return true;
      case "safe-dispatch": {
        const out = {};
        for (const sel of msg.selectors) {
          const el = document.querySelector(sel);
          if (!el) {
            out[sel] = {missing: true};
            continue;
          }
          const ev = msg.event === "keydown-enter"
            ? new KeyboardEvent("keydown", {key: "Enter", bubbles: true})
            : msg.event === "submit"
              ? new Event("submit", {bubbles: true, cancelable: true})
              : new MouseEvent("click", {bubbles: true, cancelable: true});
          out[sel] = safeDispatch(el, ev);
        }
        respond(out);
        return false;
      }
      case "raw-click": {
        // what safeDispatch exists to prevent: an untrusted click on any element
        // delay: the driver's message itself carries a gesture (see findings), so wait it out
        setTimeout(() => {
          document.querySelector(msg.selector).dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
          respond({ok: true});
        }, msg.delay || 0);
        return true;
      }
      case "raw-request-submit": {
        setTimeout(() => {
          const f = document.querySelector(msg.selector);
          if (msg.submitter) f.requestSubmit(document.querySelector(msg.submitter));
          else f.requestSubmit();
          respond({ok: true});
        }, msg.delay || 0);
        return true;
      }
      case "resource-status": {
        // Reads the timeline only; the request was made by the page (or, with own:true, by us).
        (msg.own ? fetch(msg.url) : Promise.resolve()).then(() => {
          const e = performance.getEntriesByType("resource").filter((x) => x.name === msg.url).pop();
          respond({
            found: !!e,
            names: e ? undefined : performance.getEntriesByType("resource").map((x) => x.name),
            status: e ? e.responseStatus : null,
            hasField: e ? "responseStatus" in e : false,
          });
        });
        return true;
      }
      case "storage-level-ok":
        respond({ok: true});
        return false;
    }
    return false;
  });

  if (document.body) start();
  else on(document, "DOMContentLoaded", start);
  send({type: "frame/hello"}).then((r) => {
    if (r) state.frameId = r.frameId;
  });
  ensurePort();
})();
