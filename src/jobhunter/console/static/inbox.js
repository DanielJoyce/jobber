(function () {
  "use strict";

  var undoStack = [];
  var pendingSelect = null;
  var current = null;

  function rows() {
    return Array.prototype.filter.call(document.querySelectorAll(".inbox article.row"), function (r) {
      var d = r.closest("details");
      return !r.classList.contains("triaged") && (!d || d.open);
    });
  }

  function select(row, scroll) {
    if (current) current.classList.remove("selected");
    current = row;
    if (!row) return;
    row.classList.add("selected");
    if (scroll !== false) row.scrollIntoView({ block: "nearest" });
  }

  function move(delta) {
    var list = rows();
    if (!list.length) return;
    var i = current ? list.indexOf(current) : -1;
    if (i === -1 && current) {
      // current row was replaced; fall back to the first row after its position
      i = delta > 0 ? -1 : list.length;
    }
    var n = Math.max(0, Math.min(list.length - 1, i + delta));
    select(list[n]);
  }

  function nextOf(row) {
    var list = rows();
    var i = list.indexOf(row);
    return list[i + 1] || list[i - 1] || null;
  }

  function label(which) {
    if (!current) return;
    var btn = current.querySelector('.act[hx-post$="label=' + which + '"]');
    if (!btn) return;
    var next = nextOf(current);
    undoStack.push(current.dataset.gid);
    btn.click();
    select(next);
  }

  function undo() {
    var gid = undoStack.pop();
    if (!gid) return;
    var el = document.getElementById("row-" + gid);
    var btn = el && el.querySelector(".undo");
    if (!btn) return;
    pendingSelect = gid;
    btn.click();
  }

  function go(suffix) {
    if (current) window.location.href = suffix.replace("{id}", current.dataset.gid);
  }

  function jump(b) {
    var sec = document.getElementById("bucket-" + b);
    if (!sec) return;
    var d = sec.querySelector("details");
    if (d) d.open = true;
    sec.scrollIntoView({ block: "start" });
    var first = sec.querySelector("article.row:not(.triaged)");
    if (first) select(first, false);
  }

  document.addEventListener("htmx:afterSwap", function () {
    if (!pendingSelect) return;
    var el = document.getElementById("row-" + pendingSelect);
    pendingSelect = null;
    if (el) select(el);
  });

  document.addEventListener("click", function (ev) {
    var row = ev.target.closest && ev.target.closest(".inbox article.row");
    if (row && !ev.target.closest("a,button")) select(row, false);
    var act = ev.target.closest && ev.target.closest(".act[hx-post]");
    if (act && !act.classList.contains("undo")) {
      // mouse-initiated label: remember for undo (keyboard path pushes itself)
      var r = act.closest("article.row");
      if (r && undoStack[undoStack.length - 1] !== r.dataset.gid) undoStack.push(r.dataset.gid);
    }
  });

  var map = {
    j: function () { move(1); },
    k: function () { move(-1); },
    Enter: function () {
      if (!current) return;
      var d = current.querySelector(".row-detail");
      if (d) d.hidden = !d.hidden;
    },
    s: function () { label("interesting"); },
    x: function () { label("not_interesting"); },
    d: function () { go("/job/{id}?deep=1"); },
    o: function () {
      if (current && current.dataset.url) window.open(current.dataset.url, "_blank", "noopener");
    },
    a: function () { go("/job/{id}#apply"); },
    A: function () { go("/apply/{id}"); },
    u: undo,
    f: function () {
      var d = document.getElementById("stale-details");
      if (d) d.open = !d.open;
    }
  };
  "ABCDEFG".split("").forEach(function (b, i) {
    map[String(i + 1)] = function () { jump(b); };
  });

  window.jh.registerKeys(map);
  var first = rows()[0];
  if (first) select(first, false);
})();
