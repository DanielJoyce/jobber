# 013 — Today dashboard, map & charts

The console's landing page, built for a **daily** visit while you search **out of state**. It
answers three questions in order: *what's new for me today, where in the country is it, and is
my search working?*

The inbox ([007](007-console-and-tracking.md)) moves to `/inbox`. `/` becomes this page, and
everything on it links into the inbox with a filter applied.

Chart decisions follow the dataviz method: pick the form first, assign color by its job,
validate the palette, and keep status signals separate from magnitude.

## Layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Today · Thu Oct 9          range: [7d] 30d 90d          ◐ theme             │
├──────────────┬──────────────┬──────────────┬──────────────┬──────────────────┤
│  NEW A + B   │  IN FLIGHT   │  FOLLOW-UPS  │ RESPONSE 30d │  LLM SPEND / wk  │
│     14       │     11       │    3 due     │  18%  (4/22) │   $6.20 / $10    │
│  ▁▂▅▃▇ +5    │  ▃▃▅▆▆       │  1 overdue ⚠ │  ▂▃▃▅        │  ▅▆▅▆            │
├──────────────┴──────────────┴──────────────┴──────────────┴──────────────────┤
│  MAP   metric: [New A+B ▾]   view: (•) Shape  ( ) Grid   layer: [✓] status   │
│                                                                              │
│        ┌───────── contiguous US, Albers projection ──────────┐  ┌ VT NH MA ┐ │
│        │                                                     │  │ RI CT NJ │ │
│        │                                                     │  │ DE MD DC │ │
│        └─────────────────────────────────────────────────────┘  └ callouts┘ │
│   ┌ AK inset ┐  ┌ HI inset ┐        ┌ Remote (US) ┐  ┌ PR GU MP VI ┐       │
│   └──────────┘  └──────────┘        │   23 A+B    │  └ tiles ─────┘       │
│                                     └─────────────┘                         │
│   legend: 0 ░░▒▒▓▓██ 12+     ● direct  ✉ email only  ⚠ source problem      │
├──────────────────────────────────────┬───────────────────────────────────────┤
│  NEW A+B JOBS PER DAY (line)         │  FUNNEL, last 30d (horizontal bars)   │
├──────────────────────────────────────┼───────────────────────────────────────┤
│  BUCKET MIX PER WEEK (stacked bars)  │  APPLICATIONS BY STATUS (bars)        │
├──────────────────────────────────────┴───────────────────────────────────────┤
│  STATE TABLE (sortable; the map's table view)                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

One range control at the top drives every panel. Panels refresh with HTMX partial swaps when it
changes. No auto-refresh; data changes once a night.

## KPI row

Single current values are stat tiles, not charts.

| Tile | Value | Sparkline | Click → |
|---|---|---|---|
| **New A + B** | Bucket A and B job groups first seen in range | daily count | inbox, buckets A+B, new |
| **In flight** | Applications in `applied` through `interview` | daily count | `/pipeline` |
| **Follow-ups** | Due today, with overdue count and a ⚠ icon | — | `/followups` |
| **Response rate (30d)** | Applications with any employer response ÷ applied, **with n shown** (`4/22`) | weekly | `/pipeline` |
| **LLM spend / week** | Spend against the cap | daily | `/costs` |

The response rate always shows its denominator. "18%" on 22 applications and on 4 are
different claims, and a lone percentage hides that.

## The map

### Geometry

**Shape view (default).** True state outlines in the Albers USA projection, with **Alaska and
Hawaii as insets**. Built from the public-domain `us-atlas` TopoJSON (`states-albers-10m.json`,
already projected with the insets in place), vendored into the app. No network, no tile server.

Small Northeastern states (VT, NH, MA, RI, CT, NJ, DE, MD, DC) are too small to hover or read on
a true-shape map, so each gets a **callout box** at the right edge, linked by a hairline leader
and carrying the same fill and status icon as the state. PR, GU, MP and VI are not in `us-atlas`
and appear as a row of tiles below the insets.

**Grid view (toggle).** Every state is an equal square in a rough geographic layout. Rhode
Island gets the same space as Texas, so small states are legible and you compare values rather
than land area. A real trade-off: Shape is easier to recognize, Grid is easier to read. The
toggle is remembered.

**Remote (US)** is a tile beside the map, not a state. Remote jobs belong to no state; drawing
them on all 50 would double-count them, and drawing them nowhere would hide your largest
pool. A tile with the same color scale keeps them visible and counted once.

### What the fill shows

**One measure at a time, chosen from a selector**, on a single-hue sequential blue ramp
(light = few, dark = many, `#cde2fb` → `#0d366b`). Never a rainbow, never two measures on one map.

| Metric | Meaning | Why you'd look |
|---|---|---|
| **New A+B** (default) | Bucket A+B job groups first seen in range | Where today's good matches are |
| All scored jobs | Everything that reached Stage 2 | Where the volume is |
| Shortlisted | Your `s` presses | Where your interest is landing |
| Applied | Applications created | Where you've actually committed |
| Response rate | Responses ÷ applications, **only where n ≥ 5** | Where applications go somewhere |
| Median offered salary | Over stated salaries of A+B jobs | Pay by state |
| **COL-adjusted salary** | Median salary ÷ the state's Regional Price Parity | Pay in real terms, for a relocation decision |

Response rate on very small n is noise. States below the threshold are filled with the neutral
"no data" gray and a tooltip says *n = 2, too few to rate*, so they don't read as 0% or 100%.

COL adjustment uses the Bureau of Economic Analysis **Regional Price Parities** by state:
annual, public, and a 51-row table refreshed once a year. Relevant because moving from Washington
to, say, Ohio changes what a salary is worth.

Class breaks are **quantiles over the visible range**, five classes, with the legend printing
the actual break values. States with zero get the lightest step, and states with no data at
all get the neutral gray, a separate swatch in the legend.

### Status layer — icons, not fill

Coverage and source health are *state*, not magnitude, so they **never share the fill
channel.** They appear as a small icon plus short label in each state's corner, toggled by the
"status" checkbox:

| Icon | Meaning | Color |
|---|---|---|
| ● | Collected directly: open board or NLx | status *good* `#0ca30c` |
| ✉ | Email alerts only: board blocks crawling | secondary ink |
| ✉? | Should be on email but no alerts received in 3 days | status *warning* `#fab219` |
| ⚠ | Source `suspect` or `broken` | status *serious* `#ec835a` / *critical* `#d03b3b` |
| ○ | Not covered | muted ink |

Status colors come from the reserved status palette and always ship with an icon. Text labels
appear in the tooltip and the table, so color is never the only signal.

### Your applications

Each state where you've applied carries a small numeral badge (`3`), so the map doubles as a
record of where you've placed bets. In the Applied metric the badge is hidden because the fill
already says it.

### Interaction

- **Hover** a state, callout, inset, or the Remote tile: a tooltip with name, the current
  metric value, new A+B, total scored, applied, responses, median and COL-adjusted salary,
  coverage status with source names, and last successful collection.
- **Click** opens `/inbox?state=XX` filtered to that state. Multi-location federal jobs appear in
  every state they list ([011](011-federal-and-geography.md#display-rules)).
- **Hit targets** are the full state shape, or the full callout box for small states, never just
  the label.
- **Keyboard:** states are focusable in the order of the state table, and Enter acts as click.

### Table view

Below the charts sits the **state table**: one sortable row per state plus Remote and
territories, with every map metric as a column, coverage status as icon + text, and sources.
It is the map's accessible equivalent, it's where precise comparisons happen, and
sorting by "New matches" is the fastest way to answer *where should I look this week?*

### Bucket filter

Above the map, one toggle chip per bucket, shown by name (Bullseye, Strong, ...) with that
bucket's count in the range; plus **All fits** (A-F) and **Reset** (Bullseye + Strong, the
default). Chips are `aria-pressed` buttons; at least one stays on. The selection drives the
"New: ..." metric, the legend, tooltips (with a per-bucket split), the median-salary metrics
and the state table. It lives in `?buckets=A,B` (letters in URLs) and `localStorage`
(`jh-dash-buckets`), the URL winning. The map payload carries `by_bucket` per state and
`bucket_totals`; a toggle re-fetches the payload, table, KPI tile and trend/funnel charts (the bucket-mix chart always shows every bucket). The state table sits in a `<details>` collapsed by default (`jh-dash-states`) and still updates while closed. Clicking a state opens
`/inbox?state=XX&bucket=A,B`; the inbox accepts a comma list. Names come from
`core/bucketnames.py`; the KPI tile, trend line and funnel use the fixed group "Bullseye +
Strong".

## Charts

| Panel | Form | Encoding | Notes |
|---|---|---|---|
| **New A+B per day** | Line, single series | slot 1 blue, 2px line | Crosshair tooltip. One line, no legend, the title names it. A dashed 7-day mean as a reference line |
| **Funnel, last 30d** | **Horizontal bars**, stages in order | one hue, ordinal blue ramp (from step 250) | Found → passed prefilter → A+B → shortlisted → applied → responded → interviewed → offer. Bars, not a funnel graphic, so lengths are comparable. Each bar labeled with count and % of the previous stage. Log scale is off because counts span 6,000 to 1, so the last stages get **their own zoomed panel** rather than a misleading axis |
| **Bucket mix per week** | Stacked columns | 4 categorical slots in fixed order: A, B, C+D+E folded as "other fits", F stale | 4 series, so direct labels plus a legend. G is excluded since it's hidden by design. Shows whether bucket F is shrinking as `done_with` gets tuned |
| **Applications by status** | Horizontal bars | one hue | Ordered by pipeline stage, not count |

Rules applied throughout: one y-axis per chart; thin marks with 2px gaps between stacked
segments; values in text ink, never series color; hover tooltips on every mark; light and dark
themes from the same ramps, each validated against its own surface.

Deliberately absent: pie and donut charts, gauges, dual-axis charts, and a per-state line chart
of all states at once (unreadable past a handful; the map and table answer that question).

### Outcomes Sankey

A Sankey under the trend charts answers "where did everything go?" in one picture, which the
funnel cannot: the funnel only shows the happy path, while the Sankey also shows where jobs
leave it (prefiltered out, dismissed, rejected). It counts **job groups** (duplicates folded),
all time, from `dashboard.sankey()`, embedded in the page as one JSON payload
(`nodes`, `links`, `total`).

- Path: Fetched -> Prefiltered out / Awaiting prefilter / Passed -> Not yet scored or bucket A-G
  -> Untriaged / Dismissed / Shortlisted -> Not applied yet / Applied -> Awaiting response,
  Screening / interview, Offer, Rejected, Withdrawn / closed, No response. Triage comes from the
  `label` table, outcomes from `application.status` (latest event wins). A group with an
  application counts as Shortlisted.
- **Applied elsewhere** is a second left-hand source: applications accepted from a mail
  proposal for a job the pipeline never saw (their group's job is on the `email-manual`
  source). It feeds Applied directly.
- Each group takes one path, so flow is conserved (a node's in equals its out); tested. Zero
  nodes are hidden. `sankey(..., extra_rejected=...)` lets a later change move groups known to
  be rejected from other evidence (employer rejection emails) onto the Rejected node.
- All seven buckets are shown rather than merged, so every node links to an exact list
  (`/inbox?bucket=X`, `/tracking`).
- Layout is a small deterministic routine in `charts.js` (one column per stage, heights
  proportional to value, cubic-bezier bands), not d3-sankey: it avoids a pinned vendor file and
  lets us place direct labels and keep a 3px floor for single-group nodes. Nodes are colored by
  meaning (flow, win, loss, rejected), always with a direct label; links take the target's
  color. Below 560px wide it is drawn as two stacked diagrams (fetch to triage, shortlisted to
  outcome) because six labeled columns do not fit a phone. A "Show as table" disclosure lists
  every link with its percent of the source node.

## Implementation

- Vendored static assets, no build step, no CDN: `d3` (geo path plus scales), `topojson-client`,
  `us-atlas` `states-albers-10m.json`. The four charts are plain d3 (no Observable Plot).
- JSON endpoints feed the panels: `/api/dash/kpis`, `/api/dash/map?metric=&range=`,
  `/api/dash/series?...`. Every number is computed in SQL from the [005](005-data-model.md)
  tables, never cached in the browser.
- State key everywhere is the 2-letter code. `us-atlas` uses FIPS ids, so a 56-row FIPS→USPS map
  lives in `core/geo.py`, shared with location normalization.
- Palette tokens are defined once as CSS custom properties, light and dark. The categorical
  slots used by the bucket chart and the sequential ramp are checked with the palette validator
  as part of the console's tests.
- **Validated 2026-10-09:** the four bucket-chart slots (blue, orange, aqua, yellow) pass every
  check in both modes: worst adjacent colorblind ΔE 9.1, normal-vision ΔE 22.9 light / 19.8
  dark. In light mode, aqua and yellow fall below 3:1 contrast against the surface, so that
  chart **must** keep its direct labels and table view; they're required, not decoration.
- The tile-grid layout is a static 2-letter→(row, col) table.

## Milestone placement

The dashboard lands in **M5** (console). The KPI row and map ship first, charts second. Charts
built on applications and responses only become meaningful after a few weeks of real use, and
the panels say *not enough data yet* until then rather than drawing three points as a trend.
