(function () {
  "use strict";

  var ORDER = ["interested", "preparing", "applied", "acknowledged", "screening", "interview", "offer"];
  var current = null;
  var pendingId = null;

  function cards() {
    return Array.prototype.slice.call(document.querySelectorAll(".pcard")).filter(function (c) {
      var d = c.closest("details");
      return !d || d.open;
    });
  }

  function lane(card) {
    var l = card.closest(".pcol, .closed-lane");
    return l ? l.id : "";
  }

  // Columns in visual order that hold at least one visible card.
  function columns() {
    var out = [];
    var order = ORDER.map(function (s) { return "col-" + s; }).concat(["col-closed"]);
    order.forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      var list = cards().filter(function (c) { return lane(c) === id; });
      if (list.length) out.push(list);
    });
    return out;
  }

  function select(card) {
    if (current) current.classList.remove("focused");
    current = card;
    if (!card) return;
    card.classList.add("focused");
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function vertical(delta) {
    var cols = columns();
    if (!cols.length) return;
    if (!current) return select(cols[0][0]);
    var col = cols.filter(function (c) { return c.indexOf(current) !== -1; })[0];
    if (!col) return select(cols[0][0]);
    var i = Math.max(0, Math.min(col.length - 1, col.indexOf(current) + delta));
    select(col[i]);
  }

  function horizontal(delta) {
    var cols = columns();
    if (!cols.length) return;
    if (!current) return select(cols[0][0]);
    var ci = -1;
    cols.forEach(function (c, n) { if (c.indexOf(current) !== -1) ci = n; });
    var row = ci === -1 ? 0 : cols[ci].indexOf(current);
    var n = Math.max(0, Math.min(cols.length - 1, ci + delta));
    select(cols[n][Math.min(row, cols[n].length - 1)]);
  }

  function shift(delta) {
    if (!current) return;
    var i = ORDER.indexOf(current.dataset.status);
    var to = ORDER[i + delta];
    if (i === -1 || !to) return;
    pendingId = current.id;
    window.htmx.ajax("POST", "/pipeline/" + current.dataset.app + "/move?to=" + to, { swap: "none" });
  }

  function openDrawer() {
    if (!current) return;
    window.htmx.ajax("GET", "/pipeline/" + current.dataset.app, { target: "#drawer", swap: "innerHTML" });
  }

  function closeDrawer() {
    var d = document.getElementById("drawer");
    if (d) d.innerHTML = "";
    if (current) current.focus({ preventScroll: true });
  }

  document.addEventListener("htmx:afterSettle", function () {
    if (!pendingId) return;
    var el = document.getElementById(pendingId);
    pendingId = null;
    if (el) select(el);
  });

  document.addEventListener("click", function (ev) {
    var card = ev.target.closest && ev.target.closest(".pcard");
    if (card) select(card);
    if (ev.target.closest && ev.target.closest("#drawer-close")) closeDrawer();
  });

  window.jh.registerKeys({
    j: function () { vertical(1); },
    ArrowDown: function () { vertical(1); },
    k: function () { vertical(-1); },
    ArrowUp: function () { vertical(-1); },
    h: function () { horizontal(-1); },
    ArrowLeft: function () { horizontal(-1); },
    l: function () { horizontal(1); },
    ArrowRight: function () { horizontal(1); },
    "[": function () { shift(-1); },
    "]": function () { shift(1); },
    Enter: openDrawer,
    Escape: closeDrawer
  });

  var first = cards()[0];
  if (first) select(first);
})();
