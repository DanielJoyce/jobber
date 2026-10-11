# 021 — Achievements

Status: **draft for review, revision 2a** (bug `4f6bedb`). Written 2026-10-11; revised the same
day after an adversarial review and your answers ([History](#history)).

You asked for "a spec for an achievements board and achievements". A job hunt is long, most of
it gets no reply, and the useful work (a tailored packet, a follow-up, a contact logged) leaves
no visible trace. This spec adds:

- **An achievement catalog** of 32 achievements (54 unlocks counting tiers) in six
  categories. Each has an exact rule over tables jobhunter already keeps, so your existing
  history counts from day one.
- **`/achievements`**, a board in the top nav: what you have earned, progress toward the next
  tier, locked ones with a hint, and a few hidden ones.
- **A small notice** in the existing activity band (PR 113) when one unlocks, and a one-line
  strip on Today.
- **Settings** on the board: turn the whole thing off, turn notices off, hide categories.

It rewards habits that make a search work. It does not reward volume for its own sake, and
nothing in it reads or rewards scoring or anything else that spends credits.

## Decisions

### Your answers (2026-10-11)

| Question | Answer |
|---|---|
| Nav link? | **Yes**: "Achievements" in the top nav (hidden while achievements are off) |
| Daily cap on application counts | **3 per local day** ("feast or famine"), on every family that counts applications you sent |
| Rejection-related achievements | **Keep them, with a positive spin**: resilience, closing the loop, learning. Names and copy never lead with "rejected" |

## Goals and guardrails

| # | Rule | How the design keeps it |
|---|---|---|
| G1 | **Reward effective habits**: targeted applications, tailored packets, follow-ups, contacts, deciding on jobs, a steady weekly routine, rest | Most of the catalog is in Search, Apply (targeted), Follow-up and Rhythm |
| G2 | **Never reward spray-and-pray** | Every family that counts your applications (Momentum, Aimed, Tailor-made, Shortlist) counts **at most 3 per local day** and has a low top tier. Triage counts are capped per day too, so one bulk click is not a volume trophy. "Targeted" needs a deliberate act on an **earlier day** than the send. Weekly achievements are judged only on **finished** weeks |
| G3 | **Never reward anything that spends credits** | No rule reads `fit_score`, `llm_spend`, `rescore_request`, `rescore_run`, cost columns, packet generation counts or "Score it" outcomes. A packet counts once it is **sent**, never per generated version. Hints and card copy never mention scoring or generating. Tests enforce all of it ([Testing](#testing)) |
| G4 | **No punishment** | Unlocks are permanent (one narrow exception: [Revocation](#revocation)). **No daily streaks.** The weekly rhythm has freezes and a "Welcome back" achievement. Copy never says "lost" or "broken" |
| G5 | **Credit only for what you did** | Automated writes never earn anything by themselves: nightly merge events, the mail classifier's rejection rows, bulk triage batches, the migration's own backfill rows ([Definitions](#definitions-used-by-the-rules)) |
| G6 | **Can be turned off**, wholly or by category | [Settings](#settings) |
| G7 | **Nothing leaves the machine** | Pure SQL and Python over the local database. No network, no model call, no share or export button, no telemetry |
| G8 | **No comparison to others** | No percentiles, "rarest", leaderboards or averages. Only your own history |
| G9 | **Outcomes are milestones, not goals** | Interviews and offers depend on employers: one-off milestones, no tier ladder, hideable |

## What exists today (checked in code, 2026-10-11)

The rules read these. Paths are under `src/jobhunter/`.

| Fact | Where |
|---|---|
| `application` (one per job group; `status`, `applied_at`, `resume_version`, `cover_letter_path`, `next_action`, `next_action_at`) | `core/migrations/0001_initial.sql:223` |
| `application_event` (append-only; `at`, `status`, `note`, `source` manual/email/import); **no insertion time**, and pipeline events accept a user-supplied `at` | `0001_initial.sql:240`, `console/tracking_routes.py:190` |
| Sent statuses: `applied`, `acknowledged`, `screening`, `interview`, `offer`. An acknowledgement email or a drag can move an application past `applied` with no `applied` event | `apply/packets.py:39`, `mail/match.py:42-48`, `console/tracking.py:328-330` |
| Automatic early-event notes: `shortlisted from inbox`, `opened apply link`, `packet started`; "Did you apply?" *Not yet* writes `not applied yet` | `console/tracking.py:71-74`, `console/detail.py:588` |
| Bulk triage: up to 500 groups in one transaction with one `labeled_at` and one shortlist event time | `console/inbox.py:310`, `:406-411`, `:315-337` |
| A dedupe merge writes an event with the winner's status, `source = 'manual'`, `at = now`, note `Merged duplicate application (...)`, and deletes the losing application; the group merge re-keys or deletes `label` rows (keyed by `job_group_id`) | `pipeline/dedupe_url.py:232-255`, `:293-307` |
| `contact` (per application; **no timestamp**) | `0001_initial.sql:252` |
| `label` (one row per group, `labeled_at` overwritten on relabel) | `0001_initial.sql:205` |
| `profile_change` (`source` ui/file/revert) | `0001_initial.sql:271` |
| `rejection`: `mail match` writes rows itself, `confirmed` whenever the phrase is specific; confirming a pending row is an `UPDATE` with **no time stored**; a manual row takes a user-entered `received_at` | `0023_rejection.sql`, `0028_rejection_state.sql`, `mail/match.py:776-795`, `core/rejections.py:148-150`, `console/rejections_routes.py:89-101` |
| `application_packet`, `packet_document.origin` (`generated`/`edited`/`base`); "Use base resume" makes an untailored `base` version that can be marked ready | `0029_application_packet.sql`, `apply/generator.py:1153-1170` |
| A sent packet sets `resume_version = 'packet:<id>/resume/v<n>'` **unless you typed one** (then only the `attachment` rows, `packets/<id>/resume-v<n>.pdf`, record it); `resume_version` is free text in the drawer | `apply/packets.py:186`, `:221-225`, `console/tracking.py:400-414` |
| `capture_log` (extension captures; `outcome`) | `0033_capture_log.sql` |
| `alert_signup.subscribed_at`; `mail_message` | `0021_alert_signup.sql`, `0009_mail_alerts.sql:12` |
| Follow-up **Done** clears `next_action`, keeping **no record**; the drawer re-posts the prefilled date on every save; Done and No-response posts carry no item kind; `fu_no_response` commits twice | `console/tracking.py:368-397`, `templates/_drawer.html:16`, `tracking_routes.py:203-211`, `:281-298` |
| `tracking.parse_ts` reads a bare date as UTC midnight (the previous evening in the Americas) | `console/tracking.py:54-57` |
| Shortlist undo deletes an application unless it has contacts, attachments, proposals or packets | `console/inbox.py:351-389` |
| The activity band: `GET /activity`, `outerHTML` swap every 3 s while work runs and 60 s idle, `role="status"`; `app.js` handles any `.activity-dismiss` click by writing `sessionStorage` and emptying the band; `inbox.js` ignores only requests whose path is `/activity` | `console/activity_routes.py`, `templates/_activity.html:2`, `static/app.js:63-75`, `static/inbox.js:155-175` |
| Connections wait up to 5 s for a lock | `core/db.py:185` |
| Nav entries: `PAGES` | `console/app.py:74-87` |
| Theme tokens: `--series-1` and `--text-secondary` exist in all three theme blocks; `--text-muted` only in light; no `color-scheme` | `static/app.css:1-140` |
| The container runs with `Timezone=local` | `specs/018-containers.md:286` |
| `readiness.essentials_ok` is specified, not built | [019](019-setup-and-readiness.md) S1 |
| Migrations end at `0034_score_failure.sql` | `core/migrations/` |

Your history on a copy of the database (counts only, 2026-10-11): 18 sent applications on 10
local days (one day had 6, one had 3), 6 rejection rows all written by `mail match`, 3 triage
labels, 14 prefs saves, 1 job added from the extension; no contacts, packets or next actions.
[Backfill](#backfill) says what that unlocks.

## Definitions used by the rules

- **Local day / week.** Calendar days in the console process's local zone
  (`datetime.astimezone()`), weeks Monday to Sunday. Not the dashboard's UTC day: an
  application sent at 6 pm Pacific would land on tomorrow. A date-only value (a
  `next_action_at` of `2026-10-14`) **is** a local calendar date and is compared as one, never
  through `parse_ts`.
- **User events.** Every event rule reads one shared query: `application_event` minus rows
  whose note starts with `Merged duplicate application`. Merge events never count, for any
  status.
- **Sent date** of an application: the earliest user event with a status in `SENT_STATUSES`
  (`applied`, `acknowledged`, `screening`, `interview`, `offer`). An application counts once.
- **Daily cap.** A family that counts your applications counts at most **3 per local day**
  of the date that family uses (sent date, or shortlist date for Shortlist). The fourth and
  later that day still exist; they just do not move that family.
- **Deliberate early event**: an `interested` or `preparing` user event that is either
  `shortlisted from inbox` from a **single** press (fewer than 3 shortlist events share its
  exact `at`; a bulk batch shares one), `packet started`, or a manual move (`source = 'manual'`,
  note not in the automatic notes and not `not applied yet`).
- **Sent packet** of an application: a packet of **that application** whose sent resume
  version has `origin IN ('generated', 'edited')`. The version is found from `resume_version`
  when it parses as `packet:<id>/resume/v<n>` with `<id>` belonging to the application, else
  from an `attachment` row of kind `resume` at `packets/<id>/resume-v<n>.pdf` for such a
  packet. A typed `packet:` string that resolves to no such packet counts for nothing; a
  `base` version (your resume unchanged) is not tailoring.
- **Targeted** application: a sent application with a sent packet, or with a deliberate early
  event on a local day **before** its sent date.
- **Tailored** application: a sent application with a sent packet.
- **Response**: a user event with status `screening`, `interview` or `offer`.
- **Recorded time** of a manual event: the new `application_event.recorded_at` (when the row
  was written) if set, else `at`. Rhythm uses it, so backdating an event does not fill a past
  week; momentum and the other send-date rules keep the real `at` (the day you applied).
- **Active day**: a local day with at least one deliberate action:
  a sent date of an email-sourced send (its `at` is the confirmation's time, which is when you
  applied); the recorded time of a manual user event in a sent status or `withdrawn` or
  `no_response`; a `label.labeled_at` (a bulk batch is one action); a `followup_log` row that is
  not a migration backfill; a `contact.created_at`; a packet `ready_at`; a manual `rejection`'s
  `created_at` or any `rejection.confirmed_at`; `alert_signup.subscribed_at`; a `capture_log`
  row with outcome `added`, `linked`, `description_added` or `linked_groups`; a
  `profile_change` with `source = 'ui'`. Never: `opened apply link`, `packet started`, merge
  events, anything written by the scorer, the nightly run or `mail match` alone.
- **Active week**: a **finished** week with **2 or more** active days. **Missed week**: any
  finished week that is not active (a 1-day week is missed). The current week is "in progress"
  and never counts either way until Monday 00:00 local.
- **Rest day**: a day of a finished active week with no active action.

## The catalog

32 achievements, 54 unlocks. A **family** has tiers (each tier is its own unlock, shown as one
card with pips); the others are single. **Hidden** ones show as "Hidden achievement" with their
category until earned. **Lost?** No, everywhere, except the 24-hour [revocation](#revocation).
**Backfill** is what your current history unlocks on the first evaluation, silently.

### Setup (4)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `setup.prefs` | Tuned in | First `profile_change` with `source = 'ui'` (a save from `/prefs`; saving is free, a re-score is a separate action and not rewarded) | Yes |
| `setup.alerts` | Net cast, tiers **1 / 5** | `alert_signup` rows with `subscribed_at IS NOT NULL` | No |
| `setup.mail` | Inbox sorted | You ran `jobhunter mail setup` successfully (not `--dry-run`): it records `mail_setup_at` in `achievement_setting` (A1). The automated fetch of alert emails does not count | No (no record before A1) |
| `setup.ready` | All set | 019's `essentials_ok`. **Added by A6 once 019 S1 lands** | n/a |

### Search (4)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `search.decide` | Decisive, tiers **10 / 50 / 150 / 300** | `label` rows, counting at most **20 per local day** of `labeled_at`. Saying no is a decision too | No |
| `search.no` | Clear no | 10 `not_interesting` labels, at most **5 per local day** | No |
| `search.shortlist` | Shortlist | 5 applications with a deliberate early event, at most 3 per local day | No |
| `search.found` | Found it yourself | First `capture_log` row with outcome `added`, `linked` or `description_added` | Yes |

### Apply (7)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `apply.first` | First one out | First sent application | Yes |
| `apply.momentum` | Momentum, tiers **5 / 10 / 25 / 50** | Sent applications, capped 3 per local day of sent date | **5 and 10** (15 counted of 18) |
| `apply.targeted` | Aimed, not sprayed, tiers **1 / 5 / 15** | Targeted applications, capped 3 per day | No |
| `apply.tailored` | Tailor-made, tiers **1 / 5 / 10** | Tailored applications, capped 3 per day | No |
| `apply.own_words` | In your own words | A tailored application whose sent resume version has `origin = 'edited'` | No |
| `apply.cover` | Dear hiring manager | A tailored application whose packet's `cover_doc_id` is set and whose `cover_letter_path` or a `cover_letter` attachment records it | No |
| `apply.quality_week` | Quality week | A **finished** week with **2 to 5** sent applications, **all** targeted | No |

### Follow-up (7)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `follow.plan` | Next step set, tiers **1 / 5 / 15** | Distinct `(application_id, due_at)` pairs among `followup_log` `planned` rows, capped at **3 per local day** of `at` and **1 per application per local day** (re-dating the same application over and over is not planning) | No |
| `follow.done` | Follow-through, tiers **1 / 5 / 15 / 30** | Distinct `(application_id, due_at)` pairs among `done` rows, plus `no_response` rows, with the same caps (3 per local day, 1 per application per day) | No |
| `follow.on_time` | On time | 5 `done` rows (after the same caps) each with an earlier `planned` row for the same pair and a local day on or before `due_at` (as a local date) | No |
| `follow.tidy` | Tidy pipeline | 5 applications with a manual `no_response` user event (closing stale ones keeps the pipeline honest) | No |
| `follow.contact` | Networker, tiers **1 / 5 / 15** | `contact` rows (rows from before the migration count, dated at migration, but are not active days) | No |
| `follow.loop` | Closing the loop | The first time **you** record an employer's answer: a manual `rejection` (by `created_at`), a confirmed `rejection.confirmed_at` (you pressed confirm), or a user event `rejected` with `source = 'manual'` or from an accepted proposal. Copy: "You recorded how an application ended. Knowing where you stand frees you to move on." | No (all 6 rows came from the classifier) |
| `follow.lessons` | Lessons kept | 3 applications with a manual user event in `rejected`, `no_response` or `withdrawn` that carries a note you wrote (not an automatic or route-generated note). Copy: "Three times you wrote down what you learned from an ending." | No |

### Outcomes (7, hideable)

Employer-driven, so no daily cap: you cannot spray responses.

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `out.heard_back` | Heard back | First response | No |
| `out.conversations` | Conversations, tiers **3 / 10** | Applications with a response | No |
| `out.interview` | First interview | First `interview` user event | No |
| `out.interviews` | Interview circuit | 3 applications with an `interview` user event | No |
| `out.offer` | Offer | First `offer` user event | No |
| `out.long_game` | The long game (**hidden**) | A response 30 or more days after that application's sent date | No |
| `out.bounce_back` | Bounce back (**hidden**) | You sent an application within 14 days after an employer said no (a `rejection` in `confirmed` state, by `received_at`, or a `rejected` user event). Copy: "A no, and you kept going within two weeks. That is what resilience looks like." | **Yes** (5 of 6) |

### Rhythm (3)

| ID | Name | Rule (exact) | Backfill |
|---|---|---|---|
| `rhythm.weeks` | Steady rhythm, tiers **2 / 4 / 8 / 12** | Longest run of consecutive active weeks, freezes applied | **Tier 1** (weeks of Sep 21 and Sep 28) |
| `rhythm.rest` | Rest is part of it | 3 active weeks that each had **2 or more** rest days | Not yet; **yes on Monday** when this week ends |
| `rhythm.comeback` | Welcome back | An active week after **2 or more** missed weeks, **and** after an earlier active week (a first-ever week is a start, not a comeback) | No |

**Freezes.** You hold up to **2**; you earn one for every 4 consecutive active weeks in a run.
A missed week, when you hold a freeze, uses one and the run continues (it does not add to the
length). Freezes are recomputed from history each time, so they need no storage, and a
confirmation accepted late can repair a past week. When a run ends the board says "Rhythm:
paused. Your best run is 6 weeks." Earned tiers stay.

**Deliberately absent:** applications per day, a daily streak, "apply to N this week", jobs
scored, re-scores, packets generated, time spent, logins, anything about other people.

## Data

New migration **`0035_achievements.sql`** (the next free number at implementation time).
All additive.

```sql
CREATE TABLE achievement_unlock (
  id              INTEGER PRIMARY KEY,
  achievement_id  TEXT NOT NULL,              -- 'apply.momentum'
  tier            INTEGER NOT NULL DEFAULT 1,  -- 1-based into the family's thresholds
  unlocked_at     TEXT NOT NULL,              -- the crossing date (display)
  detected_at     TEXT NOT NULL,              -- when evaluate() wrote it (revocation clock)
  evidence        TEXT NOT NULL,              -- JSON {"application": [12, 15]}: display only
  catalog_version INTEGER NOT NULL,
  seen_at         TEXT,                       -- NULL: notice not yet shown or dismissed
  UNIQUE (achievement_id, tier)
);
CREATE INDEX achievement_unlock_unseen ON achievement_unlock(seen_at) WHERE seen_at IS NULL;

CREATE TABLE followup_log (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  at             TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('planned', 'done', 'no_response')),
  due_at         TEXT,                         -- the next_action_at in force (date or time)
  backfill       INTEGER NOT NULL DEFAULT 0 CHECK (backfill IN (0, 1))
);
CREATE INDEX followup_log_app ON followup_log(application_id);

CREATE TABLE achievement_setting (            -- enabled, notify, hidden_categories, backfilled_at
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE contact ADD COLUMN created_at TEXT;            -- NULL: predates this migration
ALTER TABLE application_event ADD COLUMN recorded_at TEXT; -- NULL: predates this migration
ALTER TABLE rejection ADD COLUMN confirmed_at TEXT;        -- set when you confirm a pending row

INSERT INTO followup_log (application_id, at, kind, due_at, backfill)
  SELECT id, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), 'planned', next_action_at, 1
  FROM application WHERE next_action_at IS NOT NULL AND next_action_at != '';
```

**Write points** (each inside the transaction of the change it records):

| Action | Writes | Code |
|---|---|---|
| Any event insert | `recorded_at = now` at every insert site: `tracking.add_event`, `detail.answer_prompt`, the apply-click event, the packet-start event, the inbox shortlist, the merge. A1 greps for `INSERT INTO application_event` and a test fails on any site that omits it | `console/tracking.py:131`, `console/detail.py:537`, `console/detail.py:480`, `apply/packets.py:90`, `console/inbox.py:334`, `pipeline/dedupe_url.py:301` |
| Next action: `next_action_at` **changes to a new non-empty value** | `planned`, `due_at` = the new value. Re-saving the same date, editing only the text, or a snooze (which moves a date you did not choose) writes nothing; snooze passes a flag | `console/tracking.py:368`, `:392` |
| Follow-up **Done**, when a next action with a date existed | `done`, `due_at` = that date. Done on a nudge with no next action writes nothing | `tracking_routes.py:281` |
| **No response** from follow-ups | `no_response`, in **one** transaction with the event (fixes today's double commit) | `tracking_routes.py:293-298` |
| Add a contact | `contact.created_at = now` | `console/tracking.py:432` |
| Confirm a rejection | `confirmed_at = now`, both when you press confirm on `/rejections` and when you accept a rejection proposal (which confirms the row too) | `core/rejections.py:148`, `console/proposals.py:153` |
| `jobhunter mail setup` succeeds (not `--dry-run`) | `achievement_setting` `mail_setup_at = now` | `mail/setup.py:79` via the CLI command |
| Application merge | `followup_log` added to the child-table loop | `pipeline/dedupe_url.py:293` |
| Shortlist undo | `followup_log` added to the `others` check, so an application you planned a follow-up on is kept, like one with a contact | `console/inbox.py:370-381` |

`evidence` holds table names and row ids only, for the "why" list. Ids that no longer exist
show as "(record since merged or removed)". Evidence is never used to decide anything.

### How unlocks are computed

**On read, gated by a change check.** Not nightly (a day late is no reward) and not hooks in
every write path (the CLI's `mail match` alone would be missed). One function:

```
achievements.evaluate(conn, now, tz) -> list[Unlock]     # the newly written ones
```

1. If `enabled` is off, return `[]`. No write.
2. **Read phase**, no transaction: compute every rule from scratch (each rule is a Python
   function over a few SQL queries returning, per tier, the crossing date and evidence ids) and
   diff against the stored rows: tiers to insert, tiers to revoke, evidence to refresh.
3. **Nothing to change: return without writing.** This is the common case and takes no lock.
4. **Write phase**: set `PRAGMA busy_timeout = 250` for this statement sequence, `BEGIN
   IMMEDIATE`, then **inside the transaction** re-read `backfilled_at` and the stored rows and
   recompute (cheap, and closes the race with another tab or an undo committed in between).
   Insert with `ON CONFLICT (achievement_id, tier) DO NOTHING RETURNING id`, revoke, refresh
   evidence, and, if `backfilled_at` was empty, write every row with `seen_at = detected_at` and
   set `backfilled_at`. Commit, restore the connection's timeout.
5. A `sqlite3.Error` anywhere (a pipeline run holding the write lock, a busy database) is
   caught by the **caller**, which renders from the stored unlocks. The next call retries.
   Every caller (`/`, `/achievements`, `/activity`) does this; none returns 500 for it.

**When it runs:**

- `/activity`, the Today strip and `GET /achievements` call it only when the **fingerprint**
  changed. `/achievements` also calls it on a full page load regardless, since it is rare.
- The fingerprint is one `SELECT` kept **in process** (`app.state`, never written to the
  database): `MAX(id)` and `COUNT(*)` of `application_event`, `contact`, `followup_log`,
  `attachment`, `capture_log`, `profile_change`; `COUNT(*)` and `MAX(labeled_at)` of `label`;
  `MAX(id)`, `COUNT(*)`, confirmed count and `MAX(confirmed_at)` of `rejection`;
  `MAX(updated_at)` of `application_packet` and `alert_signup`; `EXISTS` on `mail_message`
  (only "any row" matters, so the mail phase of a run does not churn it); plus the local date
  and the start of the current week. After a restart the first call evaluates.
- CLI writes (`mail match`, a proposal accepted in the terminal) show up through the
  fingerprint on the next poll.

`/activity`'s docstring (`activity_routes.py:1-8`) is updated to say it may write unlocks.

**Performance budget:** read phase under 50 ms and the fingerprint under 2 ms on a copy of your
database; read phase under 250 ms on a synthetic database 10 times larger (200 applications,
3,000 labels, 2,000 events). No rule reads `job`, `job_fts` or `fit_score`.

### Backfill

The first write phase after the migration, or after turning achievements back on, writes every
unlock your history supports with `unlocked_at` at the real crossing date (contacts from before
the migration use the migration time) and `seen_at = detected_at`, so nothing pops up. Because
`backfilled_at` is read inside the write transaction, a second tab racing it inserts nothing new.
The board shows once: "7 achievements from your history so far" (the real count).

On your copy that is **7**: Tuned in, Found it yourself, First one out, Momentum 5 and 10,
Bounce back, and Steady rhythm tier 1 (the weeks of Sep 21 and Sep 28 each had 2 or more active
days; the week of Sep 14 had one). This week (Oct 5 to 11) has 4 active days and ends tonight:
on Monday the run becomes 3 weeks and Rest is part of it unlocks, with a notice. The next steps
the board points at are Aimed, not sprayed, Next step set, Networker and Closing the loop.

Later unlocks always notify, even when the crossing date is old (a confirmation accepted today
for an application sent last week is still news to you today).

### Revocation

Unlocks are permanent, with **one** exception: within **24 hours of `detected_at`**, a tier
(and every tier above it) is revoked only when the **fresh computation no longer reaches it**:
you undid the shortlist, deleted the contact, or removed the label that crossed it. If the tier
still holds (one of six shortlisted undone, a nightly merge that deleted an evidence row), the
row stays, its `evidence` is refreshed in place and `seen_at` is untouched, so no second notice.
Whether evidence rows exist is never checked. After 24 hours nothing is revoked. A rule that
merely stops being current (a rhythm run ending) never revokes.

`catalog_version` is stored on each unlock. A later catalog change that tightens a rule keeps
existing unlocks; one that renames an id ships a migration that renames rows.

## UI

### `/achievements`

In the top nav: `PAGES` gets `("/achievements", "Achievements", "Achievements")`, and
`base.html` skips that entry while achievements are off (a Jinja global reading a setting cached
in `app.state`, refreshed by the settings route).

```
Achievements                                         Settings ▸
14 earned · Rhythm 3 weeks (best 5) · 1 freeze held · this week: in progress

Apply ─────────────────────────────────────────────────────────
[badge] Aimed, not sprayed          ●●○   Tier 2 of 3
        Applications you shortlisted or prepared a packet for on an earlier day.
        ███████░░░  9 of 15                      earned Oct 3 · why ▸
[badge] Tailor-made                 ○○○   locked
        Hint: prepare a packet, review it, mark it ready, then apply with it.
        ░░░░░░░░░░  0 of 1
...
Outcomes ──────────────────────────────────────────────────────
[?]     Hidden achievement          keep going
```

- Sections in category order: Setup, Search, Apply, Follow-up, Outcomes, Rhythm. Within one:
  earned (newest first), then in progress, then locked.
- Each card: badge, name, one-line plain-words rule, tier pips with "Tier n of m" text, a
  `<progress>` with a visible "9 of 15", the earned date in local time, and **why ▸**
  (`<details>`) linking the evidence to `/pipeline/{id}` or `/job/{group_id}`. Locked cards show a
  hint naming a healthy next action. Hints and descriptions never mention scoring, generating
  or sending more applications.
- Weekly cards say "judged when the week ends" while the current week is in progress.
- Hidden achievements show "Hidden achievement" with no hint until earned.
- The rhythm line reads "Rhythm: 3 weeks", "Rhythm: paused. Your best run is 5 weeks", or
  nothing before the first active week.
- Visiting the page marks the unlocks it rendered as seen, and only when one of them is unseen (so an ordinary visit writes nothing). That write uses the same 250 ms busy timeout and the same fallback: on a busy database the page still renders and the notice stays until the next visit.
- `Cache-Control: no-store`; behind the existing loopback and same-origin middleware.

**Badge art: inline SVG, not emoji.** One `<symbol>` per category in a sprite partial,
`fill="currentColor"`. Earned badges use `--series-1`; locked ones `--text-secondary` with a
dashed outline (`--text-muted` is not defined in the dark blocks). Tier shows as ring count
**and** text, never colour alone. Emoji were rejected: they render differently per platform,
cannot follow the theme, and screen readers read their Unicode names. Badges are `aria-hidden`.

**Progress bars** are styled, not native: `appearance: none`, track `--grid`, fill `--series-1`
through `::-webkit-progress-bar`, `::-webkit-progress-value` and `::-moz-progress-bar`, so they
follow light and dark. Grid `repeat(auto-fill, minmax(16rem, 1fr))`: one column at 375 px, no
horizontal scroll.

### Settings

A `<details>` "Settings" panel on the board, `POST /achievements/settings`:

| Setting | Default | Effect |
|---|---|---|
| Achievements on | **on** | Off: `evaluate` writes nothing; no nav entry, notice or Today strip; `/achievements` shows "Achievements are off" and a turn-on button. Unlocks are kept. Turning on runs a silent backfill |
| Show unlock notices | on | Off: unlocks are written with `seen_at = detected_at` |
| Hide categories | none | Hidden categories are still computed (un-hiding shows the true history) but get `seen_at` at once and are left off the board, the strip and the count |

Stored in `achievement_setting`: not `config.toml` (read-only in the container, 018 D3) and not
`preferences.yaml` (it feeds `filter_version`).

### Unlock notice (the activity band)

`_activity.html` gets one more item after the existing ones:

```
★ Achievement: Follow-through (tier 2)  View · ×        (and "+2 more" when several)
```

- Shown when an unseen unlock exists in a visible category and notices are on. The newest one,
  with `data-unlock="<id>"`; "+N more" and View link to `/achievements`.
- **×** uses its own class `ach-dismiss` (not `.activity-dismiss`, whose `app.js` handler would
  overwrite the re-score dismissal and empty the band). It is `hx-post="/achievements/seen"`
  with the **ids it shows** (`hx-vals`), `hx-swap="none"`; the response carries
  `HX-Trigger: jh-activity`, so the band refreshes through `/activity`, which `inbox.js`
  already ignores. Unlocks that arrived after the band rendered stay unseen. Seen is
  server-side, so it does not reappear in another tab.
- **Announcement once:** the notice span has `aria-live="off"`, so the band's own live region
  does not re-read it on every 3 s or 60 s swap. `app.js` copies its text into a persistent,
  visually hidden `#ach-announce` (`role="status"`, in `base.html`, outside `#activity`) only
  when `data-unlock` differs from the last id it announced (kept in `sessionStorage`).
- **No animation.** A fade would replay on every `outerHTML` swap. No confetti, no sound.
- **Prompt feedback:** console write routes that can unlock something (pipeline event and move,
  "Did you apply?", follow-up done and no-response, next action, contact add, label, packet
  mark ready, rejection add and confirm, alert subscribe) add `HX-Trigger: jh-activity`, and
  the band's `hx-trigger` gains `jh-activity from:body`. The fingerprint changes with each of
  these writes (rejection confirm through the confirmed count), so the refresh evaluates.

### Today strip

One line under the KPI row in `dashboard.html`, outside the range-driven refresh, gated by the
fingerprint like the band:

```
Achievements 14 · Next: Follow-through 3 of 5 · Rhythm 3 weeks          View all ▸
```

"Next" is the visible in-progress achievement closest to its next tier, preferring Follow-up
and Aimed over Momentum on a tie. Hidden when off. Wraps at 375 px.

## Privacy and money

- The `achievements` module imports nothing from `scoring`, `apply.generator`, `apply.score`,
  `anthropic`, `httpx` or `core.fetch`. A test asserts it.
- Rule SQL never names `fit_score`, `llm_spend`, `rescore_request`, `rescore_run` or a cost
  column (`cost_usd`, `api_equiv_usd`). A test greps the rule SQL for each.
- Evidence stores ids only; employer and title are joined at render time, as `/pipeline` does.

## Testing

All under the conftest network guard, with a fixture that makes `anthropic.Anthropic` raise
if constructed.

- **Per rule, table-driven**, on real shapes: email-sourced sends; an application that went
  straight to `acknowledged` (sent, counts); a merge event at `applied` and at `interview` 40
  days after sending (counts for nothing, no long game); only automatic early events; *Not yet*
  then *Yes* (not targeted); a shortlist and a send on the same day (not targeted); a bulk
  shortlist of 20 then 20 sends over a week (**0** targeted: a bulk batch is not deliberate); 6 sends in one day
  (Momentum counts 3); a typed `packet:9/resume/v1` with no such packet (not tailored); a typed
  resume version with the packet attachment present (tailored); a `base` version sent (not
  tailored); a `fit_score` strong row alone (not targeted).
- **Follow-up:** set a date, save the text only, save the same date (one `planned`); set,
  Done, set the same date again, Done (one pair); Done on the due day with
  `TZ=America/Los_Angeles` and a date-only due (on time); snooze writes nothing; No-response is
  one transaction.
- **Classifier and bulk:** classifier-confirmed rejections alone unlock no Closing the loop;
  confirming a pending one does, and the next `/activity` shows the notice; a 300-row bulk
  dismiss moves Decisive by 20.
- **Time zone:** an application at 01:30 UTC counts on the previous local day; weeks split at
  Monday 00:00 local.
- **Weeks:** a week good on Tuesday that ends with 6 untargeted sends gives no Quality week; a
  current week with 2 active days gives no Rest and no rhythm credit until it ends; a 1-day
  week is missed; freezes earned at 4, capped at 2; comeback needs an earlier active week; a
  backdated manual event does not fill a past week; migration-backfill `planned` rows make no
  active day.
- **Evaluate:** a no-change call performs no write (assert with a read-only connection); a
  second connection holding `BEGIN IMMEDIATE` makes `GET /`, `/achievements` and `/activity`
  return 200 from stored rows within about 300 ms; two first-time evaluations racing give 7
  silent rows and no notice; twice in a row, same rows.
- **Revocation:** undo 1 of 6 shortlisted within 24 h (no revoke, no second notice); undo the
  only one (revoked); a nightly merge deleting an evidenced application under 24 h (no revoke);
  after 25 h nothing revokes.
- **Notice:** × marks only the shown ids; a later unlock still shows; × does not touch the
  re-score line's `sessionStorage`; `/inbox`'s pending selection survives a × on the inbox
  page; announcement happens once across several polls.
- **Off and hidden:** off writes nothing anywhere and drops the nav entry, strip and notice; on
  again backfills silently; a hidden category is absent from board, strip, notice and count.
- **Guardrails:** the import and SQL greps; every hint **and description** string checked
  against a deny list (`score`, `generate`, `more applications`).
- **Write points and migration:** each table row above; a dedupe merge and a shortlist undo
  succeed with `followup_log` rows; the migration on a copy-shaped database (contacts without
  dates, existing next actions get one backfill row); `upgrade` applies it.
- **Links:** every internal href the board, strip and notice render returns 200.
- **Performance:** the budget above on the synthetic 10x database.
- **Browser check before merge**, on a copy of your database: light, dark (progress bars
  included), 375 px, reduced motion, a screen reader pass over the notice.

## Proposed tickets

Not filed yet; the PM files them after you approve. Labels `area:console`; worker and reviewer
per the CLAUDE.md table (the reviewer is always stronger).

| # | Ticket | Difficulty | Worker / reviewer | Depends on |
|---|---|---|---|---|
| A1 | Migration `0035`: the three tables, three columns, follow-up backfill; every write point in the table (including every `application_event` insert site, `confirmed_at` on `/rejections` confirm and on accepting a rejection proposal, `mail_setup_at` from `mail setup`, the no-response single transaction, the merge loop and the undo check); tests for each; migration on a copy-shaped DB | hard (migration; touches merge and undo) | opus / opus at higher effort | |
| A2 | `achievements` module: definitions, catalog rules (minus `setup.ready`), local-tz helpers, daily caps, rhythm with freezes, `evaluate` (read phase, no-change fast path, guarded write phase, silent backfill, revocation), in-process fingerprint, settings read; **plus** the `PAGES` entry with the nav filter, a stub `GET /achievements`, `POST /achievements/seen` and `POST /achievements/settings`, so A3 to A5 can run in parallel and their links resolve; guardrail and performance tests | hard (exact rules on real data, write-lock handling) | opus / opus at higher effort | A1 |
| A3 | Board page: cards, pips, styled progress, evidence, hidden cards, weekly "in progress", rhythm line, settings panel, SVG sprite, CSS (dark, 375 px); link test | normal | sonnet / opus | A2 |
| A4 | Notice: `/activity` item with fingerprint-gated `evaluate` and error fallback, `ach-dismiss` with shown ids, `#ach-announce` and its `app.js`, `HX-Trigger: jh-activity` on the listed routes and in the band trigger; tests including the PR 113 interplay | hard (a polled GET that writes, lock contention, a dozen routes) | opus / opus at higher effort | A2 |
| A5 | Today strip, fingerprint-gated, error fallback, "Next" selection | normal (a query, a gated evaluate and a fallback path, not a template tweak) | sonnet / opus | A2 |
| A6 | `setup.ready` from 019's `essentials_ok` | mechanical | haiku / sonnet | A2, 019 S1 |

Order: A1, then A2, then A3, A4 and A5 together; A6 once 019 S1 has landed.

**Estimate**, by the calibrated rule (ticket wall-clock: mechanical 0.75 h, normal 1.4 h, hard
2.1 h; schedule = sum / 3, plus 30%, range 0.7x to 1.6x; floor = the longest dependency chain):

- Sum: 3 hard (6.3 h) + 2 normal (2.8 h) + 1 mechanical (0.75 h) = 9.85 h.
- Schedule: 9.85 / 3 = 3.28 h, plus 30% = 4.3 h, range 3.0 to 6.8 h.
- Floor: A1, A2, A4 is 6.3 h, above the estimate, so **it binds: about 6.3 h** of continuous
  run, up to about 10 h at 1.6x.
- Tokens by the calibrated per-difficulty rule (hard 2.0M, normal 0.6M, mechanical 0.5M): 6.0M + 1.2M + 0.5M = **about 7.7M** subagent tokens.

**Later, not proposed now:** a "start fresh" button; contacts not tied to an application
(informational interviews), which needs a UI that does not exist yet; a weekly summary line on
Today.

## Open questions

None. The nav link, the daily cap and the rejection-related achievements were answered on
2026-10-11.

## History

### Revision 2a: final check

One blocker: the token estimate used a flat 0.9M per ticket; it now uses the per-difficulty rule
(7.7M). Also: every `application_event` insert site sets `recorded_at` (apply click and packet
start added); accepting a rejection proposal sets `confirmed_at`; `/achievements` writes seen
only when something is unseen, with the busy fallback; the bulk-shortlist test expects 0
targeted; A5 is normal; Inbox sorted needs your `mail setup`, not the automated fetch; the
follow-up families are capped per day and per application.

### Revision 2: adversarial review and your answers

23 verified findings, 6 blocking, all addressed:

- **Blocking.** Targeted no longer reads `fit_score` (a paid "Score it" or a nightly score could
  stand in for preparation), and the SQL grep forbids it. Weekly achievements are judged only
  on finished weeks. The sent date covers applications that skip the `applied` event. Tailored
  needs a real packet of that application with a generated or edited resume, found through the
  ref or the attachment, never a typed string or a `base` version. `evaluate` writes only when
  something changed, recomputes inside its write transaction, waits at most 250 ms, keeps the
  fingerprint in process, and every caller falls back to stored rows. The page, nav and `seen`
  route moved into A2, A4 is rated hard, and the floor is 6.3 h.
- **Automated actions earn nothing:** merge events are excluded from every rule; classifier
  rejections no longer unlock anything by themselves (`confirmed_at` added); bulk triage is
  capped per day and a bulk shortlist is not deliberate; migration backfill rows and backdated
  events make no active day (`recorded_at` added).
- **Caps:** every family counting your applications is capped at 3 per local day (your
  answer); triage counts are capped too.
- **Follow-ups:** `planned` only on a new date, `done` only when a dated action existed,
  counted as distinct pairs; date-only due dates compared as local dates; `item_kind` dropped;
  no-response in one transaction; undo keeps an application with follow-up history.
- **Revocation** decides by recomputation within 24 h of `detected_at`, never by evidence
  existence, so merges and partial undos cause no churn or repeat notices.
- **Band:** its own dismiss class, posts the shown ids, refreshes through `/activity`, announces
  once through a separate live region, no fade.
- **Theme:** real tokens (`--series-1`, `--text-secondary`), styled progress bars.
- **Your answers:** a nav link; a cap of 3 per day; rejection-related achievements kept and
  renamed: Closing the loop, Lessons kept (new) and Bounce back.

### Revision 1 (2026-10-11)

First draft from your request.
