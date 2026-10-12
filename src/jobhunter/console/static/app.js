(function () {
  "use strict";

  var keymap = {};

  // Input types that take no typed text. A checkbox you just clicked keeps focus, and the
  // keys must still work then (mouse-select, then press x is the main bulk workflow).
  var NON_TEXT = ["checkbox", "radio", "button", "submit", "reset", "image"];

  function typing(el) {
    if (!el) return false;
    var tag = el.tagName;
    if (tag === "INPUT") return NON_TEXT.indexOf((el.type || "").toLowerCase()) === -1;
    return tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  function isCheckbox(el) {
    return !!el && el.tagName === "INPUT" && (el.type || "").toLowerCase() === "checkbox";
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (typing(ev.target)) return;
    // Space on a focused checkbox is the browser's own toggle; do not also toggle the row.
    if (ev.key === " " && isCheckbox(ev.target)) return;
    var fn = keymap[ev.key];
    if (fn) {
      ev.preventDefault();
      fn(ev);
    }
  });

  window.jh = window.jh || {};
  // Bucket letter -> name, injected once by base.html from core/bucketnames.py.
  var bucketNames = {};
  try {
    bucketNames = JSON.parse(document.getElementById("bucket-names").textContent) || {};
  } catch (e) {}
  window.jh.bucketNames = bucketNames;
  window.jh.bucketName = function (letter) { return bucketNames[letter] || letter; };
  // Replace the page's key bindings: registerKeys({ j: fn, k: fn, "?": fn }).
  window.jh.registerKeys = function (map) {
    keymap = Object.assign({}, map);
  };

  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("jh-theme", t); } catch (e) {}
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      if (!cur) {
        cur = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      applyTheme(cur === "dark" ? "light" : "dark");
    });
  });

  // Activity indicator (/activity): remember a dismissed done/failed line for this tab, and
  // tell the server which one so a later page does not show it again.
  document.addEventListener("htmx:configRequest", function (e) {
    if (e.detail.path !== "/activity") return;
    try { e.detail.parameters.dismissed = sessionStorage.getItem("jh-activity-dismissed") || ""; } catch (x) {}
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".activity-dismiss");
    if (!b) return;
    try { sessionStorage.setItem("jh-activity-dismissed", b.getAttribute("data-dismiss")); } catch (x) {}
    var box = document.getElementById("activity");
    if (box) box.replaceChildren();
  });

  // Achievement notice (specs/021). Its x (.ach-dismiss) is an htmx POST of the ids it shows
  // to /achievements/seen; seen is server-side, so other tabs drop it on their next poll.
  // Here we only hide it at once, and keep it hidden if a poll that raced the POST renders
  // the same ids again. It is never removed from the DOM: htmx fires the response's
  // HX-Trigger (jh-activity, which refreshes the band) on the button, so it must stay attached.
  var dismissedIds = {};
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".ach-dismiss");
    if (!b) return;
    var n = b.closest(".ach-notice");
    if (!n) return;
    (n.getAttribute("data-ids") || "").split(",").forEach(function (id) {
      if (id) dismissedIds[id] = true;
    });
    n.hidden = true;
  });

  // Announce a new unlock once, through #ach-announce (outside the band): the notice itself is
  // aria-live="off" so the band's live region does not re-read it on every 3 s or 60 s swap.
  var lastAnnounced = null;
  function lastUnlock() {
    try { return sessionStorage.getItem("jh-ach-announced"); } catch (x) { return lastAnnounced; }
  }
  function onBand() {
    var n = document.querySelector("#activity .ach-notice");
    if (!n) return;
    var ids = (n.getAttribute("data-ids") || "").split(",").filter(Boolean);
    if (ids.length && ids.every(function (id) { return dismissedIds[id]; })) {
      n.hidden = true;
      return;
    }
    var id = n.getAttribute("data-unlock");
    if (!id || id === lastUnlock()) return;
    lastAnnounced = id;
    try { sessionStorage.setItem("jh-ach-announced", id); } catch (x) {}
    var region = document.getElementById("ach-announce");
    var text = n.querySelector(".ach-text");
    if (region && text) region.textContent = text.textContent;
  }
  document.addEventListener("htmx:afterSettle", onBand);
})();
