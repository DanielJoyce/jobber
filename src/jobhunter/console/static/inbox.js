(function () {
  "use strict";

  var undoStack = [];
  var pendingSelect = null;
  var current = null;
  var lastChecked = null; // anchor for shift-click range select
  var BULK_MAX = 500;
  var nextAfterBulk = null;

  function rows() {
    return Array.prototype.filter.call(document.querySelectorAll(".inbox article.row"), function (r) {
      var d = r.closest("details");
      return !r.classList.contains("triaged") && !r.classList.contains("readonly") && (!d || d.open);
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

  function box(row) { return row.querySelector("input.sel"); }

  function checked() {
    return document.querySelectorAll(".inbox input.sel:checked");
  }

  // Refresh the sticky bar: count, tri-state header box, action visibility.
  function updateBar() {
    var vis = rows();
    var visBoxes = vis.map(box).filter(Boolean);
    // checks inside a collapsed section are not "visible"; drop them so they are never submitted
    Array.prototype.forEach.call(document.querySelectorAll(".inbox input.sel:checked"), function (c) {
      if (visBoxes.indexOf(c) === -1) c.checked = false;
    });
    var n = checked().length;
    var all = document.getElementById("select-all");
    var count = document.getElementById("bulk-count");
    var actions = document.getElementById("bulk-actions");
    if (!all) return;
    all.checked = n > 0 && n === visBoxes.length;
    all.indeterminate = n > 0 && n < visBoxes.length;
    count.textContent = n ? n + " selected" : "Select all visible";
    actions.hidden = n === 0;
    var over = n > BULK_MAX;
    Array.prototype.forEach.call(document.querySelectorAll(".bulk-act"), function (b) { b.disabled = over; });
    document.getElementById("bulk-hint").textContent = over
      ? "max " + BULK_MAX + " per action; clear some"
      : "s / x apply to all selected";
  }

  function setAll(on) {
    rows().forEach(function (r) { var b = box(r); if (b) b.checked = on; });
    lastChecked = null;
    updateBar();
  }

  function toggleRow(row) {
    var b = row && box(row);
    if (!b) return;
    b.checked = !b.checked;
    lastChecked = row;
    updateBar();
  }

  function bulkAct(which) {
    var btn = document.querySelector('.bulk-act[hx-vals*="' + which + '"]');
    if (!btn || btn.disabled) return;
    btn.click();
  }

  // Which row should take focus once the selected rows are replaced.
  function rememberNext() {
    var list = rows();
    var from = current ? list.indexOf(current) : 0;
    var keep = list.filter(function (r) { return !box(r).checked; });
    nextAfterBulk = keep.filter(function (r) { return list.indexOf(r) > from; })[0] || keep[keep.length - 1] || null;
  }

  function label(which) {
    if (checked().length) return bulkAct(which);
    if (!current) return;
    var btn = current.querySelector('.act[hx-post$="label=' + which + '"]');
    if (!btn) return;
    var next = nextOf(current);
    undoStack.push(current.dataset.gid);
    btn.click();
    select(next);
  }

  function gidsOf(boxes) {
    return Array.prototype.map.call(boxes, function (b) { return b.closest("article.row").dataset.gid; });
  }

  // Undo one bulk batch from the stack. Each batch keeps its own ids (the toast only ever
  // shows the latest), so a second `u` reaches the batch before it.
  function undoBulk(ids) {
    if (!ids || !ids.length || !window.htmx) return;
    window.htmx.ajax("POST", "/inbox/bulk/undo", { values: { group_id: ids }, swap: "none" });
  }

  function undo() {
    var entry = undoStack.pop();
    if (!entry) return;
    if (typeof entry === "object") return undoBulk(entry.bulk);
    var gid = entry;
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
    if (!sec) {
      // Not on this page (filtered out): follow the chip, which keeps the state filter.
      var chip = document.querySelector('.chips a[data-bucket="' + b + '"]');
      if (chip) window.location.href = chip.getAttribute("href");
      return;
    }
    var d = sec.querySelector("details");
    if (d) d.open = true;
    sec.scrollIntoView({ block: "start" });
    var first = sec.querySelector("article.row:not(.triaged):not(.readonly)");
    if (first) select(first, false);
  }

  document.addEventListener("htmx:afterSwap", function () {
    updateBar();
    if (!pendingSelect) return;
    var el = document.getElementById("row-" + pendingSelect);
    pendingSelect = null;
    if (el) select(el);
  });

  // A bulk request swaps only out-of-band, so htmx:afterSwap is not the hook; wait for the
  // request to finish, then move focus to the row that followed the selection.
  document.addEventListener("htmx:afterRequest", function () {
    updateBar();
    if (!nextAfterBulk) return;
    var n = document.getElementById("row-" + nextAfterBulk.dataset.gid);
    nextAfterBulk = null;
    if (n && !n.classList.contains("triaged")) select(n);
  });

  document.addEventListener("click", function (ev) {
    var row = ev.target.closest && ev.target.closest(".inbox article.row");
    var sel = ev.target.closest && ev.target.closest("input.sel");
    if (sel) {
      // checkbox: range select with shift, never row navigation
      var r0 = sel.closest("article.row");
      if (ev.shiftKey && lastChecked && lastChecked !== r0) {
        var list = rows();
        var a = list.indexOf(lastChecked), b = list.indexOf(r0);
        if (a !== -1 && b !== -1) {
          list.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (r) { box(r).checked = sel.checked; });
        }
      }
      lastChecked = r0;
      updateBar();
      return;
    }
    if (ev.target.closest && ev.target.closest(".bulk-bar")) {
      var bb = ev.target.closest(".bulk-act");
      if (bb) rememberNext();
      if (bb) undoStack.push({ bulk: gidsOf(checked()) });
      // Mouse "undo all" undoes the batch the toast shows; drop it so `u` does not spend a
      // press on it (or undo it twice).
      var bu = ev.target.closest(".bulk-undo");
      if (bu) {
        var ids = Array.prototype.map.call(bu.form.querySelectorAll('input[name="group_id"]'), function (i) { return i.value; }).join();
        for (var k = undoStack.length - 1; k >= 0; k--) {
          if (typeof undoStack[k] === "object" && undoStack[k].bulk.join() === ids) {
            undoStack.splice(k, 1);
            break;
          }
        }
      }
      if (ev.target.closest("#bulk-clear")) setAll(false);
      return;
    }
    if (row && !ev.target.closest("a,button,input,label")) select(row, false);
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
    " ": function () {
      var ae = document.activeElement;
      if (ae && ae.closest && ae.closest("a,button")) { ae.click(); return; }
      toggleRow(current);
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

  var all = document.getElementById("select-all");
  if (all) all.addEventListener("change", function () { setAll(all.checked); });
  document.addEventListener("toggle", updateBar, true);

  window.jh.registerKeys(map);
  var first = rows()[0];
  if (first) select(first, false);
})();
