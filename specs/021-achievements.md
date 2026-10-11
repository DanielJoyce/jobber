# 021 — Achievements

Status: **draft for review, revision 1** (bug `4f6bedb`). Written 2026-10-11.

You asked for "a spec for an achievements board and achievements". A job hunt is long, most of
it gets no reply, and the useful work (a tailored packet, a follow-up, a contact logged) leaves
no visible trace. This spec adds:

- **An achievement catalog** of 31 achievements (53 unlocks counting tiers) in six categories. Each has an exact rule over
  tables jobhunter already keeps, so your existing history counts from day one.
- **`/achievements`**, a board: what you have earned, progress toward the next tier, locked ones
  with a hint, and a few hidden ones.
- **A small notice** in the existing activity band (PR 113) when one unlocks, and a one-line
  strip on Today.
- **Settings** on the board: turn the whole thing off, turn notices off, hide categories.

It rewards habits that make a search work. It does not reward volume for its own sake, and
nothing in it spends scorer or API credits.

## Goals and guardrails

| # | Rule | How the design keeps it |
|---|---|---|
| G1 | **Reward effective habits**: targeted applications, tailored packets, follow-ups, contacts, deciding on jobs, a steady weekly routine, rest | Most of the catalog is in Search, Apply (targeted), Follow-up and Rhythm. Volume has one family, capped |
| G2 | **Never reward spray-and-pray** | The only count-of-applications family (`apply.momentum`) counts at most **5 applications per day** and stops at 50. "Targeted" and "tailored" families need evidence of preparation. `apply.quality_week` rewards a week of 2 to 5 applications that were all targeted. No achievement for applications per day or per hour |
| G3 | **Never reward anything that spends credits** | No rule reads `llm_spend`, `rescore_request`, `rescore_run`, packet generation counts or "Score it" outcomes. Packets count only once **sent** (review done, application made), never per generated version. Hints never say "score" or "generate". A test enforces both ([Testing](#testing)) |
| G4 | **No punishment** | Unlocks are permanent (one narrow exception: [Revocation](#idempotence-and-revocation)). There are **no daily streaks**. The weekly rhythm has freezes and a "Welcome back" achievement for returning after a gap. Copy never says "lost" or "broken" |
| G5 | **Can be turned off**, wholly or by category | [Settings](#settings) |
| G6 | **Nothing leaves the machine** | Pure SQL and Python over the local database. No network, no model call, no share or export button, no telemetry |
| G7 | **No comparison to others** | No percentiles, "rarest", leaderboards or averages. Only your own history |
| G8 | **Outcomes are milestones, not goals** | Interviews and offers depend on employers. They get one-off milestones with no tier ladder, and you can hide the category |

## What exists today (checked in code, 2026-10-11)

The rules read these. Paths are under `src/jobhunter/`.

| Fact | Where |
|---|---|
| `application` (one per job group; `status`, `applied_at`, `resume_version`, `cover_letter_path`, `next_action`, `next_action_at`) | `core/migrations/0001_initial.sql:223` |
| `application_event` (append-only log; `at`, `status`, `note`, `source` manual/email/import) | `0001_initial.sql:240` |
| `contact` (per application; **no timestamp column**) | `0001_initial.sql:252` |
| `label` (one row per group; `interesting` / `not_interesting` / `applied`, `labeled_at` overwritten on relabel) | `0001_initial.sql:205`, written by `console/inbox.py:315` |
| `profile_change` (every prefs save; `source` ui/file/revert) | `0001_initial.sql:271` |
| `fit_score.verdict` (`strong`/`possible`/`weak`/`mismatch`) | `0001_initial.sql:180` |
| `mail_message` (alert emails processed) | `0009_mail_alerts.sql:12` |
| `alert_signup.subscribed_at` | `0021_alert_signup.sql` |
| `rejection` (employer rejections; `state` pending/confirmed) | `0023_rejection.sql`, `0028_rejection_state.sql` |
| `application_packet` (`status` draft/ready/abandoned, `ready_at`), `packet_document.origin` (generated/edited/base) | `0029_application_packet.sql` |
| `capture_log` (extension captures; `outcome`) | `0033_capture_log.sql` |
| A sent packet sets `application.resume_version = 'packet:<id>/resume/v<n>'` on the `applied` event, on every path | `apply/packets.py:186` (`attach_sent_packet`) |
| Regenerating a packet clears `ready_at` | `apply/generator.py:837` |
| Automatic early events carry fixed notes: `shortlisted from inbox`, `opened apply link`, `packet started` | `console/tracking.py:71-74` |
| A dedupe merge writes an event with the winner's status and note `Merged duplicate application (...)` | `pipeline/dedupe_url.py:301-305` |
| "Response" means `screening`, `interview`, `offer` or `rejected` (acknowledged is an automated receipt) | `console/dashboard.py:49` |
| Follow-up **Done** clears `next_action` and keeps **no record** that a follow-up happened | `console/tracking.py:388`, route `console/tracking_routes.py:281` |
| Setting a next action overwrites it; no history | `console/tracking.py:368` |
| The activity band: `GET /activity` on every page, polls every 3 s while work runs and 60 s idle, `role="status"` | `console/activity_routes.py:28-29`, `templates/_activity.html`, `templates/base.html:29` |
| Nav entries: `PAGES` (a `None` label keeps a page out of the nav) | `console/app.py:74-87` |
| The dashboard counts days in UTC | `console/dashboard.py:122` |
| The container runs with `Timezone=local` | `specs/018-containers.md:286` |
| `/setup` readiness checks (`readiness.py`, `essentials_ok`) are **specified, not built** | [019](019-setup-and-readiness.md) S1 |

Your history on a copy of the database (counts only, 2026-10-11): 20 applications, 18 with an
`applied` event (all from accepted Gmail confirmations), on 10 different days across 4 weeks, at
most 6 on one day; 6 confirmed employer rejections; 3 triage labels; 14 prefs saves; 3
extension captures; 0 contacts, 0 packets, 0 next actions set. The backfill section says what
that unlocks.

Two gaps must be closed for the follow-up and networking achievements to mean anything: a
follow-up leaves no trace today, and a contact has no date. [Data](#data) adds both.

## Definitions used by the rules

- **Local day / week.** Days are calendar days in the console process's local time zone
  (`datetime.astimezone()`), weeks are Monday to Sunday. Deliberately not the dashboard's UTC
  day: at UTC an application sent at 6 pm Pacific lands on tomorrow, which would make a rhythm
  feel wrong. The container already runs with the host's zone.
- **Applied date** of an application: the earliest `application_event.at` with
  `status = 'applied'`, ignoring events whose note starts with `Merged duplicate application`.
  An application counts once, however many `applied` events it has.
- **Sent application**: one with an applied date.
- **Targeted** application: a sent application with at least one of
  1. a sent packet: `resume_version LIKE 'packet:%'`;
  2. an `interested` or `preparing` event dated before its applied date (it was on your
     shortlist or in preparation first; the automatic notes count here, they record a real
     shortlist press or packet start);
  3. an existing `fit_score` row for its group with `verdict IN ('strong', 'possible')` and
     `created_at` before its applied date. **Read only**: nothing is ever scored for this.
- **Tailored** application: a sent application with `resume_version LIKE 'packet:%'`.
- **Response**: an `application_event` with status `screening`, `interview` or `offer`
  (rejections are counted separately, never as a "response" achievement).
- **Active day**: a local day with at least one of these deliberate actions:
  an application's applied date; a `label.labeled_at`; a `followup_log` row (new);
  a `contact.created_at` (new); a packet `ready_at`; a `rejection` with `source = 'manual'`;
  a status event `screening`/`interview`/`offer`/`withdrawn`/`no_response` with
  `source = 'manual'`; `alert_signup.subscribed_at`; a `capture_log` row with outcome
  `added`, `linked`, `description_added` or `linked_groups`; a `profile_change` with
  `source = 'ui'`. Not counted: `opened apply link` events, `packet started` events, merge
  events, anything from the scorer or the nightly run.
- **Active week**: a week with **2 or more** active days. One active day a week is still a
  search, but the rhythm is about a habit, and two touches is the lightest habit worth naming.
- **Rest day**: a day in a week that has no active action.

## The catalog

31 achievements, 53 unlocks. A **family** has tiers (each tier is its own unlock, shown as one card with
pips); the others are single. **Hidden** ones show as "Hidden achievement" with their category
until earned. **Lost?** says whether the unlock can go away; the answer is no everywhere except
the 24-hour undo ([Revocation](#idempotence-and-revocation)). **Backfill** is what your current
history unlocks on the first evaluation, silently.

### Setup (4)

| ID | Name | Rule (exact) | Backfill (your data) |
|---|---|---|---|
| `setup.prefs` | Tuned in | First `profile_change` with `source = 'ui'` (saved from `/prefs`). Saving is free; a re-score is a separate, explicit action and is not rewarded | Yes |
| `setup.alerts` | Net cast, tiers **1 / 5** | Count of `alert_signup` rows with `subscribed_at IS NOT NULL` | No (0) |
| `setup.mail` | Inbox sorted | First `mail_message` row (`processed_at`): alert emails are reaching jobhunter | No (0 alert emails processed) |
| `setup.ready` | All set | 019's `essentials_ok` is true. **Added by ticket A6 once 019 S1 lands**; absent from the catalog until then | n/a |

### Search (4)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `search.decide` | Decisive, tiers **10 / 50 / 150 / 300** | Count of `label` rows (any label: saying no is a decision too). The date of a tier is the `labeled_at` of the row that crossed it, ordered by `labeled_at` | No (3) |
| `search.no` | Clear no | 10 `label` rows with `label = 'not_interesting'`. Knowing what you do not want is a skill | No (1) |
| `search.shortlist` | Shortlist | 5 applications with an `interested` event | No (2) |
| `search.found` | Found it yourself | First `capture_log` row with outcome `added`, `linked` or `description_added` (a job you found and sent from the extension). The capture's optional "Score it" is not part of the rule | Yes |

### Apply (7)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `apply.first` | First one out | First sent application | Yes |
| `apply.momentum` | Momentum, tiers **5 / 10 / 25 / 50** | Sent applications, counting **at most 5 per local day** (by applied date). Stops at 50 | 5 and 10 (17 counted of 18: one day had 6) |
| `apply.targeted` | Aimed, not sprayed, tiers **1 / 5 / 15** | Targeted applications | No (the 2 scored groups are your 2 `interested` ones, not yet sent) |
| `apply.tailored` | Tailor-made, tiers **1 / 5 / 10** | Tailored applications | No (0) |
| `apply.own_words` | In your own words | A tailored application whose sent resume version (the `v<n>` in `resume_version`) is a `packet_document` with `origin = 'edited'` | No |
| `apply.cover` | Dear hiring manager | A tailored application with `cover_letter_path` set and its packet's `cover_doc_id` not null | No |
| `apply.quality_week` | Quality week | A local week with **2 to 5** sent applications, **all** targeted | No |

### Follow-up (6)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `follow.plan` | Next step set, tiers **1 / 5 / 15** | Count of `followup_log` rows with `kind = 'planned'` (setting a next-action date). Backfill: each application whose `next_action_at` is set at migration time gets one row dated at migration | No (0 set) |
| `follow.done` | Follow-through, tiers **1 / 5 / 15 / 30** | `followup_log` rows with `kind IN ('done', 'no_response')` | No: no history exists before this spec |
| `follow.on_time` | On time | 5 `followup_log` rows of `kind = 'done'` with `at` on or before the local day of `due_at` | No |
| `follow.tidy` | Loose ends | 5 applications with a `no_response` event (closing stale applications keeps the pipeline honest) | No |
| `follow.contact` | Networker, tiers **1 / 5 / 15** | `contact` rows. Existing rows (no date) count as of migration time | No (0) |
| `follow.outcome_logged` | Noted, moving on | First `rejection` with `state = 'confirmed'`. Recording outcomes keeps your data honest; the copy says so and nothing more | Yes |

### Outcomes (7, hideable)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `out.heard_back` | Heard back | First response event | Depends on your events (none today) |
| `out.conversations` | Conversations, tiers **3 / 10** | Applications with any response event | No |
| `out.interview` | First interview | First `interview` event | No |
| `out.interviews` | Interview circuit | 3 different applications with an `interview` event | No |
| `out.offer` | Offer | First `offer` event | No |
| `out.long_game` | The long game (**hidden**) | A response event 30 or more days after that application's applied date | No |
| `out.second_wind` | Second wind (**hidden**) | An application sent within 14 days after a confirmed `rejection.received_at` | **Yes**: 5 of your 6 rejections were followed by an application within 14 days |

### Rhythm (3)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `rhythm.weeks` | Steady rhythm, tiers **2 / 4 / 8 / 12** | Longest run of consecutive active weeks, freezes applied (below) | Tier 1 (2 weeks): the last 3 weeks each had 2 to 4 active days |
| `rhythm.rest` | Rest is part of it | 3 active weeks that each also had **2 or more** rest days | Yes |
| `rhythm.comeback` | Welcome back | An active week that follows **2 or more** weeks with no active day | No |

Count: 4 + 4 + 7 + 6 + 7 + 3 = **31 cards, 53 unlocks** (each tier is an unlock).
`setup.ready` arrives with A6, so 30 cards and 52 unlocks ship first.

**What your history unlocks on day one** (silently, from the copy): Tuned in, Found it
yourself, First one out, Momentum 5 and 10, Noted moving on, Second wind, Steady rhythm (2
weeks) and Rest is part of it: **9 unlocks**. The strongest next steps the board will show are
Aimed, not sprayed (shortlist or prepare before sending), Next step set and Networker, which
your history has none of yet.

**Freezes.** Rhythm is a weekly count, so one missed week would end it. You hold up to **2
freezes**; you earn one for every 4 consecutive active weeks (counted inside the run). A week
with no active day, when you hold a freeze, uses one automatically and the run continues
(the frozen week does not add to the length). The current week never counts as missed until it
ends. Everything is recomputed from history on each evaluation, so freezes need no storage and
an email confirmation accepted late can repair an earlier week. When the run does end, the
board shows "Rhythm: paused. Your best run is 6 weeks." The earned tiers stay.

**What is deliberately absent:** applications per day, a daily streak, "apply to N jobs this
week", jobs scored, re-scores run, packets generated, time spent, logins, or anything about
other people.

## Data

New migration (the next free number at implementation time; `0034` today). All additive.

```sql
-- One row per unlocked achievement tier. Never updated except seen_at.
CREATE TABLE achievement_unlock (
  id              INTEGER PRIMARY KEY,
  achievement_id  TEXT NOT NULL,             -- 'apply.momentum'
  tier            INTEGER NOT NULL DEFAULT 1, -- 1-based index into the family's thresholds
  unlocked_at     TEXT NOT NULL,             -- when it was earned: the crossing row's date
  detected_at     TEXT NOT NULL,             -- when evaluate() wrote it
  evidence        TEXT NOT NULL,             -- JSON {"application": [12, 15], "label": [...]}
  catalog_version INTEGER NOT NULL,
  seen_at         TEXT,                      -- NULL: notice not yet shown or dismissed
  UNIQUE (achievement_id, tier)
);
CREATE INDEX achievement_unlock_unseen ON achievement_unlock(seen_at) WHERE seen_at IS NULL;

-- Follow-ups had no history (tracking.done clears the field).
CREATE TABLE followup_log (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER REFERENCES application(id),  -- NULLed on application delete
  at             TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('planned', 'done', 'no_response')),
  item_kind      TEXT CHECK (item_kind IN ('action', 'nudge', 'packet')),
  due_at         TEXT                         -- the next_action_at that was due, when known
);
CREATE INDEX followup_log_at ON followup_log(at);

-- Settings: enabled, notify, hidden_categories (JSON list), backfilled_at, fingerprint.
CREATE TABLE achievement_setting (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE contact ADD COLUMN created_at TEXT;  -- NULL for rows that predate this

-- Backfill: current next actions count as planned once.
INSERT INTO followup_log (application_id, at, kind, item_kind, due_at)
  SELECT id, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), 'planned', 'action', next_action_at
  FROM application WHERE next_action_at IS NOT NULL AND next_action_at != '';
```

**Write points** (each in the same transaction as the change it records):

| Action | Writes | Code |
|---|---|---|
| Set or change a next-action date (`set_next_action` with a date) | `followup_log` `planned`, `due_at` = the date. Snooze calls the same function: snooze writes **nothing** (it passes a flag) | `console/tracking.py:368`, `:392` |
| Follow-up **Done** | `done`, with the item's kind and `due_at` | `tracking_routes.py:281` |
| **No response** from follow-ups | `no_response` | `tracking_routes.py:293` |
| Add a contact | `contact.created_at = now` | `console/tracking.py:432` |
| Merge of applications | `followup_log.application_id` re-pointed like the other child tables | `pipeline/dedupe_url.py:293` (the child-table loop) |
| Shortlist undo deleting an application | `followup_log.application_id` set NULL first | `console/inbox.py:387` |

The two dedupe and undo lines matter: both delete `application` rows, and a new child table
with a foreign key would otherwise block them.

`achievement_unlock.evidence` holds table names and row ids only, no text. If a row is later
deleted, the board says "(record since removed)" for it.

### How unlocks are computed

**On read, with a change check.** Not a nightly job (an unlock a day late is no reward) and not
hooks in every write path (a dozen paths, including the CLI's `mail match`, would each need one,
and a missed one silently loses an unlock). One function does it all:

```
achievements.evaluate(conn, now, tz) -> list[Unlock]   # the newly written ones
```

1. If `enabled` is off, return `[]` and write nothing.
2. Compute every rule from scratch: each rule is a Python function over a few SQL queries
   returning, per tier, the crossing date and the evidence ids.
3. `INSERT ... ON CONFLICT (achievement_id, tier) DO NOTHING` for each crossed tier, in one
   `BEGIN IMMEDIATE` transaction. Evaluate twice and the table is unchanged.
4. Apply the revocation rule (below).

**When it runs:**

- `GET /achievements` and the Today strip: always.
- `GET /activity` (every page): only when the **fingerprint** changed. The fingerprint is one
  `SELECT` of `MAX(id)` and `COUNT(*)` over each evidence table (`application_event`, `label`
  by `MAX(labeled_at)`, `contact`, `followup_log`, `rejection`, `application_packet` by
  `MAX(updated_at)`, `alert_signup` by `MAX(updated_at)`, `capture_log`, `profile_change`,
  `mail_message` count) plus the local date (a week rollover can change rhythm). Stored in
  `achievement_setting`. Rowid maxima are index lookups; the counts are over small tables.
  `mail_message` is the largest and still a count over a primary key.
- Writes made by the CLI (`jobhunter mail match`, a proposal accepted from the terminal) are
  picked up by the next poll through the fingerprint. No CLI change is needed.

`/activity` is documented as read-only except for the stale re-score heartbeat
(`activity_routes.py:1-8`). It now also writes unlocks; its docstring says so. A
`sqlite3.Error` (busy database) is swallowed exactly like the existing queries there, so a
locked database never breaks a page; the next poll retries.

**Performance budget:** `evaluate` under 50 ms and the fingerprint under 2 ms on a copy of your
database, and `evaluate` under 250 ms on a synthetic database 10 times larger (200
applications, 3,000 labels, 2,000 events). The rules touch no `job` or `job_fts` rows; the only
large-table read is `fit_score` by `job_group_id` for sent applications, which uses the existing
unique index.

### Backfill

The first evaluation after the migration (or after turning achievements back on) is a
**backfill pass**: it writes every unlock the history supports with `unlocked_at` set to the
real crossing date where one exists (contacts and pre-existing next actions use the migration
time) and `seen_at = detected_at`, so nothing pops up. `achievement_setting.backfilled_at`
records it, and the board shows once: "9 achievements from your history so far" (the real count).

Later unlocks always notify, even when the crossing date is old (an email confirmation accepted
today for an application sent last week is still news to you today).

### Idempotence and revocation

`UNIQUE (achievement_id, tier)` makes every write idempotent; concurrent evaluations (two tabs,
a poll during a page load) race safely.

Unlocks are permanent, with **one** exception: an unlock less than **24 hours** old whose
evidence rows no longer exist (a shortlist undone, a contact deleted, a rejection dismissed, a
mistaken label removed) is deleted on the next evaluation. After 24 hours it is kept even if
the evidence goes. Status moves are not evidence deletion (events are append-only), so moving a
card back never revokes anything. A rule that merely stops being true (a rhythm run ending) never
revokes.

`catalog_version` is stored on each unlock. A later catalog change that tightens a rule keeps
existing unlocks; one that renames an id ships a migration that renames rows.

## UI

### `/achievements`

Reached from the Today strip and from a notice. Not in the top nav by default
([Open questions](#open-questions)): the nav already has nine entries and wraps at 375 px.
`PAGES` gets `("/achievements", None, "Achievements")`.

```
Achievements                                         Settings ▸
14 earned · Rhythm 3 weeks (best 5) · 1 freeze held

Apply ─────────────────────────────────────────────────────────
[badge] Aimed, not sprayed          ●●○   Tier 2 of 3
        Applications you shortlisted, prepared or saw scored strong before sending.
        ███████░░░  9 of 15                      earned Oct 3 · why ▸
[badge] Tailor-made                 ○○○   locked
        Hint: prepare a packet, mark it ready, then apply with it.
        ░░░░░░░░░░  0 of 1
...
Outcomes ──────────────────────────────────────────────────────
[?]     Hidden achievement          keep going
```

- Sections in category order: Setup, Search, Apply, Follow-up, Outcomes, Rhythm. Within a
  section: earned (newest first), then in progress, then locked.
- Each card: badge, name, one-line plain-words rule, tier pips with a text equivalent
  ("Tier 2 of 3"), a `<progress>` element with a visible "9 of 15", the earned date in local
  time, and **why ▸** (a `<details>`) listing the evidence as links to `/pipeline/{id}` or
  `/job/{group_id}`. Locked cards show a hint that names a healthy next action; hints never
  mention scoring, generating, or sending more applications.
- Hidden achievements show "Hidden achievement" with no hint until earned.
- The rhythm line at the top says "Rhythm: 3 weeks", "Rhythm: paused. Your best run is 5
  weeks", or nothing before the first active week. Never "lost" or "broken".
- Visiting the page marks every unlock seen (`seen_at`).
- `Cache-Control: no-store` like every page; behind the existing loopback and same-origin
  middleware.

**Badge art: inline SVG, not emoji.** One `<symbol>` per category (six) in a sprite partial,
`fill="currentColor"`, so badges follow the `app.css` light and dark tokens. Earned badges use
the accent token; locked ones the muted token with a dashed outline. Tier is shown by ring count
**and** the "Tier n of m" text, never by colour alone. Emoji were rejected: they render
differently per platform and font, cannot follow the theme, and screen readers read their
Unicode names. Each badge is `aria-hidden`; the card's heading carries the name.

**Layout:** CSS grid `repeat(auto-fill, minmax(16rem, 1fr))`, one column at 375 px with no
horizontal scroll. Light and dark through the existing tokens (`app.css:66`, `:123`).

### Settings

A `<details>` "Settings" panel on the board, one form, `POST /achievements/settings`:

| Setting | Default | Effect |
|---|---|---|
| Achievements on | **on** | Off: `evaluate` writes nothing, no notices, no Today strip; `/achievements` shows "Achievements are off" and a turn-on button. Existing unlocks are kept. Turning on again runs a silent backfill |
| Show unlock notices | on | Off: unlocks are written with `seen_at = detected_at` |
| Hide categories | none hidden | Checkboxes per category. Hidden categories are still computed (so un-hiding shows the true history) but get `seen_at` at once, and are left off the board, the strip and the count |

Stored in `achievement_setting`. Not `config.toml` (read-only in the container, 018 D3, and a
restart to toggle is too much) and not `preferences.yaml` (it feeds `filter_version`, and a
setting there would look like a scoring input). The CSRF and origin rules are the existing
middleware's.

### Unlock notice (the activity band)

Reuse `#activity` rather than a new poller. `_activity.html` gets one more item, after the
existing ones:

```
★ Achievement: Follow-through (tier 2)  View · ×        (and "+2 more" when several)
```

- Shown when an unseen unlock exists in a visible category and notices are on. One line, the
  newest; "+N more" links to the board.
- **×** posts `POST /achievements/seen` (marks all unseen as seen) and is server-side, unlike
  the re-score line's `sessionStorage` dismissal, so it does not reappear in another tab.
- The band is already `role="status" aria-live="polite"`, so screen readers announce it once.
- **Prompt feedback:** the console write routes that can unlock something (pipeline event and
  move, "Did you apply?", follow-up done and no-response, next action, contact add, label,
  packet mark ready, rejection add and confirm, alert subscribe) add the response header
  `HX-Trigger: jh-activity`, and `_activity.html`'s `hx-trigger` gains `jh-activity from:body`.
  An htmx action then refreshes the band at once instead of within 60 s. Non-htmx posts
  redirect, and the next page load fetches `/activity` anyway.
- **Motion:** a 150 ms opacity fade only under `prefers-reduced-motion: no-preference`; none
  otherwise (same pattern as `app.css:237`). No confetti, no sound, no badge animation.

### Today strip

One line under the KPI row in `dashboard.html`, outside the range-driven htmx refresh:

```
Achievements 14 · Next: Follow-through 3 of 5 · Rhythm 3 weeks          View all ▸
```

"Next" is the in-progress, visible achievement closest to its next tier, preferring Follow-up
and Apply-targeted over Momentum when tied. Hidden when achievements are off. Wraps to two
lines at 375 px.

## Privacy and money

- `jobhunter/achievements.py` (or `console/achievements.py`) imports nothing from `scoring`,
  `apply.generator`, `apply.score`, `anthropic`, `httpx` or `core.fetch`. A test asserts it.
- No rule reads `llm_spend`, `rescore_request` or `rescore_run`, and no rule reads cost columns
  (`fit_score.cost_usd`, `packet_document.cost_usd`). A test greps the rule SQL.
- Evidence stores ids only. The board shows employer and title by joining at render time, as
  `/pipeline` does; nothing new is stored about you.

## Testing

All under the conftest network guard; no model client is constructed (a fixture makes
`anthropic.Anthropic` raise if instantiated).

- **Per rule, table-driven**: build the minimal rows in a temp database, run `evaluate`, assert
  the tiers and `unlocked_at` and evidence. Include real shapes: email-sourced `applied`
  events, a merge event with status `applied` (not double-counted, not an applied date), an
  application with only automatic early events, a regenerated packet (`ready_at` NULL) that was
  already sent, an application with a typed `resume_version` (not tailored), 6 applications in
  one local day (Momentum counts 5).
- **Time zone**: an application at 01:30 UTC counts on the previous local day with
  `TZ=America/Los_Angeles`; weeks split Monday 00:00 local.
- **Rhythm**: table of week patterns: gap with and without freezes, freeze earned at 4, cap of
  2, current week not counted missed, comeback after 2 empty weeks, a late-accepted
  confirmation repairing a past week.
- **Idempotence**: evaluate twice, same rows; two connections evaluating at once, no error, no
  duplicate.
- **Backfill**: first evaluation writes with `seen_at` set and `/activity` shows nothing; a
  later unlock shows the notice; × hides it in a second client too.
- **Revocation**: shortlist then undo within 24 h removes `search.shortlist`'s evidence-backed
  unlock; the same after 25 h keeps it; moving a card back never revokes.
- **Off**: `enabled=0` writes nothing on any route; strip and notice absent; turning on again
  backfills silently.
- **Hidden category**: absent from board, strip, notice and count; un-hiding shows earned
  unlocks without a notice.
- **Guardrails**: the import and SQL checks above; every hint string is checked against a
  deny list (`score`, `generate`, `more applications`).
- **Write points**: follow-up done, no-response and next-action routes write `followup_log`;
  snooze does not; contact add sets `created_at`; a dedupe merge and a shortlist undo still
  succeed with `followup_log` rows present.
- **Migration** on a copy-shaped database: contacts without dates, existing next actions get
  one `planned` row, nothing else changes; `upgrade` applies it like the others.
- **Links**: request every internal href `/achievements`, the strip and the notice render and
  expect 200 (the lesson from the Sankey link that pointed nowhere).
- **Performance**: the budget above, on a synthetic 10x database, as a non-flaky upper bound.
- **Browser check before merge**, on a copy of your database: light, dark, 375 px,
  reduced motion.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console`; worker and reviewer
per the CLAUDE.md table (the reviewer is always stronger).

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| A1 | Migration: `achievement_unlock`, `achievement_setting`, `followup_log`, `contact.created_at`, the next-action backfill; write points (next action without snooze, done, no-response, contact add); `followup_log` handled in the dedupe merge table loop and the shortlist undo; tests for each, migration on a copy-shaped DB | hard (migration, touches merge and undo) | opus / opus at higher effort | |
| A2 | `achievements` module: definitions, rule functions for the catalog (minus `setup.ready`), local-tz day and week helpers, rhythm with freezes, Momentum day cap, `evaluate` with backfill, revocation, fingerprint; guardrail tests (imports, SQL, hint deny list), the performance test | hard (exact rules on real data shapes) | opus / opus at higher effort | A1 |
| A3 | `/achievements` page: cards, pips, `<progress>`, evidence `<details>`, hidden cards, rhythm line, settings form and route, SVG sprite, CSS (grid, dark, 375 px), marks seen; `PAGES` entry; link test | normal | sonnet / opus | A2 |
| A4 | Notice in `/activity` (fingerprint-gated `evaluate`, one line, "+N more", `POST /achievements/seen`), `HX-Trigger: jh-activity` on the listed write routes, `jh-activity from:body` in the trigger, reduced-motion fade; tests incl. the second-client dismissal | normal | sonnet / opus | A2 |
| A5 | Today strip in `dashboard.html` from A2's summary function; hidden when off; 375 px wrap | mechanical | haiku / sonnet | A2 |
| A6 | `setup.ready` from 019's `essentials_ok` | mechanical | haiku / sonnet | A2, 019 S1 |

Order: A1, then A2, then A3, A4 and A5 together; A6 whenever 019 S1 has landed.

**Estimate**, by the calibrated rule (ticket wall-clock: mechanical 0.75 h, normal 1.4 h, hard
2.1 h; schedule = sum / 3, plus 30%, range 0.7x to 1.6x; floor = the longest dependency chain):

- Sum: 2 hard (4.2 h) + 2 normal (2.8 h) + 2 mechanical (1.5 h) = 8.5 h.
- Schedule: 8.5 / 3 = 2.83 h, plus 30% = 3.7 h, range 2.6 to 5.9 h.
- Floor: A1 then A2 then A3 is 5.6 h, above the estimate, so **it binds: about 5.6 h** of
  continuous run (up to about 9 h at the 1.6x end of the chain).
- About 5.5M subagent tokens (019's rate of about 0.9M per ticket).

**Later, not proposed now:** a "start fresh" button that clears unlocks; contacts not tied to
an application (informational interviews), which needs a UI that does not exist yet; a weekly
summary line on Today ("this week: 2 targeted applications, 3 follow-ups").

## Open questions

1. **Nav link or not?** Recommended default: **no nav entry**; reach the board from the Today
   strip and the notices. Say if you want it in the top nav.
2. **Momentum's cap of 5 applications per local day.** It is the anti-spray guard; your busiest
   day so far had 6, so one application of history does not count toward Momentum (it still
   counts everywhere else). Recommended default: **keep 5**.
3. **Rejection-related achievements** ("Noted, moving on" for logging a confirmed rejection,
   and the hidden "Second wind" for applying again within 14 days). Some people find any
   badge near a rejection grating. Recommended default: **keep both**, worded as record-keeping
   and resilience; hiding Outcomes removes Second wind, and Follow-up's "Noted" can be cut if you
   would rather not see it.

## History

### Revision 1 (2026-10-11)

First draft from your request. Decisions recorded on `4f6bedb`.
