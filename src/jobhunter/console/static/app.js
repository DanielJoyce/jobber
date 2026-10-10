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
})();
