// Today dashboard map (specs/013 "The map"). No build step: d3 + topojson-client are vendored.
(function () {
  "use strict";

  var SMALL = ["VT", "NH", "MA", "RI", "CT", "NJ", "DE", "MD", "DC"];
  var TERRITORIES = ["PR", "GU", "MP", "VI", "AS"];
  var REMOTE = "REMOTE";
  // Sequential ramp steps for the five quantile classes; zero gets --seq-100, no data --axis.
  var STEPS = ["--seq-200", "--seq-350", "--seq-500", "--seq-600", "--seq-700"];
  var COVERAGE = {
    critical: ["⚠", "source broken"],
    serious: ["⚠", "source suspect"],
    email_stale: ["✉?", "email, no alerts in 3 days"],
    email: ["✉", "email alerts only"],
    direct: ["●", "collected directly"],
    none: ["○", "not covered"]
  };
  var SVGNS = "http://www.w3.org/2000/svg";

  var ui = {};
  var atlas = null;
  var data = null;
  var reqSeq = 0;

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) {}
    return null;
  }

  function readJSON(id) {
    var el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; }
  }

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // ─── values and classes ────────────────────────────────────────────────────

  function fmt(v, kind) {
    if (v === null || v === undefined) return "no data";
    if (kind === "rate") return Math.round(v * 100) + "%";
    if (kind === "money") return "$" + Math.round(v / 1000) + "k";
    return String(v);
  }

  function classOf(v, breaks) {
    if (v === null || v === undefined) return -1;
    if (v <= 0 || !breaks.length) return 0;
    for (var i = 0; i < breaks.length; i++) if (v <= breaks[i]) return i + 1;
    return breaks.length;
  }

  function stepFor(cls, n) {
    if (n <= 1) return STEPS[2];
    return STEPS[Math.round(((cls - 1) * (STEPS.length - 1)) / (n - 1))];
  }

  function fillFor(row) {
    var cls = classOf(row ? row.value : null, data.breaks);
    if (cls < 0) return token("--axis");
    if (cls === 0) return token("--seq-100");
    return token(stepFor(cls, data.breaks.length));
  }

  function inkOn(fill) {
    var c = d3.hsl(fill);
    return c && c.l > 0.55 ? "#0b0b0b" : "#ffffff";
  }

  // ─── geometry ──────────────────────────────────────────────────────────────

  function shapeCells() {
    var path = d3.geoPath(null);
    var feats = topojson.feature(atlas, atlas.objects.states).features;
    var cells = [];
    var centroid = {};
    feats.forEach(function (f) {
      var usps = ui.fips[f.id];
      if (!usps) return;
      var c = path.centroid(f);
      centroid[usps] = c;
      var small = SMALL.indexOf(usps) >= 0;
      cells.push({ usps: usps, d: path(f), lx: c[0], ly: c[1], focus: !small, label: !small });
    });
    var bx = 992, bw = 100, bh = 34, gap = 7, y0 = 40;
    SMALL.forEach(function (usps, i) {
      var y = y0 + i * (bh + gap);
      var c = centroid[usps] || [bx, y];
      cells.push({
        usps: usps, x: bx, y: y, w: bw, h: bh, lx: bx + 26, ly: y + bh / 2, focus: true,
        label: true, callout: true, leader: [c[0], c[1], bx, y + bh / 2]
      });
    });
    var ry = y0 + SMALL.length * (bh + gap) + 14;
    cells.push({ usps: REMOTE, x: bx, y: ry, w: bw, h: 92, lx: bx + bw / 2, ly: ry + 30,
      focus: true, label: true, remote: true });
    TERRITORIES.forEach(function (usps, i) {
      var x = 10 + i * 74;
      cells.push({ usps: usps, x: x, y: 628, w: 66, h: 44, lx: x + 33, ly: 628 + 20,
        focus: true, label: true });
    });
    // Aleutians extend to x = -58 in the projected atlas.
    return { cells: cells, x0: -60, w: 1160, h: 680 };
  }

  function gridCells() {
    var size = 64, step = 68, cells = [];
    Object.keys(ui.grid).forEach(function (usps) {
      var rc = ui.grid[usps];
      var x = rc[1] * step, y = rc[0] * step;
      cells.push({ usps: usps, x: x, y: y, w: size, h: size, lx: x + size / 2, ly: y + 24,
        focus: true, label: true });
    });
    var gw = 12 * step;
    cells.push({ usps: REMOTE, x: gw + 16, y: 0, w: 120, h: 92, lx: gw + 76, ly: 30,
      focus: true, label: true, remote: true });
    TERRITORIES.forEach(function (usps, i) {
      var x = i * step, y = 8 * step + 16;
      cells.push({ usps: usps, x: x, y: y, w: size, h: size, lx: x + size / 2, ly: y + 24,
        focus: true, label: true });
    });
    return { cells: cells, x0: 0, w: gw + 140, h: 9 * step + 20 };
  }

  // ─── rendering ─────────────────────────────────────────────────────────────

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVGNS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }

  function text(parent, x, y, str, cls, fill) {
    var t = el("text", { x: x, y: y, "class": cls, "text-anchor": "middle" }, parent);
    if (fill) t.setAttribute("fill", fill);
    t.textContent = str;
    return t;
  }

  function rowFor(usps) {
    return (data && data.byState[usps]) || null;
  }

  function ariaLabel(row) {
    if (!row) return "";
    var cov = COVERAGE[row.coverage] || COVERAGE.none;
    return row.name + ": " + data.label + " " + fmt(row.value, data.kind) + ", " + cov[1] +
      (row.applications ? ", " + row.applications + " applications" : "");
  }

  function render() {
    if (!data || (ui.view === "shape" && !atlas)) return;
    var layout = ui.view === "grid" ? gridCells() : shapeCells();
    ui.map.textContent = "";
    var svg = el("svg", {
      viewBox: layout.x0 + " 0 " + layout.w + " " + layout.h,
      "class": "map-svg view-" + ui.view + (ui.status.checked ? "" : " no-status"),
      role: "group",
      "aria-label": "Map of " + data.label + " by state"
    }, ui.map);
    var gLeaders = el("g", { "class": "leaders", "aria-hidden": "true" }, svg);
    var gTargets = el("g", { "class": "targets" }, svg);
    var gLabels = el("g", { "class": "labels", "aria-hidden": "true" }, svg);
    var showBadge = data.metric !== "applied";

    layout.cells.forEach(function (c) {
      var row = rowFor(c.usps);
      var fill = fillFor(row);
      var node;
      if (c.d) {
        node = el("path", { d: c.d }, gTargets);
      } else {
        node = el("rect", { x: c.x, y: c.y, width: c.w, height: c.h, rx: 4 }, gTargets);
      }
      node.setAttribute("class", "cell" + (c.callout ? " callout" : "") + (c.remote ? " remote" : ""));
      node.setAttribute("fill", fill);
      node.dataset.state = c.usps;
      if (c.focus) {
        node.setAttribute("tabindex", "0");
        node.setAttribute("role", "link");
        node.setAttribute("aria-label", ariaLabel(row));
      }
      if (c.leader) {
        var l = c.leader;
        el("line", { x1: l[0], y1: l[1], x2: l[2], y2: l[3], "class": "leader" }, gLeaders);
        el("circle", { cx: l[0], cy: l[1], r: 1.5, "class": "leader-dot" }, gLeaders);
      }
      if (!c.label || !row) return;
      var ink = inkOn(fill);
      var cov = COVERAGE[row.coverage] || COVERAGE.none;
      if (c.remote) {
        text(gLabels, c.lx, c.ly - 6, "Remote (US)", "lbl", ink);
        text(gLabels, c.lx, c.ly + 18, fmt(row.value, data.kind), "lbl-value", ink);
        text(gLabels, c.lx, c.ly + 40, cov[0], "status cov-" + row.coverage);
      } else if (c.callout) {
        text(gLabels, c.lx, c.ly + 5, c.usps, "lbl", ink);
        text(gLabels, c.x + 58, c.ly + 5, cov[0], "status cov-" + row.coverage);
      } else {
        text(gLabels, c.lx, c.ly, c.usps, "lbl", ink);
        text(gLabels, c.lx, c.ly + 15, cov[0], "status cov-" + row.coverage);
      }
      if (showBadge && row.applications > 0) {
        var bx = c.callout ? c.x + c.w - 14 : c.remote ? c.x + c.w - 14 : c.lx + 15;
        var by = c.callout ? c.ly : c.remote ? c.y + 14 : c.ly - 12;
        el("circle", { cx: bx, cy: by, r: 8, "class": "badge" }, gLabels);
        text(gLabels, bx, by + 3.5, String(row.applications), "badge-text");
      }
    });

    orderFocus();
    renderLegend();
  }

  // Focus order follows the state table's current sort.
  function orderFocus() {
    var g = ui.map.querySelector("g.targets");
    if (!g) return;
    var rows = document.querySelectorAll("#state-table tbody tr[data-state]");
    rows.forEach(function (tr) {
      g.querySelectorAll('[tabindex][data-state="' + tr.dataset.state + '"]').forEach(function (n) {
        g.appendChild(n);
      });
    });
  }

  function swatch(color, label) {
    var item = document.createElement("span");
    item.className = "legend-item";
    var sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = color;
    item.appendChild(sw);
    item.appendChild(document.createTextNode(label));
    return item;
  }

  function renderLegend() {
    var box = ui.legend;
    box.textContent = "";
    var scale = document.createElement("div");
    scale.className = "legend-scale";
    var title = document.createElement("span");
    title.className = "legend-title";
    title.textContent = data.label + ":";
    scale.appendChild(title);
    scale.appendChild(swatch(token("--seq-100"), "0"));
    var prev = 0;
    data.breaks.forEach(function (b, i) {
      var color = token(stepFor(i + 1, data.breaks.length));
      var label;
      if (data.kind === "count") {
        var lo = Math.floor(prev) + 1;
        label = lo >= b ? fmt(b, "count") : lo + "–" + b;
      } else {
        label = "≤ " + fmt(b, data.kind);
      }
      scale.appendChild(swatch(color, label));
      prev = b;
    });
    scale.appendChild(swatch(token("--axis"),
      data.metric === "response_rate" ? "no data / n < 5" : "no data"));
    box.appendChild(scale);

    var status = document.createElement("div");
    status.className = "legend-status" + (ui.status.checked ? "" : " hidden");
    ["direct", "email", "email_stale", "serious", "critical", "none"].forEach(function (k) {
      var item = document.createElement("span");
      item.className = "legend-item";
      var icon = document.createElement("span");
      icon.className = "cov cov-" + k;
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = COVERAGE[k][0];
      item.appendChild(icon);
      item.appendChild(document.createTextNode(" " + COVERAGE[k][1]));
      status.appendChild(item);
    });
    if (data.metric !== "applied") {
      var badge = document.createElement("span");
      badge.className = "legend-item";
      badge.innerHTML = '<span class="badge-html">3</span> applications sent';
      status.appendChild(badge);
    }
    box.appendChild(status);
  }

  // ─── tooltip and interaction ───────────────────────────────────────────────

  function tooltipLines(row) {
    var rr = row.response_rate === null
      ? (row.applied ? "n = " + row.applied + ", too few to rate" : "no applications")
      : fmt(row.response_rate, "rate") + " (" + row.responses + "/" + row.applied + ")";
    var cov = COVERAGE[row.coverage] || COVERAGE.none;
    return [
      [data.label, fmt(row.value, data.kind)],
      ["New A+B", row.new_ab],
      ["Scored", row.scored],
      ["Shortlisted", row.shortlisted],
      ["Applied", row.applied],
      ["Responses", row.responses],
      ["Response rate", rr],
      ["Median salary", fmt(row.median_salary, "money")],
      ["COL-adjusted", fmt(row.col_adjusted, "money")],
      ["Coverage", cov[0] + " " + cov[1]],
      ["Sources", row.sources.length ? row.sources.join(", ") : "none"],
      ["Last collected", row.last_ok_at ? row.last_ok_at.slice(0, 10) : "never"],
      ["Applications", row.applications]
    ];
  }

  function showTip(usps, x, y) {
    var row = rowFor(usps);
    if (!row) return;
    var tip = ui.tip;
    tip.textContent = "";
    var h = document.createElement("strong");
    h.textContent = row.name;
    tip.appendChild(h);
    var dl = document.createElement("dl");
    tooltipLines(row).forEach(function (pair) {
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = String(pair[1]);
      dl.appendChild(dt);
      dl.appendChild(dd);
    });
    tip.appendChild(dl);
    tip.hidden = false;
    var box = ui.map.parentNode.getBoundingClientRect();
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var left = Math.min(x - box.left + 14, box.width - tw - 4);
    var top = y - box.top + 14;
    if (top + th > box.height) top = Math.max(0, y - box.top - th - 10);
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = top + "px";
  }

  function hideTip() { ui.tip.hidden = true; }

  function target(ev) {
    var n = ev.target;
    return n && n.classList && n.classList.contains("cell") ? n : null;
  }

  function go(usps) {
    window.location.href = "/inbox?state=" + encodeURIComponent(usps);
  }

  function bindMap() {
    ui.map.addEventListener("mousemove", function (ev) {
      var n = target(ev);
      if (n) showTip(n.dataset.state, ev.clientX, ev.clientY); else hideTip();
    });
    ui.map.addEventListener("mouseleave", hideTip);
    ui.map.addEventListener("click", function (ev) {
      var n = target(ev);
      if (n) go(n.dataset.state);
    });
    ui.map.addEventListener("focusin", function (ev) {
      var n = target(ev);
      if (!n) return;
      var r = n.getBoundingClientRect();
      showTip(n.dataset.state, r.left + r.width / 2, r.top + r.height / 2);
    });
    ui.map.addEventListener("focusout", hideTip);
    ui.map.addEventListener("keydown", function (ev) {
      var n = target(ev);
      if (n && ev.key === "Enter") { ev.preventDefault(); go(n.dataset.state); }
      if (ev.key === "Escape") hideTip();
    });
  }

  // ─── data flow ─────────────────────────────────────────────────────────────

  function params() {
    var form = document.getElementById("dash-controls");
    var fd = new FormData(form);
    return { range: fd.get("range") || "7", metric: ui.metric.value };
  }

  function loadMap() {
    var p = params();
    var seq = ++reqSeq;
    return fetch(ui.map.dataset.api + "?metric=" + encodeURIComponent(p.metric) +
      "&range=" + encodeURIComponent(p.range))
      .then(function (r) { return r.json(); })
      .then(function (json) {
        if (seq !== reqSeq) return;
        json.byState = {};
        json.states.forEach(function (s) { json.byState[s.state] = s; });
        data = json;
        render();
      })
      .catch(function () { ui.map.textContent = "Map data failed to load."; });
  }

  function refresh() {
    var p = params();
    try {
      var url = new URL(window.location.href);
      url.searchParams.set("range", p.range);
      url.searchParams.set("metric", p.metric);
      history.replaceState(null, "", url);
    } catch (e) {}
    document.body.dispatchEvent(new CustomEvent("dash-refresh"));
    loadMap();
  }

  function init() {
    ui.map = document.getElementById("map");
    if (!ui.map) return;
    ui.legend = document.getElementById("map-legend");
    ui.tip = document.getElementById("map-tooltip");
    ui.metric = document.getElementById("map-metric");
    ui.status = document.getElementById("map-status");
    ui.grid = readJSON("tile-grid");
    ui.fips = readJSON("fips-usps");

    var view = store("jh-dash-view");
    ui.view = view === "grid" ? "grid" : "shape";
    document.querySelectorAll('input[name="map-view"]').forEach(function (r) {
      r.checked = r.value === ui.view;
      r.addEventListener("change", function () {
        if (!r.checked) return;
        ui.view = r.value;
        store("jh-dash-view", ui.view);
        render();
      });
    });
    if (store("jh-dash-status") === "off") ui.status.checked = false;
    ui.status.addEventListener("change", function () {
      store("jh-dash-status", ui.status.checked ? "on" : "off");
      render();
    });

    var urlMetric = new URLSearchParams(window.location.search).get("metric");
    var saved = store("jh-dash-metric");
    var metricChanged = false;
    if (!urlMetric && saved && saved !== ui.metric.value &&
        ui.metric.querySelector('option[value="' + saved + '"]')) {
      ui.metric.value = saved;
      metricChanged = true;
    }

    document.getElementById("dash-controls").addEventListener("change", function () {
      store("jh-dash-metric", ui.metric.value);
      refresh();
    });
    ui.metric.addEventListener("change", function () {
      store("jh-dash-metric", ui.metric.value);
      refresh();
    });

    document.body.addEventListener("htmx:afterSwap", function (ev) {
      if (!ev.detail || !ev.detail.target || ev.detail.target.id !== "state-table") return;
      var table = ev.detail.target.querySelector("table");
      var form = document.getElementById("dash-controls");
      if (table && form) {
        form.elements.sort.value = table.dataset.sort;
        form.elements.dir.value = table.dataset.dir;
      }
      orderFocus();
    });

    bindMap();

    var mo = new MutationObserver(function () { render(); });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    try {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
    } catch (e) {}

    fetch(ui.map.dataset.atlas)
      .then(function (r) { return r.json(); })
      .then(function (json) { atlas = json; render(); })
      .catch(function () { ui.view = "grid"; render(); });
    if (metricChanged) refresh(); else loadMap();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
