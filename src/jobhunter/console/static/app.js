(function () {
  "use strict";

  var keymap = {};

  function typing(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (typing(ev.target)) return;
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
