// Today dashboard charts (specs/013 "Charts"): line, funnel, stacked columns, status bars,
// and the outcomes Sankey (own small layout, no d3-sankey; see "Outcomes Sankey" below).
// d3 alone (vendored); colors come from CSS tokens via style="fill:var(--...)", so a theme
// change recolors the marks without a redraw.
(function () {
  "use strict";

  // "Bullseye + Strong": the A+B group, named from the injected bucket table.
  function abName() {
    var n = window.jh && window.jh.bucketName;
    return n ? n("A") + " + " + n("B") : "Bullseye + Strong";
  }

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

  function tip(ev, title, rows, tipEl, boxEl) {
    var t = tipEl || ui.tip;
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
    var box = (boxEl || ui.section).getBoundingClientRect();
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

  // ─── line: new Bullseye + Strong per day ────────────────────────────────────────────────

  function lineChart(body, block) {
    var pts = block.points;
    var H = 220, m = { l: 30, r: 62, t: 12, b: 26 };
    var svg = svgIn(body, H);
    svg.attr("aria-label", "New " + abName() + " per day, last " + pts.length + " days, mean of the last 7 days " +
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
          { label: "New " + abName(), value: best.count, color: "var(--series-1)" },
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


  // ─── Outcomes Sankey ──────────────────────────────────────────────────────
  // Deterministic layout: one column per stage, node height proportional to value (3px floor so
  // a one-group node stays visible), links are cubic-bezier bands stacked in node order. The
  // payload comes from dashboard.sankey(); zero-value nodes never arrive.

  var KIND_COLOR = {
    flow: "var(--series-1)", win: "var(--series-3)", bad: "var(--series-2)", loss: "var(--text-muted)"
  };
  var sankeyData = null;
  var sankeyWidth = 0;

  function wrapWords(text, max) {
    var lines = [], cur = "";
    text.split(" ").forEach(function (w) {
      if (cur && (cur + " " + w).length > max) { lines.push(cur); cur = w; }
      else cur = cur ? cur + " " + w : w;
    });
    if (cur) lines.push(cur);
    return lines;
  }

  function layoutSankey(data, width, height) {
    var nw = 10, cols = d3.max(data.nodes, function (n) { return n.col; }) + 1;
    var byId = {};
    data.nodes.forEach(function (n) { byId[n.id] = n; n.in = []; n.out = []; });
    data.links.forEach(function (l) {
      l.s = byId[l.source]; l.t = byId[l.target];
      l.s.out.push(l); l.t.in.push(l);
    });
    var colNodes = d3.range(cols).map(function (c) {
      return data.nodes.filter(function (n) { return n.col === c; });
    });
    var pad = 10, k = Infinity;
    colNodes.forEach(function (ns) {
      if (!ns.length) return;
      var sum = d3.sum(ns, function (n) { return n.value; });
      k = Math.min(k, (height - pad * (ns.length - 1)) / sum);
    });
    var step = (width - nw) / Math.max(1, cols - 1);
    var fit = false;
    for (var guard = 0; !fit && guard < 80; guard++) {
      fit = true;
      colNodes.forEach(function (ns) {
        var total = d3.sum(ns, function (n) { return Math.max(3, n.value * k); }) + pad * Math.max(0, ns.length - 1);
        if (total > height + 0.5) fit = false;
      });
      if (!fit) k *= 0.95;
    }
    // Neighbors sit far enough apart that their labels (n.lh tall, centered on the node)
    // never overlap, even when the node itself is a 3px sliver.
    var bottom = height;
    colNodes.forEach(function (ns, c) {
      var y = 0, prev = null;
      ns.forEach(function (n) {
        n.x0 = c * step; n.x1 = c * step + nw;
        n.h = Math.max(3, n.value * k);
        if (prev) y += Math.max(pad, (prev.lh + n.lh) / 2 - (prev.h + n.h) / 2);
        n.y0 = y; n.y1 = y + n.h;
        y = n.y1;
        prev = n;
      });
      bottom = Math.max(bottom, y);
    });
    data.nodes.forEach(function (n) {
      var oy = n.y0;
      n.out.sort(function (a, b) { return a.t.y0 - b.t.y0; }).forEach(function (l) {
        l.w = n.h * (l.value / n.value); l.sy = oy; oy += l.w;
      });
      n.in.sort(function (a, b) { return a.s.y0 - b.s.y0; });
    });
    data.nodes.forEach(function (n) {
      var iy = n.y0;
      n.in.forEach(function (l) {
        l.tw = n.h * (l.value / n.value); l.ty = iy; iy += l.tw;
      });
    });
    return { nw: nw, cols: cols, height: bottom };
  }

  function sankeyTable(wrap, data, pctOf) {
    wrap.textContent = "";
    var tbl = document.createElement("table");
    var head = document.createElement("tr");
    ["From", "To", "Job groups", "% of From"].forEach(function (h) {
      var th = document.createElement("th");
      th.textContent = h;
      head.appendChild(th);
    });
    var thead = document.createElement("thead");
    thead.appendChild(head);
    tbl.appendChild(thead);
    var tb = document.createElement("tbody");
    data.links.forEach(function (l) {
      var tr = document.createElement("tr");
      [l.s.label, l.t.label, l.value.toLocaleString(), pctOf(l.value, l.s.value)].forEach(function (v, i) {
        var td = document.createElement("td");
        td.textContent = v;
        if (i > 1) td.className = "num";
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    wrap.appendChild(tbl);
  }

  function pctOf(v, d) { return d ? Math.round(v / d * 100) + "%" : "n/a"; }

  // One Sankey into ``body``. ``data`` = {nodes, links}, columns numbered from 0.
  function drawSankey(box, body, data, width, narrow, ariaLabel) {
    var colCount = d3.max(data.nodes, function (n) { return n.col; }) + 1;
    var counts = d3.range(colCount).map(function (c) {
      return data.nodes.filter(function (n) { return n.col === c; }).length;
    });
    var plotH = Math.max(narrow ? 330 : 280, d3.max(counts) * (narrow ? 38 : 30));
    var m = { l: 4, r: 4, t: 6, b: 6 };
    var lastC = colCount - 1;
    var gap0 = (width - m.l - m.r - 10) / Math.max(1, lastC);
    var maxChars = narrow ? Math.max(8, Math.floor((gap0 - 10) / 5.6)) : 40;
    data.nodes.forEach(function (n) {
      n.lines = wrapWords(n.label, maxChars);
      n.lh = (n.lines.length + 1) * 12;
    });
    var lay = layoutSankey(data, width - m.l - m.r, plotH);
    plotH = Math.ceil(lay.height);
    var svg = d3.select(body).append("svg").attr("class", "sankey")
      .attr("viewBox", "0 0 " + width + " " + (plotH + m.t + m.b))
      .attr("role", "img")
      .attr("aria-label", ariaLabel);
    var g = svg.append("g").attr("transform", "translate(" + m.l + "," + m.t + ")");
    var tipEl = box.querySelector(".chart-tip");

    function path(l) {
      var x0 = l.s.x1, x1 = l.t.x0, xm = (x0 + x1) / 2;
      var a0 = l.sy, a1 = l.sy + l.w, b0 = l.ty, b1 = l.ty + l.tw;
      return "M" + x0 + "," + a0 + "C" + xm + "," + a0 + " " + xm + "," + b0 + " " + x1 + "," + b0 +
        "L" + x1 + "," + b1 + "C" + xm + "," + b1 + " " + xm + "," + a1 + " " + x0 + "," + a1 + "Z";
    }

    g.append("g").selectAll("path").data(data.links).enter().append("path")
      .attr("class", "slink").attr("d", path)
      .style("fill", function (l) { return KIND_COLOR[l.t.kind]; })
      .call(hover, function (ev, l) {
        tip(ev, l.s.label + " → " + l.t.label, [
          { label: "Job groups", value: l.value.toLocaleString() },
          { label: "Of " + l.s.label, value: pctOf(l.value, l.s.value) }
        ], tipEl, box);
      });
    // hover() hides the shared tooltip on leave; the Sankey has its own.
    g.selectAll("path.slink").on("mouseleave", function () { tipEl.hidden = true; });

    var lastCol = lay.cols - 1;
    var node = g.append("g").selectAll("g.snode").data(data.nodes).enter().append("g").attr("class", "snode");
    node.each(function (n) {
      var el = d3.select(this);
      var host = n.href ? el.append("a").attr("href", n.href) : el;
      if (n.href) host.attr("aria-label", n.label + ", " + n.value.toLocaleString() + " job groups, open list");
      host.append("rect").attr("class", "snode-rect").attr("x", n.x0).attr("y", n.y0)
        .attr("width", lay.nw).attr("height", n.h).attr("rx", 2)
        .style("fill", KIND_COLOR[n.kind]);
      var left = n.col === lastCol;
      var tx = left ? n.x0 - 6 : n.x1 + 6;
      var lines = n.lines;
      var lh = 12;
      var y0 = n.y0 + n.h / 2 - ((lines.length + 1) * lh) / 2 + lh - 2;
      var t = host.append("text").attr("class", "slabel" + (n.href ? " linked" : ""))
        .attr("text-anchor", left ? "end" : "start");
      lines.forEach(function (ln, i) {
        t.append("tspan").attr("x", tx).attr("y", y0 + i * lh).text(ln);
      });
      t.append("tspan").attr("class", "sval").attr("x", tx).attr("y", y0 + lines.length * lh)
        .text(n.value.toLocaleString());
      host.append("rect").attr("class", "hit").attr("x", n.x0 - 2).attr("y", n.y0 - 2)
        .attr("width", lay.nw + 4).attr("height", n.h + 4);
      host.on("mousemove", function (ev) {
        var rows = n.out.map(function (l) {
          return { label: "→ " + l.t.label, value: l.value.toLocaleString() + " (" + pctOf(l.value, n.value) + ")" };
        });
        tip(ev, n.label + ": " + n.value.toLocaleString(),
          rows.length ? rows : [{ label: "End of path", value: "" }], tipEl, box);
      }).on("mouseleave", function () { tipEl.hidden = true; });
    });
  }

  // Narrow screens cannot fit six labeled columns, so the same flows are drawn as two stacked
  // diagrams: fetch to triage, then shortlisted to outcome. Both read left to right.
  function splitSankey(data) {
    var late = ["shortlisted", "elsewhere"];
    function part(pick, remap) {
      var nodes = data.nodes.filter(pick).map(function (n) {
        var c = JSON.parse(JSON.stringify(n));
        c.col = remap(n);
        return c;
      });
      var ids = {};
      nodes.forEach(function (n) { ids[n.id] = true; });
      return {
        nodes: nodes,
        links: data.links.filter(function (l) { return ids[l.source] && ids[l.target]; })
          .map(function (l) { return { source: l.source, target: l.target, value: l.value }; })
      };
    }
    return [
      part(function (n) { return n.col <= 3 && n.id !== "elsewhere"; }, function (n) { return n.col; }),
      part(function (n) { return n.col >= 4 || late.indexOf(n.id) >= 0; },
        function (n) { return n.col >= 4 ? n.col - 3 : 0; })
    ];
  }

  function sankeyChart(box) {
    var body = box.querySelector(".body");
    body.textContent = "";
    var data = JSON.parse(JSON.stringify(sankeyData));
    if (!data.nodes.length || data.total < 1) { empty(body, "Outcomes"); return; }
    sankeyWidth = box.clientWidth;
    var narrow = sankeyWidth < 560;
    var width = Math.max(300, Math.floor(sankeyWidth || 640));
    var label = "Outcomes Sankey, " + data.total.toLocaleString() +
      " job groups from fetch to outcome. A table follows.";
    var byId = {};
    data.nodes.forEach(function (n) { byId[n.id] = n; });
    var rows = data.links.map(function (l) {
      return { s: byId[l.source], t: byId[l.target], value: l.value };
    });
    if (narrow) {
      splitSankey(data).forEach(function (part, i) {
        if (part.nodes.length < 2) return;
        drawSankey(box, body, part, width, true, label + (i ? " Part two, shortlisted to outcome." : " Part one, fetch to triage."));
      });
    } else {
      drawSankey(box, body, data, width, false, label);
    }
    sankeyTable(box.querySelector(".sankey-table .table-wrap"), { links: rows }, pctOf);
  }


  function initSankey() {
    var box = document.getElementById("chart-sankey");
    var dataEl = document.getElementById("sankey-data");
    if (!box || !dataEl || !window.d3) return;
    try { sankeyData = JSON.parse(dataEl.textContent); } catch (e) { return; }
    sankeyChart(box);
    var timer = null;
    window.addEventListener("resize", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (Math.abs(box.clientWidth - sankeyWidth) > 8) sankeyChart(box);
      }, 120);
    });
  }

  // ─── data flow ────────────────────────────────────────────────────────────

  var CHARTS = [
    { id: "chart-line", key: "line", draw: lineChart, name: "New Bullseye + Strong per day" },
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
    initSankey();
    ui.section = document.getElementById("charts");
    if (!ui.section || !window.d3) return;
    ui.tip = document.getElementById("chart-tooltip");
    document.body.addEventListener("dash-refresh", load);
    load();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
