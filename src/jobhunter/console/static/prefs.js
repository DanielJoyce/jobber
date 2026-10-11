// /prefs: show weight sliders normalized to 1.0 as they move (the server normalizes on save).
(function () {
  "use strict";

  function updateWeights() {
    var sliders = document.querySelectorAll("input[data-weight]");
    var total = 0;
    sliders.forEach(function (s) { total += Number(s.value) || 0; });
    sliders.forEach(function (s) {
      var key = s.name.replace(/^w\./, "");
      var out = document.querySelector('[data-weight-out="' + key + '"]');
      if (!out) return;
      var share = total > 0 ? (Number(s.value) || 0) / total : 0;
      out.textContent = share.toFixed(2);
    });
  }

  // Section help: a "?" button toggles a region; open/closed is remembered per section.
  function store(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable: the page works without it */ }
  }
  function remembered(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function setHelp(btn, open) {
    var region = document.getElementById(btn.getAttribute("aria-controls"));
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (region) region.hidden = !open;
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("button[data-help]") : null;
    if (!btn) return;
    var open = btn.getAttribute("aria-expanded") !== "true";
    setHelp(btn, open);
    store("prefs-help-" + btn.getAttribute("data-help"), open ? "1" : "0");
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("button[data-help]").forEach(function (btn) {
      if (remembered("prefs-help-" + btn.getAttribute("data-help")) === "1") setHelp(btn, true);
    });
  });

  document.addEventListener("input", function (ev) {
    if (ev.target && ev.target.matches && ev.target.matches("input[data-weight]")) updateWeights();
  });
  document.addEventListener("DOMContentLoaded", updateWeights);

  // Live preview: it waits for an idle pause (hx-trigger delay), so say it is pending from the
  // first change; htmx keeps the note up (hx-indicator) while the request runs. A request that
  // finishes after a newer change does not hide it: that change's preview is still to come.
  var changes = 0;
  function pending(on) {
    var note = document.getElementById("preview-pending");
    if (note) note.classList.toggle("is-pending", on);
  }
  function changed() {
    changes += 1;
    pending(true);
  }
  function inForm(ev) {
    return ev.target && ev.target.closest && ev.target.closest("#prefs-form");
  }
  function fromForm(ev) {
    return ev.detail && ev.detail.elt && ev.detail.elt.id === "prefs-form" && ev.detail.xhr;
  }
  document.addEventListener("input", function (ev) { if (inForm(ev)) changed(); });
  document.addEventListener("change", function (ev) { if (inForm(ev)) changed(); });
  document.addEventListener("prefs-changed", changed);
  document.addEventListener("htmx:beforeRequest", function (ev) {
    if (fromForm(ev)) ev.detail.xhr.jhChanges = changes;
  });
  document.addEventListener("htmx:afterRequest", function (ev) {
    if (fromForm(ev) && ev.detail.xhr.jhChanges === changes) pending(false);
  });

  // Per-section Save (b25ec96). Each editable section keeps the values it loaded with; its Save
  // button is enabled while a field differs, and the sticky bar counts the sections that do.
  // Controls inside .section-status (the paid cost choice) are page plumbing, not fields.
  var SKIP = { mtime_ns: 1, "ans.present": 1 };
  var baseline = {};
  var submitting = false;
  var forcedDirty = {};
  var submitted = {};

  function form() { return document.getElementById("prefs-form"); }
  function sectionIds() {
    var ids = [];
    document.querySelectorAll("#prefs-form section.panel[id^='sec-']").forEach(function (sec) {
      ids.push(sec.id.replace(/^sec-/, ""));
    });
    return ids;
  }
  function signature(id) {
    var sec = document.getElementById("sec-" + id);
    var out = [];
    if (!sec) return "";
    sec.querySelectorAll("input, select, textarea").forEach(function (el) {
      if (!el.name || SKIP[el.name] || el.closest(".section-status") || el.form !== form()) return;
      if (el.type === "checkbox" || el.type === "radio") {
        if (el.checked) out.push([el.name, el.value]);
      } else if (el.type !== "button" && el.type !== "submit") {
        out.push([el.name, el.value]);
      }
    });
    return JSON.stringify(out);
  }
  function isDirty(id) {
    if (forcedDirty[id]) return true;
    return baseline[id] !== undefined && signature(id) !== baseline[id];
  }
  function dirtyIds() { return sectionIds().filter(isDirty); }
  function refresh() {
    var dirty = dirtyIds();
    sectionIds().forEach(function (id) {
      var on = dirty.indexOf(id) >= 0;
      var btn = document.querySelector('[data-section-save="' + id + '"]');
      if (btn) btn.disabled = !on;
      var hint = document.querySelector('[data-section-hint="' + id + '"]');
      if (hint) hint.textContent = on ? "Unsaved changes" : "No unsaved changes";
      var sec = document.getElementById("sec-" + id);
      if (sec) sec.classList.toggle("is-dirty", on);
    });
    var bar = document.getElementById("dirty-bar");
    var n = dirty.length;
    if (bar) {
      bar.hidden = n === 0;
      var count = document.getElementById("dirty-count");
      if (count) count.textContent = n + (n === 1 ? " section" : " sections") + " with unsaved changes";
    }
    return n;
  }
  function captureBaselines() {
    var f = form();
    if (!f) return;
    var forced = (f.getAttribute("data-unsaved") || "").split(/\s+/).filter(Boolean);
    sectionIds().forEach(function (id) {
      baseline[id] = signature(id);
      if (forced.indexOf(id) >= 0) forcedDirty[id] = true;
    });
    refresh();
  }
  document.addEventListener("DOMContentLoaded", captureBaselines);
  ["input", "change"].forEach(function (name) {
    document.addEventListener(name, function (ev) { if (inForm(ev)) refresh(); });
  });
  // State tiles swap their hidden inputs through htmx, with no input event.
  document.addEventListener("htmx:afterSwap", function (ev) {
    refresh();
    // A result or the paid cost choice appears under the button: keep it clear of the sticky bar.
    var t = ev.detail && ev.detail.target;
    if (t && t.classList && t.classList.contains("section-status") && t.firstElementChild) {
      t.scrollIntoView({ block: "nearest" });
    }
  });
  document.addEventListener("prefs-changed", refresh);

  // Remember what was submitted: it, not the page a moment later, is what the file now holds.
  document.addEventListener("htmx:beforeRequest", function (ev) {
    var elt = ev.detail && ev.detail.elt;
    var id = elt && elt.getAttribute && (elt.getAttribute("data-section-save") ||
      elt.getAttribute("data-section-confirm"));
    if (id) {
      ev.detail.xhr.jhSection = { id: id };
      submitted[id] = signature(id);
    }
  });
  // A refused section save answers 409/422 with a status block: let htmx swap it.
  document.addEventListener("htmx:beforeSwap", function (ev) {
    var xhr = ev.detail && ev.detail.xhr;
    if (xhr && xhr.jhSection && (xhr.status === 409 || xhr.status === 422)) {
      ev.detail.shouldSwap = true;
      ev.detail.isError = false;
    }
  });
  document.addEventListener("prefs-saved", function (ev) {
    var id = ev.detail && ev.detail.section;
    if (!id) return;
    // The answers section is re-rendered by the server, so the DOM is the truth there.
    baseline[id] = id === "answers" || submitted[id] === undefined ? signature(id) : submitted[id];
    delete forcedDirty[id];
    refresh();
  });
  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("[data-dismiss-status]") : null;
    if (!btn) return;
    var box = document.getElementById("status-" + btn.getAttribute("data-dismiss-status"));
    if (box) box.innerHTML = "";
  });
  document.addEventListener("submit", function (ev) {
    if (ev.target && ev.target.id === "prefs-form") submitting = true;
  });
  window.addEventListener("pageshow", function () { submitting = false; });
  window.addEventListener("beforeunload", function (ev) {
    if (submitting || dirtyIds().length === 0) return;
    ev.preventDefault();
    ev.returnValue = "";
  });
})();
