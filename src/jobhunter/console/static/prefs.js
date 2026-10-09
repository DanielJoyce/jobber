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
})();
