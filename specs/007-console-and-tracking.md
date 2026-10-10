# 007 — Console & application tracking

FastAPI + Jinja2 + HTMX, bound to `127.0.0.1:8808`. Server-rendered, no build step, no SPA, no
auth (localhost only). `jobhunter console` starts it.

The console has two jobs: **triage fast**, then **never lose track of an application.** It is
optimized for the former, because triaging 100 scored jobs is a weekly chore and anything that
makes it slow means it does not happen.

## Views

### `/` Today dashboard

The landing page: KPI tiles, a US map with Alaska and Hawaii, per-state stats and coverage
status, and trend charts. Designed separately in [013](013-dashboard.md).

### `/inbox` Inbox — the triage surface

**Grouped by compatibility bucket** ([006](006-fit-scoring.md#compatibility-buckets)), not by a
flat score. Duplicates collapsed, one row per `job_group`.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Inbox   A 6   B 23   C 11   D 9   E 4   F 31   G ·hidden·      $4.12 / wk   │
│         └─ buckets. F = matches your old experience, not your current work  │
├── A · BULLSEYE ── apply, minimal tailoring ────────────────────────────── 6 ─┤
│  87  Systems Administrator IV            CO Dept of Revenue   Denver, CO    │
│      $118k–$142k · full-time · onsite · posted 2d · Connecting Colorado     │
│      recent-skills 92 · senior 85 · domain 78 · comp 71 · loc 95            │
│      ⚑ on_call_heavy                                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  81  IT Specialist (INFOSEC) GS-2210-13  VA · federal   Denver, CO +13 ⊕    │
│      $117k–$152k · full-time · remote · posted 1d · USAJOBS                 │
│      recent-skills 88 · senior 90 · domain 72 · comp 80 · loc 88            │
├── B · STRONG ── apply, tailor to the gaps ───────────────────────────── 23 ─┤
│  64  Network Engineer II                 Private employer    Boulder, CO    │
│      salary not stated ⚠ · full-time · hybrid · posted 3d · ConnectingCO    │
├── F · STALE MATCH ── matched your pre-2019 work ─ collapsed ─────────── 31 ─┤
│  ▸  31 jobs matched on skills you last used 7+ years ago.  [show]           │
│     most common: VMware (11) · Windows/AD (9) · Oracle DBA (6)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

Deliberate choices in that layout:

- **Buckets are the organizing principle**, each with its action in the header. You work
  top-down and stop when the quality drops off.
- **Bucket F is collapsed and summarized by *why*.** Those 31 jobs are precisely the noise you
  described — the ones a keyword search ranks highly because they match your resume's oldest
  third. They are counted, grouped by which stale skill triggered them, and out of your way,
  but not hidden: if `VMware (11)` keeps appearing, either your `done_with` list is wrong or
  the recency weighting needs tuning, and you can see that at a glance.
- **`recent-skills`, not `skills`.** The displayed number is `recency_weighted_skills`. Hovering
  shows `raw_skills` alongside it, so the gap is inspectable.

- **Dimension scores inline.** A single number hides the thing you actually want to know — an
  84 that is "great skills, terrible comp" is a different decision than an 84 that is mediocre
  across the board.
- **`shape_flags` as visible badges** (`⚑ on_call_heavy`), never folded into the score. Your
  `narrative.avoid` entries are not negotiable the way a score is.
- **"salary not stated" is shown as its own state** (⚠), never as `$0` or a blank. This comes
  straight from `missing_info` and is the distinction a score erases.
- **`Denver, CO +13 ⊕`** — multi-location federal postings appear once here and expand inline
  ([011](011-federal-and-geography.md#display-rules)).
- **Source name on every row**, so you always know which market you are looking at.

**Keyboard-driven triage** (the feature that determines whether this gets used):

| Key | Action |
|---|---|
| `j` / `k` | next / previous |
| `Enter` | expand detail inline |
| `s` | shortlist → `label: interesting`, creates an `application` at `interested` |
| `1`–`7` | jump to bucket A–G |
| `f` | toggle bucket F (stale matches) visibility |
| `x` | dismiss → `label: not_interesting` |
| `d` | request Stage 3 deep pass now (Opus 5, ~5s, shown inline) |
| `o` | open the original posting in a browser |
| `a` | mark applied (opens the application form) |
| `A` | open the resolved application link ([015](015-apply-links.md)) |
| `u` | undo the last action |

Every `s`/`x` writes a `label` row, which is the calibration set from
[006](006-fit-scoring.md#calibration). Triage *is* the labeling, with no separate chore.

**Bulk triage.** Each row has a checkbox (accessible name: the job title). Selecting one or
more shows a sticky bar with "N selected", **Shortlist**, **Dismiss** and **Clear selection**;
the bar's header checkbox selects all visible rows (tri-state), shift-click selects a range,
and `Space` toggles the focused row. While anything is selected, `s`/`x` apply to the whole
selection (the bar says so), otherwise to the focused row; `u` undoes the last action, which
for a bulk action is the whole batch. A bulk action is one request (`POST /inbox/bulk`, up to
500 ids) applied in one transaction, so it is all-or-nothing, and `POST /inbox/bulk/undo`
reverses it in one step. Why: a morning of triage is often "dismiss these 12 of the same
kind", and 12 keystrokes plus 12 round trips is both slow and leaves a half-applied state if
the page is interrupted. Bulk writes the same `label` rows as single triage, so calibration
is unaffected.

### `/job/{group_id}` Detail

At the top: the **Apply** button, which goes straight to the employer's application after
following every redirect and aggregator hop. It names the destination (**Apply on Workday ↗**),
shows the resolved chain underneath, and greys out when the posting has closed. Design:
[015](015-apply-links.md).

Then the full description, score breakdown with per-dimension rationale, and **evidence quotes
highlighted in the description text** — so you can see exactly what the model was looking at
when it said "strong skills match." Quotes that failed verbatim verification are badged
`unverified`, which is how a fabricated rationale becomes visible rather than persuasive.

Also: `blockers`, `missing_info`, `tailoring_hints`, all `job_locations`, duplicate siblings
("also posted by 3 other agencies") with a **split group** control for when dedupe was wrong,
the Stage 3 report if present, and a disagreement banner if Stage 3 contradicted Stage 2.

### `/pipeline` Applications

Kanban over `application.status`:

```
interested │ preparing │ applied │ acknowledged │ screening │ interview │ offer
                                                                    ├─ rejected
                                                                    └─ withdrawn
```

Drag or keyboard to move a card; each move appends an `application_event`, never overwrites.
Cards show days-in-status and go amber past a per-status threshold — `applied` with no
acknowledgement for 21 days is the state that actually needs your attention.

Per application: the resume version sent, cover letter, the board's confirmation number,
contacts, notes, attachments, and the full event history.

### `/followups` Next actions

Flat date-sorted list of everything with `next_action_at` due or overdue, plus auto-generated
nudges (`applied` + 21 days with no event → "follow up or mark no_response"). This is the page
that exists because the failure mode of a job hunt is not a bad application, it is forgetting
one.

### `/sources` Health — the page that keeps 55 sources alive

One row per source: status, last successful run, jobs in the last 7 days vs
`expect.min_jobs_per_week`, resolve backlog depth, error rate, family signals, `robots.txt`
status and check date, and `policy`.

Non-`ok` sources sort to the top with the reason spelled out. A `blocked` source says
*"blocked: robots.txt disallows generic agents (checked 2026-10-09) — needs your decision"*,
not just a red dot. See [008](008-compliance.md).

### `/rejected` Prefilter audit

What Stage 1 dropped, filterable by rule. Exists so the prefilter stays honest: if
`salary_floor` is quietly discarding jobs you would have wanted, this is the only place you
would ever find out ([006](006-fit-scoring.md#stage-1--deterministic-prefilter)).

### `/search` Full-text

FTS5 over title, employer, and description, with filters for state, source class, score range,
salary, and date. Covers every job ever ingested, including dismissed ones.

### `/prefs` Preferences

Salary floors, states, weights, bucket thresholds, search queries, spend caps and the narrative
blocks, with a live preview of what each change does before you save. Free settings re-apply
instantly; settings the model reads show a re-scoring cost estimate. Design:
[014](014-preferences-console.md).

### `/costs` Spend

Daily and weekly LLM spend by model and tier against the configured cap, token counts, cache
hit rate, and cost per shortlisted job. The cache hit rate is on this page specifically because
a silent caching failure costs 10x and is otherwise invisible
([006](006-fit-scoring.md#prompt-construction--caching)).

## Application tracking details

**Status is derived, events are the truth.** `application.status` is a rebuildable cache of the
latest `application_event`. Six weeks later, "what actually happened with this one" is answered
by the event log, not by a single mutated field.

**Applying stays manual** ([001](001-goals-and-scope.md#non-goals)). The console makes it fast —
`a` opens a form pre-filled with the posting URL and reference number, and a one-click copy of
the tailoring hints — and then you apply on the board yourself.

### Optional: Gmail matching

You have Gmail connected. An opt-in `jobhunter mail sync` scans for messages matching known
employers and application references and proposes `application_event` rows
(`acknowledged`, `rejected`, `interview`) for you to accept.

**Proposals only — never auto-applied.** An automated misread that silently moves a live
application to `rejected` is worse than no automation at all. Each proposal shows the matched
email and an accept/dismiss control. Milestone 9.
