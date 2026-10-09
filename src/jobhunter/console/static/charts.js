// Today dashboard charts (specs/013 "Charts"): line, funnel, stacked columns, status bars.
// d3 alone (vendored); colors come from CSS tokens via style="fill:var(--...)", so a theme
// change recolors the marks without a redraw.
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var W = 460;
  var ui = {};
  var seq = 0;
  var last = null;

  var STATUS_LABEL = {
    interested: "Interested", preparing: "Preparing", applied: "Applied",
    acknowledged: "Acknowledged", screening: "Screening", interview: "Interview",
    offer: "Offer", rejected: "Rejected", withdrawn: "Withdrawn",
    no_response: "No response", closed: "Closed"
  };

  function svgIn(body, h) {
    return d3.select(body).append("svg")
      .attr("viewBox", "0 0 " + W + " " + h).attr("role", "img");
  }

  function pct(v) { return v === null || v === undefined ? "n/a" : Math.round(v * 100) + "%"; }

  function shortDay(iso) {
    var d = new Date(iso + "T00:00:00Z");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  }

  function niceInt(max) {
    var m = Math.max(1, max);
    return d3.scaleLinear().domain([0, m]).nice(4);
  }

  function intTicks(scale) {
    return scale.ticks(4).filter(function (t) { return Number.isInteger(t); });
  }

  function empty(body, svgLabel) {
    var p = document.createElement("p");
    p.className = "empty";
    p.textContent = "not enough data yet";
    p.setAttribute("aria-label", svgLabel + ": not enough data yet");
    body.appendChild(p);
  }

  // ─── tooltip ──────────────────────────────────────────────────────────────

  function tip(ev, title, rows) {
    var t = ui.tip;
    t.textContent = "";
    var h = document.createElement("strong");
    h.textContent = title;
    t.appendChild(h);
    rows.forEach(function (r) {
      var row = document.createElement("div");
      row.className = "tip-row";
      var a = document.createElement("span");
      if (r.color) {
        var sw = document.createElement("i");
        sw.className = "sw";
        sw.style.background = r.color;
        a.appendChild(sw);
      }
      a.appendChild(document.createTextNode(r.label));
      var b = document.createElement("b");
      b.textContent = r.value;
      row.appendChild(a);
      row.appendChild(b);
      t.appendChild(row);
    });
    t.hidden = false;
    var box = ui.section.getBoundingClientRect();
    var left = ev.clientX - box.left + 14;
    if (left + t.offsetWidth > box.width - 4) left = ev.clientX - box.left - t.offsetWidth - 14;
    var top = ev.clientY - box.top + 14;
    t.style.left = Math.max(4, left) + "px";
    t.style.top = top + "px";
  }

  function untip() { ui.tip.hidden = true; }

  function hover(sel, fn) {
    sel.on("mousemove", function (ev, d) { fn(ev, d); }).on("mouseleave", untip);
  }

  // ─── line: New A+B per day ────────────────────────────────────────────────

  function lineChart(body, block) {
    var pts = block.points;
    var H = 220, m = { l: 30, r: 62, t: 12, b: 26 };
    var svg = svgIn(body, H);
    svg.attr("aria-label", "New A+B per day, last " + pts.length + " days, mean of the last 7 days " +
      block.mean7.toFixed(1));
    var x = d3.scalePoint().domain(pts.map(function (p) { return p.day; })).range([m.l, W - m.r]);
    var y = niceInt(d3.max(pts, function (p) { return p.count; })).range([H - m.b, m.t]);

    var grid = svg.append("g").attr("class", "grid");
    intTicks(y).forEach(function (t) {
      grid.append("line").attr("x1", m.l).attr("x2", W - m.r).attr("y1", y(t)).attr("y2", y(t));
      svg.append("text").attr("class", "tick").attr("x", m.l - 6).attr("y", y(t) + 4)
        .attr("text-anchor", "end").text(t);
    });
    var every = Math.ceil(pts.length / 6);
    pts.forEach(function (p, i) {
      if (i % every !== 0 && i !== pts.length - 1) return;
      if (i !== pts.length - 1 && pts.length - 1 - i < every / 2) return;
      svg.append("text").attr("class", "tick").attr("x", x(p.day)).attr("y", H - 8)
        .attr("text-anchor", "middle").text(shortDay(p.day));
    });

    svg.append("line").attr("class", "mean").attr("x1", m.l).attr("x2", W - m.r)
      .attr("y1", y(block.mean7)).attr("y2", y(block.mean7));
    svg.append("text").attr("class", "mean-label").attr("x", W - m.r + 6).attr("y", y(block.mean7) + 4)
      .text("7d mean " + block.mean7.toFixed(1));

    svg.append("path").attr("class", "line")
      .attr("d", d3.line().x(function (p) { return x(p.day); }).y(function (p) { return y(p.count); })(pts));

    var cross = svg.append("line").attr("class", "cross").attr("y1", m.t).attr("y2", H - m.b).attr("display", "none");
    var dot = svg.append("circle").attr("class", "dot").attr("r", 4).attr("display", "none");
    svg.append("rect").attr("class", "hit").attr("x", m.l).attr("y", m.t)
      .attr("width", W - m.l - m.r).attr("height", H - m.t - m.b)
      .on("mousemove", function (ev) {
        var box = this.ownerSVGElement.getBoundingClientRect();
        var px = (ev.clientX - box.left) * (W / box.width);
        var best = pts[0];
        pts.forEach(function (p) { if (Math.abs(x(p.day) - px) < Math.abs(x(best.day) - px)) best = p; });
        cross.attr("display", null).attr("x1", x(best.day)).attr("x2", x(best.day));
        dot.attr("display", null).attr("cx", x(best.day)).attr("cy", y(best.count));
        tip(ev, shortDay(best.day), [
          { label: "New A+B", value: best.count, color: "var(--series-1)" },
          { label: "7-day mean", value: block.mean7.toFixed(1) }
        ]);
      })
      .on("mouseleave", function () { cross.attr("display", "none"); dot.attr("display", "none"); untip(); });
  }

  // ─── horizontal bars (funnel, zoom, status) ───────────────────────────────

  // rows: [{label, count, color, note, tipRows}]; one y-band per row, labels in text ink.
  function hbars(body, rows, opts) {
    var band = opts.band || 26, thick = 14;
    var m = { l: opts.left || 110, r: opts.right || 120, t: 4, b: 4 };
    var H = m.t + m.b + band * rows.length;
    var svg = svgIn(body, H);
    svg.attr("aria-label", opts.label);
    var max = d3.max(rows, function (r) { return r.count; }) || 1;
    var x = d3.scaleLinear().domain([0, max]).range([0, W - m.l - m.r]);
    var g = svg.append("g");
    svg.append("line").attr("class", "cross").attr("x1", m.l).attr("x2", m.l).attr("y1", m.t).attr("y2", H - m.b)
      .style("stroke", "var(--axis)");
    var row = g.selectAll("g.r").data(rows).enter().append("g").attr("class", "r")
      .attr("transform", function (d, i) { return "translate(0," + (m.t + i * band) + ")"; });
    row.append("text").attr("class", "cat").attr("x", m.l - 8).attr("y", band / 2 + 4)
      .attr("text-anchor", "end").text(function (d) { return d.label; });
    row.append("rect").attr("class", "bar").attr("x", m.l).attr("y", (band - thick) / 2)
      .attr("height", thick).attr("rx", 2)
      .attr("width", function (d) { return d.count > 0 ? Math.max(2, x(d.count)) : 0; })
      .style("fill", function (d) { return d.color; });
    row.append("text").attr("class", "val")
      .attr("x", function (d) { return m.l + (d.count > 0 ? Math.max(2, x(d.count)) : 0) + 6; })
      .attr("y", band / 2 + 4)
      .each(function (d) {
        var t = d3.select(this);
        t.append("tspan").text(d.count.toLocaleString());
        if (d.note) t.append("tspan").attr("class", "pct").attr("dx", 6).text(d.note);
      });
    // Wide transparent hit area so thin bars and zero rows are hoverable.
    row.append("rect").attr("class", "hit").attr("x", 0).attr("y", 0).attr("width", W).attr("height", band);
    hover(row, function (ev, d) { tip(ev, d.label, d.tipRows); });
  }

  function funnelChart(body, block) {
    var stages = block.stages;
    var rows = stages.map(function (s, i) {
      return {
        label: s.label, count: s.count,
        color: "var(--ord-" + (i + 1) + ")",
        note: i === 0 ? "" : pct(s.pct_prev) + " of prev",
        tipRows: [
          { label: "Count", value: s.count.toLocaleString(), color: "var(--ord-" + (i + 1) + ")" },
          { label: "% of previous stage", value: i === 0 ? "n/a" : pct(s.pct_prev) },
          { label: "% of found", value: stages[0].count ? pct(s.count / stages[0].count) : "n/a" }
        ]
      };
    });
    hbars(body, rows, { label: "Funnel, last " + block.days + " days", left: 112, right: 128 });

    var from = block.zoom_from;
    var title = document.createElement("h3");
    title.className = "zoom-title";
    title.textContent = "Zoomed: " + stages[from].label + " to " + stages[stages.length - 1].label;
    body.appendChild(title);
    var sub = document.createElement("p");
    sub.className = "sub";
    sub.textContent = "Own linear scale; the full funnel's axis would flatten these.";
    body.appendChild(sub);
    hbars(body, rows.slice(from), { label: "Funnel, later stages zoomed", left: 112, right: 128 });
  }

  function statusChart(body, block) {
    var rows = block.items.map(function (s) {
      var label = STATUS_LABEL[s.status] || s.status;
      return {
        label: label, count: s.count, color: "var(--series-1)", note: "",
        tipRows: [{ label: "Applications", value: s.count, color: "var(--series-1)" }]
      };
    });
    hbars(body, rows, { label: "Applications by status", band: 22, left: 112, right: 50 });
  }

  // ─── stacked columns: bucket mix per week ─────────────────────────────────

  function mixChart(body, block) {
    var weeks = block.weeks, slots = block.slots;
    var H = 250, m = { l: 30, r: 74, t: 18, b: 26 };
    var svg = svgIn(body, H);
    svg.attr("aria-label", "Bucket mix per week, " + weeks.length + " weeks, series " +
      slots.map(function (s) { return s.label; }).join(", "));
    var stack = d3.stack().keys(slots.map(function (s) { return s.key; }))(weeks);
    var total = function (w) { return d3.sum(slots, function (s) { return w[s.key]; }); };
    var y = niceInt(d3.max(weeks, total)).range([H - m.b, m.t]);
    var x = d3.scaleBand().domain(weeks.map(function (w) { return w.week; }))
      .range([m.l, W - m.r]).paddingInner(0.35).paddingOuter(0.2);
    var bw = Math.min(x.bandwidth(), 36);
    var off = (x.bandwidth() - bw) / 2;

    var grid = svg.append("g").attr("class", "grid");
    intTicks(y).forEach(function (t) {
      grid.append("line").attr("x1", m.l).attr("x2", W - m.r).attr("y1", y(t)).attr("y2", y(t));
      svg.append("text").attr("class", "tick").attr("x", m.l - 6).attr("y", y(t) + 4)
        .attr("text-anchor", "end").text(t);
    });
    weeks.forEach(function (w) {
      svg.append("text").attr("class", "tick").attr("x", x(w.week) + x.bandwidth() / 2).attr("y", H - 8)
        .attr("text-anchor", "middle").text(shortDay(w.week));
    });

    stack.forEach(function (layer, si) {
      var color = "var(--series-" + (si + 1) + ")";
      svg.append("g").selectAll("rect").data(layer).enter().append("rect").attr("class", "seg")
        .attr("x", function (d) { return x(d.data.week) + off; })
        .attr("width", bw)
        .attr("y", function (d) { return y(d[1]); })
        .attr("height", function (d) { return Math.max(0, y(d[0]) - y(d[1])); })
        .style("fill", color)
        .call(hover, function (ev, d) {
          var w = d.data;
          tip(ev, "Week ending " + shortDay(w.week), slots.map(function (s, i) {
            return { label: s.label, value: w[s.key], color: "var(--series-" + (i + 1) + ")" };
          }).concat([{ label: "Total", value: total(w) }]));
        });
    });
    // Column totals in text ink.
    weeks.forEach(function (w) {
      var t = total(w);
      if (t > 0) {
        svg.append("text").attr("class", "val").attr("text-anchor", "middle")
          .attr("x", x(w.week) + x.bandwidth() / 2).attr("y", y(t) - 5).text(t);
      }
    });

    // Direct labels at the last column, nudged apart so they never overlap.
    var lastW = weeks[weeks.length - 1];
    var labels = slots.map(function (s, i) {
      var seg = stack[i][weeks.length - 1];
      return { label: s.label, y: (y(seg[0]) + y(seg[1])) / 2 + 4, slot: i, has: lastW[s.key] > 0 };
    }).filter(function (l) { return l.has; });  // no label for a series with no segment
    var minGap = 13;
    for (var pass = 0; pass < 3; pass++) {
      // Slots are bottom-up in value, so y decreases with index; enforce spacing upward.
      for (var i = 1; i < labels.length; i++) {
        if (labels[i - 1].y - labels[i].y < minGap) labels[i].y = labels[i - 1].y - minGap;
      }
    }
    var lx = x(lastW.week) + off + bw + 8;
    labels.forEach(function (l) {
      svg.append("text").attr("class", "cat").attr("x", lx).attr("y", l.y).text(l.label);
    });

    var legend = document.createElement("div");
    legend.className = "chart-legend";
    slots.forEach(function (s, i) {
      var item = document.createElement("span");
      item.className = "legend-item";
      var sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = "var(--series-" + (i + 1) + ")";
      item.appendChild(sw);
      item.appendChild(document.createTextNode(s.label));
      legend.appendChild(item);
    });
    body.appendChild(legend);
  }

  // ─── data flow ────────────────────────────────────────────────────────────

  var CHARTS = [
    { id: "chart-line", key: "line", draw: lineChart, name: "New A+B per day" },
    { id: "chart-funnel", key: "funnel", draw: funnelChart, name: "Funnel" },
    { id: "chart-mix", key: "mix", draw: mixChart, name: "Bucket mix per week" },
    { id: "chart-status", key: "status", draw: statusChart, name: "Applications by status" }
  ];

  function render(data) {
    last = data;
    CHARTS.forEach(function (c) {
      var body = document.querySelector("#" + c.id + " .body");
      body.textContent = "";
      var block = data[c.key];
      if (!block.enough) { empty(body, c.name); return; }
      c.draw(body, block);
    });
  }

  function range() {
    var form = document.getElementById("dash-controls");
    var r = form ? new FormData(form).get("range") : null;
    return r || "7";
  }

  function load() {
    var n = ++seq;
    return fetch(ui.section.dataset.api + "?range=" + encodeURIComponent(range()))
      .then(function (r) { return r.json(); })
      .then(function (data) { if (n === seq) render(data); })
      .catch(function () {
        document.querySelectorAll("#charts .body").forEach(function (b) {
          b.textContent = "Chart data failed to load.";
        });
      });
  }

  function init() {
    ui.section = document.getElementById("charts");
    if (!ui.section || !window.d3) return;
    ui.tip = document.getElementById("chart-tooltip");
    document.body.addEventListener("dash-refresh", load);
    load();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
