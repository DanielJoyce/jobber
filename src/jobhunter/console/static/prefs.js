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
})();
