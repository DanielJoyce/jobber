# 017 — Assisted apply: targeted resume, cover letter, resume-first fill

Status: **design, revision 4, awaiting your approval.** Bug `7fa29db`. Proposed as a new
milestone M10 ([Deviations](#deviations-from-earlier-specs)). No code yet. Revisions 2 to 4
and why are at the end ([History](#history)).

You asked: *"Is there any way to automate the application part of the process? Filling out web
forms?"* Then you added that most sites now parse an uploaded resume and fill themselves, that
your existing fill helpers already handle forms and skip salary and EEO questions, and that
targeted resume and cover letter tools would help too. So this spec is mostly about the
**documents** and the questions your helpers cannot answer, with form help as a later, smaller
step:

| Phase | What you get | Works on |
|---|---|---|
| **1. Packet** | For any posting (a jobhunter job, a pasted URL, or pasted text): a **targeted resume** built only from facts in your resume, with the source line shown beside every line; a **cover letter**; checked drafts for custom questions; saved custom answers you can reuse; checklists; export | Every posting, with no browser automation at all |
| **2. Resume-first fill** (gated) | In a dedicated Chrome profile, the helper snapshots the form, you attach your resume, the site fills itself, and jobhunter's own code says what the parser filled, what differs from your resume, and which custom questions have drafts. It **stops before Submit** | Public no-account ATS forms (Ashby, Greenhouse, Lever, Workable) first. Built only if the [gate](#phase-2-gate) passes |

**jobhunter never submits an application**, in any phase, behind any flag. You read the form and
press Submit. The confirmation email or the existing "Did you apply?" prompt then moves the job
to `applied` ([007](007-console-and-tracking.md#optional-gmail-matching)).

### Where the jobs you apply to are

Checked against the local database on 2026-10-09: 2,127 job groups (USAJOBS 1,088, NLx 1,065,
the rest state banks); 5 `apply_link` rows, none on the four ATSs; 20 applications, 18 of them
`email-manual` groups made from confirmation emails. Most of your applications happen outside
jobhunter (007), and the 60-day mail scan shows their confirmations come from Ashby, Greenhouse,
Lever, Workable and Workday. Two consequences:

1. **A packet must start from a pasted posting**, not only from an ingested job
   ([New packet](#new-packet-from-a-url-or-text)).
2. **Form filling waits for evidence** that the resume parse plus your fill helpers leave enough
   work behind to be worth it.

## Goals

1. **Better applications in less time.** A targeted resume and a cover letter in a few minutes
   of review instead of 20-40 minutes of rewriting.
2. **Tailor without inventing.** The variant, the letter and the drafts only select, reorder and
   rephrase facts in your resume, plus employer facts quoted from the posting or written by you.
   A deterministic checker flags mechanical fabrication; every generated line shows the line it
   cites so you can judge the rest ([no-fabrication rule](#no-fabrication-rule)).
3. **Help with the remainder** your helpers and the resume parse leave: custom questions, and
   checking the parsed work history.
4. **Close the loop** without updating the pipeline by hand, whichever way the application is
   recorded.

## Non-goals

| Not doing | Why |
|---|---|
| **Submitting an application. Ever.** | See below |
| **Storing, drafting or filling anything on the [never-store list](#never-store-list)** (salary, EEO and self-ID, citizenship, address, pronouns, DOB, SSN, consent, certification, signature) | You answer these by hand; your fill helpers skip them too. Enforced in code at every write, draft and fill, not only stated here |
| A separate answer store (the revision 2 `answers.yaml` bank, its own page or deny rule) | Answers live in [Application answers](#application-answers) on `/prefs`, in the preferences file |
| Passwords, security questions, payment details, criminal history, references' contacts | Never stored, never filled, always yours |
| Solving, bypassing or "humanizing" past a CAPTCHA, email code or bot check | [008](008-compliance.md#what-we-will-not-build). A challenge means stop and hand back |
| Creating ATS accounts, logging in, storing credentials | [008](008-compliance.md#what-we-will-not-build) |
| Batch, queued or scheduled filling | One job, one tab, one review. A mass-apply bot is not a personal assistant |
| Writing a resume from scratch, or claims not in your resume or notes | The no-fabrication rule |
| Suggesting a level on a federal self-assessment question | The level is your attestation; overrated ones are what staffing reviews check |

**Why never auto-submit.** You are attesting that the application is true and complete (federal
ones formally). A wrong field submitted automatically usually cannot be withdrawn. ATSs treat
automated submission as spam (Ashby's "Application Spam Protection"; Greenhouse's invisible
reCAPTCHA). And [001](001-goals-and-scope.md#non-goals) and
[008](008-compliance.md#what-we-will-not-build) already rule it out.

### Never-store list

Data in `apply/labels.py` (`NEVER_STORE`), matched against the normalized question label (and,
in phase 2, the field `name`/`id`) **before anything else** at every entry point:

| Category | Label patterns (examples; the table grows like `ats_rules.py`) |
|---|---|
| Pay | salary, compensation, pay expectation, desired pay, rate, current pay |
| EEO and self-ID | gender, sex, race, ethnicity, hispanic, veteran, disability, sexual orientation, transgender |
| Identity | citizenship, citizen, national origin, street address, address line, pronouns, date of birth, birthdate, age (except a yes/no "18 or older"), SSN, social security |
| Attestation | consent, certify, certification, attest, signature, sign, acknowledge, "information is true", agree to the terms |

| Entry point | On a match |
|---|---|
| **Save as answer** on a custom question | Refused: "This one is yours; jobhunter doesn't store it" |
| Any `packet_answer` write (one function, `apply/answers.py::save_packet_answer`, the only writer of `packet_answer`) | Raises; nothing written |
| **Promote to /prefs** on a saved packet answer, and the `/prefs` Application answers save | Refused with the same message; nothing written |
| Loading `preferences.yaml` (hand edits included) | The `answers:` pydantic model rejects the entry; the page shows the error and packets ignore `answers:` until it is fixed |
| **Question draft** | No generation, no spend; the page says "This one is yours" |
| Phase 2 `fill-classify` | Action `yours`, evaluated before every other row ([precedence](#what-the-helper-does)) |

A false positive only means you answer by hand. One test per entry point.

## Application answers

A section on the existing `/prefs` page ([014](014-preferences-console.md)), not a new page or file. It uses the
014 conventions: the same form, live preview and save flow, and a `?` help text on each field. It is stored
in the existing preferences file under an `answers:` key. No `answers.yaml`, and no deny rule beyond what
the preferences file already has.

**Holds reusable, non-sensitive answers only:** links (portfolio, GitHub, LinkedIn), notice period,
relocation and remote preference, work authorization as yes/no (only if you want it there), and saved
custom answers such as "why this company" templates.

**The [never-store list](#never-store-list) still applies, at the model level.** The check lives in the
pydantic `answers` model (`Profile` is `extra="ignore"` today, so `answers:` gets an explicit model), which
runs on every load and every save: a hand edit of `profile/preferences.yaml`, the `/prefs` form save, and
promotion from a packet. Any answer whose label matches is rejected with a clear message (for example
"'Desired salary' is on the never-store list; answer it by hand") and nothing is written. The `answers:` key
is not part of any scoring prompt.

**Two stores, no overlap.** **Save as answer** on a packet writes only `packet_answer` (per-packet, source
`user` or `draft`) through `apply/answers.py::save_packet_answer`. A separate **Promote to /prefs** action on a
saved packet answer copies it into `answers:` through the same model validation and the 014 ruamel round-trip
write. The `/prefs` form save is the other writer of `answers:`; there is no single writer of `answers:`,
which is why the check sits in the model.

**Packets read from it**: the packet page offers `answers:` entries to copy next to matching questions. They
are never sent to the model (see Never sent).

## Phase 1: packets

### Flow

```
 Inbox / Detail / New packet        Packet page                              You
 ───────────────────────────        ───────────                              ───
 [p] Prepare, or [n] paste a  ──►   Generate (≈ $0.15-0.25) resume, cover v1
     URL or posting text            cited line beside every line
                                    checker flags 2 lines → you fix → v2
                                    [Mark ready] → export PDF / text
                                    drafts + checklist ────────────────────► attach resume,
                                    [Open application] (via /apply/{id}) ──► let the site and
                                                                             your helper fill,
                                                                             paste drafts, Submit
 Pipeline: applied ◄── "Did you apply?" on return, or mail proposal ◄───────── confirmation email
```

| # | Step | Writes |
|---|---|---|
| 1 | **Prepare** (`p` in the inbox, or the button next to **Apply** on `/job/{id}`), or **New packet** (`n`) | `application` at `preparing`, `application_packet`; for a pasted posting also a `paste-manual` group |
| 2 | **Generate** resume variant and, if wanted, cover letter. Estimate shown first; nothing spent until you click | `packet_document` v1, `llm_spend` tier `packet` |
| 3 | **Review and edit** beside the base resume, cited line beside each generated line | new `packet_document` versions |
| 4 | **Mark ready.** Enabled only when the **current version's** `check_report` has no unconfirmed item | `status = 'ready'`, exports rendered |
| 5 | **Open application** through the existing `/apply/{id}` redirect, so 015's click log and "Did you apply?" prompt work. Hidden when no URL was given | `apply_click` |
| 6 | You attach the resume, let the site and your helper fill, paste drafts, and submit | nothing |
| 7 | The application reaches `applied` by any path: "Did you apply?" *Yes*, an accepted mail proposal, or a manual event | `application_event` `applied`; `tracking.add_event` attaches the packet ([Closing the loop](#closing-the-loop)) |

### New packet from a URL or text

**New packet** on the inbox toolbar (`n`), route `GET/POST /apply/new`. It is the standalone
entry to the resume and cover letter tools: no ingest, no score, no fill needed.

| Field | Required | Notes |
|---|---|---|
| Posting or apply URL | one of URL or text | Classified with `ats_rules` ([015](015-apply-links.md#resolution)); embed URLs (`/embed/job_app`, `ashby_embed`) rewritten to the direct board URL |
| Posting text | one of URL or text | Pasted description; needed for tailoring and for quote checks |
| Employer, title | yes | Pre-filled from the page title or URL when parseable; you confirm |

**Fetch posting text** (optional button) goes through `FetchContext`, so robots.txt decides
(surveyed 2026-10-09): `jobs.lever.co` yes (`Crawl-delay: 1`); `apply.workable.com` yes;
Greenhouse yes for the direct board URL, never `/embed/`; `jobs.ashbyhq.com` **no** (its text
loads from `/api/`, which robots disallows), so paste; anything else per its robots.txt, and on
refusal or an empty page you paste.

It writes, through a new helper `apply/paste.py::insert_pasted_posting` (not
`detail.paste_description`, which bumps `description_rev` to queue a re-score):

- a `job_group` with source **`paste-manual`** and one `job` row holding the description, at
  stage **`normalized`**, so the nightly prefilter and screen never pick it up;
- `job.url` = the URL given, or a synthetic `paste:<packet_id>` that is never shown or opened
  (Open application is hidden and `/apply/{id}` returns 404 for it);
- an `apply_link` when a URL was given.

If the URL, or `employer_norm` + normalized title, matches an existing group, that group is
offered instead. **Prepare** on a group with no description (today's 18 `email-manual` ones)
asks for the posting text first.

**Score this group now** (button on the packet page; `jobhunter score --group <id>`) is the only
way a pasted posting is scored. It shows the Stage 2 estimate, and on confirm writes a
`prefilter_result` with `passed = 1` and reason `user-requested` at the current
`filter_version`, moves the job to `prefiltered`, and runs screen with `only = {group}` against
the scoring caps. Over the cap it is refused and nothing is written. Your request bypasses
prefilter rules (state, salary floor) because you chose this posting.

**Dashboard.** `paste-manual` is **not** added to the `email-manual` equality in
`dashboard.py` (which drops groups without an application and labels the rest "elsewhere"). It
gets its own Sankey source node, **Pasted**, and once scored flows through the buckets like an
ingested group; unscored pasted groups appear only in the pipeline.

### Targeted resume

| | |
|---|---|
| Model | `claude-opus-5`, configurable as `[apply] model`. A handful per week; wording quality is the point |
| API | Official `anthropic` SDK, `messages.stream` (long structured output; you watch it arrive), structured output via `output_config`, adaptive thinking, **effort `medium`** (`[apply] effort`) |
| Caching | System prompt + numbered resume as the cached prefix. The 5-minute TTL usually expires while you review, so regenerate is costed uncached |
| Prompt version | `apply-v1`, stored on every `packet_document` |
| Spend cap | Its own **`[apply] daily_cap_usd`** (default `$1.00`), checked before the call; refused, not truncated, when over. Packet spend neither counts against nor is blocked by `scoring.daily_cap_usd` / `weekly_cap_usd`: `screen.remaining_daily_budget` and `weekly_remaining` exclude tier `packet`. Both show on `/costs` |

**Sent:** your base resume as numbered lines (`L1`...`Ln`), header included, exactly as Stage 2
already sends it ([008](008-compliance.md#personal-data)); `current_focus`, `done_with` and
`narrative.want`; the posting's title, employer and text; when the job is scored, the verified
evidence quotes, `tailoring_hints` and Stage 3 `requirement_gaps` ([006](006-fit-scoring.md));
for a cover letter or "Why us?" draft, your employer notes; for a behavioral draft, your story
facts.

**Never sent:** saved answers (`packet_answer` values other than notes and story facts), everything under
`answers:` on `/prefs` (links, notice period, work authorization, templates; it is only shown to you to copy), salary
preferences, filters and weights, other jobs, application history, email content.

The model returns structure, not prose, so every line can be checked:

```json
{"resume": {
   "summary":  {"text": "...", "sources": ["L4", "L12"]},
   "sections": [{"heading": "Experience", "entries": [
       {"source_line": "L10", "employer": "...", "title": "...", "dates": "...",
        "bullets": [{"text": "...", "sources": ["L14"]}]}]}],
   "skills":   [{"name": "...", "sources": ["L31"]}],
   "omitted":  ["L18", "L19"],
   "change_notes": ["Moved the migration bullet first: the posting asks for ..."]},
 "cover_letter": {"paragraphs": [
   {"text": "...", "resume_sources": ["L14"], "posting_quotes": ["verbatim posting text"]}]}}
```

<a id="no-fabrication-rule"></a>**No-fabrication rule: only facts in the resume, the posting, or
your notes.** `apply/factcheck.py` runs on every version, generated or edited, and writes its
`check_report`:

| Check | Fails when |
|---|---|
| Citation | a bullet, summary, skill or paragraph cites no source line, or a line that does not exist. Sources are resume lines `L*`, employer-note lines `N*` and story-fact lines `S*` |
| Structure | an entry's employer, title or dates differ from the cited line (after whitespace and case normalization) |
| Numbers | a number, percentage, dollar amount, year or "N+" is not in the cited lines |
| Skills | a skill or technology is not anywhere in the resume (token match, small synonym table such as `k8s` = `Kubernetes`) |
| Credentials | *certified*, *licensed*, *clearance*, *degree*, *PhD*, *MBA* and similar without the same term in a cited line |
| **Claim strength** | the output uses a word from a claim class that no cited line uses. Classes, as data in `apply/claims.py`: **leadership** (*led, managed, owned, headed, directed, supervised, spearheaded, drove*), **scope** (*architected, designed the, founded, built the team, company-wide*), **team size** (*team of N*, *N engineers*, *N reports*), **superlative** (*expert, best, first, sole, top, world-class*). "Contributed to the migration" (L14) rewritten as "led the migration" fails |
| **Employer claims** | a letter or draft sentence containing *you*, *your* or the employer's name (normalized tokens of `employer_norm`) that does not contain a posting quote passing the check below. Flagged for confirmation even when it came from your notes; the model's own say-so is never trusted |
| Posting quotes | a quote fails the existing verbatim check (`quote_found`, [006](006-fit-scoring.md)) |

**Every generated line shows its cited lines** beside it, not only failures. The checker catches
mechanical inventions; it cannot tell whether "improved deploy reliability" fairly restates L14.
That judgment is yours, and the evidence is in front of you for every line. Failures are badged
**unsupported**; **This is true, keep it** confirms one and is recorded in `check_report`.
**Mark ready is gated on the current version's `check_report` alone**: enabled only when every
item is passing or confirmed. Editing a line never clears its badge; the checker re-runs on the
edited text.

**Advisory entailment pass** (`[apply] entailment_check`, default off): `claude-haiku-4-5` says
per line whether the cited lines support it (`yes` / `partly` / `no`). Advisory only: it never
clears an **unsupported** badge. About $0.003 per packet.

**Editing** is structured, so citations survive. The variant sits beside the base resume, with
`omitted` lines restorable in one click and the model's `change_notes`. Each summary, bullet,
skill and paragraph is its own edit box that keeps its `sources`; moving an item keeps them.
A new item needs a picked source line or **This is true**. A confirmation carries forward to the
next version only for an item whose text is unchanged. **Save** writes a new version
(`origin = 'edited'`); nothing is overwritten. **Regenerate** takes an optional one-line
instruction ("shorter", "lead with the security work"). **Use base resume** is always there and
costs nothing.

### Cover letter and question drafts

Optional per packet. Before generating, the packet page asks for one to three lines of
**employer notes** in your words (why this employer, any history), numbered `N1`-`N3`. Blank is
fine; the letter then says nothing about the employer beyond posting quotes. Same generator,
same checker, same editor and versions.

**Question drafts:** paste a custom question and get a checked draft (`kind =
'question_draft'`). The question is first checked against the [never-store
list](#never-store-list); a match gets "This one is yours" and no draft.

| Question kind (patterns in `apply/labels.py`) | What happens |
|---|---|
| **Behavioral** ("Describe a time...", "Tell us about a situation...", "Give an example...") | You write one to three lines of the story's facts first (`S1`-`S3`). The draft only structures what you wrote and what the resume says; any sentence without an `S*` or `L*` source is **unsupported**. No facts, no draft |
| **Why us / why this role** | Uses employer notes and posting quotes; employer-claims check applies |
| **Numeric experience** ("Years of Terraform") | Shows the resume evidence lines, not a computed number |
| Anything else | Drafted from resume lines, checked as above |

**Save as answer** keeps a draft or your own text on the packet (`packet_answer`, source
`user` or `draft`) and writes nothing to `/prefs`. Saved answers are reusable: the same normalized question on a later packet
offers your earlier answers to copy. **Promote to /prefs** on a saved answer copies it into [Application answers](#application-answers), which every packet offers to copy.

### Export

| Format | How |
|---|---|
| **PDF** (default) | Markdown → one jinja HTML template → headless Chromium `page.pdf()` (the existing optional `browser` extra), networking disabled. Without Playwright, the HTML is offered for print-to-PDF |
| **Plain text** | Copy button, for "paste your resume" boxes and email |
| **Markdown** | Download, for your own editing |
| `.docx` | [Open question](#open-questions). Some ATS parsers read `.docx` more reliably than PDF, which matters more now that filling leans on the parse |

Files go to `<data dir>/packets/<packet_id>/`, named for employers as
`<First>-<Last>-Resume.pdf` and `-Cover-Letter.pdf`. `<data dir>` is today's gitignored,
repo-relative `data/` ([002](002-architecture.md#configuration)); it becomes
`$XDG_DATA_HOME/jobhunter` if bug `e7da19f` lands first. 017 depends on nothing else from
`e7da19f` and resolves the path through the same config setting either way.

### Checklists

The packet page shows the exported files, the drafts and saved answers with a copy button
each, and a per-board checklist in resume-first order:

| Board | Checklist |
|---|---|
| **Any ATS** (default) | Attach the resume first (or use the site's "Autofill from resume" box) and let the site and your fill helper fill; check the parsed work history against your resume; answer custom questions (drafts available); salary, EEO, consent and signatures are yours; review; submit. **Do not re-upload the file after correcting fields: most ATSs re-parse and overwrite your corrections** |
| **USAJOBS** | Sign in via login.gov; build or pick the resume in USAJOBS and check the announcement's rules (page limit, month/year dates, hours per week); attach listed documents; questionnaire. For each self-assessment question the panel shows **the resume lines that may be relevant, nothing else**, never a level |
| **Workday** (`*.myworkdayjobs.com`) | Sign in to the employer account (your password manager); upload; **check the parsed history** (Workday often splits or merges jobs); then My Information, Application Questions; disclosures yourself; review |
| **NEOGOV** (`governmentjobs.com`) | Profile fields; supplemental question drafts (checked like letters) |

### Cost

Opus 5 at $5 / $25 per million input / output tokens (`scoring/scorers.py`); thinking tokens bill
as output.

| Item | Input | Output (incl. thinking, effort medium) | Cost |
|---|---:|---:|---:|
| Resume variant + cover letter | ~9,000 | ~4,000-7,000 | **≈ $0.15-0.22** |
| Regenerate (cache usually expired) | ~9,000 | ~4,000-7,000 | ≈ $0.15-0.22 |
| One question draft | ~5,000 | ~600-1,000 | ≈ $0.04-0.05 |
| **Typical packet** (generate, regenerate, two drafts) | | | **≈ $0.40-0.55** |

Five packets a week is about **$2-3/week**, under the default `[apply] daily_cap_usd`. These are
estimates: phase 1b replaces them with `count_tokens` on your real resume and a real posting
before the estimate is shown, and once there is history the estimate before **Generate** uses
measured cost from `llm_spend`. Scoring a pasted posting is the normal Stage 2 cost, shown
separately and charged to the scoring caps.

### Data model

One migration, numbered next free at implementation time (highest on `main` today `0028`):
`NNNN_application_packet.sql`.

```sql
CREATE TABLE application_packet (
  id               INTEGER PRIMARY KEY,
  application_id   INTEGER NOT NULL REFERENCES application(id),  -- group via application
  status           TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'ready', 'abandoned')),
  resume_doc_id    INTEGER REFERENCES packet_document(id),   -- the version to send
  cover_doc_id     INTEGER REFERENCES packet_document(id),   -- NULL = no cover letter
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  ready_at         TEXT
);
-- One live packet per application; abandoned ones are kept as history.
CREATE UNIQUE INDEX application_packet_live
  ON application_packet(application_id) WHERE status != 'abandoned';

CREATE TABLE packet_document (
  id               INTEGER PRIMARY KEY,
  packet_id        INTEGER NOT NULL REFERENCES application_packet(id),
  kind             TEXT NOT NULL CHECK (kind IN ('resume', 'cover_letter', 'question_draft')),
  version          INTEGER NOT NULL,
  question_key     TEXT NOT NULL DEFAULT '',   -- question_draft: normalized label
  origin           TEXT NOT NULL CHECK (origin IN ('generated', 'edited', 'base')),
  parent_id        INTEGER REFERENCES packet_document(id),
  doc_json         TEXT NOT NULL,              -- the structure above, sources per item
  body_md          TEXT NOT NULL,              -- rendered from doc_json
  check_report     TEXT NOT NULL,              -- JSON: unsupported items, your confirmations
  rendered_path    TEXT,                       -- <data dir>/packets/<packet_id>/resume-v3.pdf
  model            TEXT,
  prompt_version   TEXT,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  cost_usd         REAL,
  created_at       TEXT NOT NULL,
  UNIQUE (packet_id, kind, question_key, version)
);

CREATE TABLE packet_answer (
  id               INTEGER PRIMARY KEY,
  packet_id        INTEGER NOT NULL REFERENCES application_packet(id),
  field_key        TEXT NOT NULL,     -- 'q:<normalized label>', 'notes:employer', 'story:<q>'
  label            TEXT,
  value            TEXT,
  source           TEXT NOT NULL CHECK (source IN ('draft', 'user')),
  UNIQUE (packet_id, field_key)
);
```

"Submitted" is not a stored status: a packet counts as sent when its application has reached
`applied` or later. `llm_spend` gets `tier = 'packet'`, its own line on `/costs`. No new
`application_event.source`.

### Merges and undo

Packets hang off `application`, never `job_group`, so `dedupe_url._absorb` and
`dedupe_xstate.merge_cross_state` need no change for them. The changes are where applications
are deleted:

| Code | Change |
|---|---|
| `dedupe_url._merge_application` | Before `DELETE FROM application` for the losing row: re-point its `application_packet` rows to the winner. If the winner already has a live packet, the loser's becomes `abandoned` (kept, with its documents and answers, under the winning application). `packet_document`, `packet_answer` and `fill_session` follow through `packet_id` |
| `inbox.py` shortlist undo | `application_packet` joins the `others` keep check, so an application with a packet is never deleted |

Tests: URL merge and cross-state merge with a packet on the absorbed side, on the winning side,
and on both (winner's stays live, loser's `abandoned`, no FK error, daily run completes);
shortlist undo keeps an application that has a packet.

## Phase 2 gate

Phase 2 is built only when both hold:

1. **Use.** After 4 weeks of phase 1, `jobhunter apply stats` shows at least **8 packets marked
   ready** for postings on the four public ATSs ([open question](#open-questions) on the
   threshold), and you say the remainder after the resume parse and your fill helpers still
   costs real time. Per-ATS support is built in order of count.
2. **Spike** (2a below) answers, per ATS: can Claude Code drive Claude in Chrome in a dedicated
   profile from where it runs; do the restricted launch flags and site rules hold; does the
   guard's dialog block the extension; **can the extension attach a local PDF to the file field
   and the parse box without a native file chooser**; are element refs stable from a page read
   to a form input; does the hook see the key name and element ref in tool input. If the dialog
   does not block the extension, phase 2 does not ship. If attaching fails, you attach (the
   default below anyway).

If the gate fails, the drafts and checklists remain the answer.

## Phase 2: resume-first fill

### What the helper does

`jobhunter apply session <job_id>` launches the `/apply-fill` skill in a restricted Claude Code
session ([isolation](#isolation)). The sorting is done by jobhunter's code, not the model: the
agent reads the form and passes the field list to `fill-classify`, which is what the tests
cover.

1. `jobhunter apply fill-start <job_id>` refuses unless the packet is `ready`, the posting is not
   `expired`, no other fill session is open, and today's count is under the cap (10, hard cap
   25). It prints a session id, the ATS, and the resume and letter file paths. **No answers or
   drafts.**
2. Opens `http://127.0.0.1:8808/apply/{group_id}` in a new tab, so the existing redirect logs
   the click. Login wall, account wall or challenge: stop, outcome `challenge`.
3. Checks the guard banner is showing ([below](#the-stop-before-submit)). No banner, no filling.
4. **Snapshot before:** reads every field (ref, id, name, label, type, value) and sends the list
   to `fill-classify <session_id> --before` on stdin.
5. **Attach.** Asks you in chat to attach the resume, using the control `ats_forms.py` names for
   this ATS (`parse_control`, for example Ashby's "Autofill from resume" box, or the
   `attachment_field` when one control does both), then say *done*. If the spike showed
   attaching works and you allow it, the helper attaches instead.
6. **Settle:** re-reads the form until no value has changed for 3 seconds, timeout 30 seconds
   (on timeout it reports `parse did not settle` and continues).
7. **Snapshot after**, sent to `fill-classify <session_id> --after`, which diffs the two and
   returns an action per field, first matching row wins:

| # | Field | Action |
|---|---|---|
| 1 | Label, name or id matches the [never-store list](#never-store-list) (includes consent, certification, signature) | `yours`: not touched, listed |
| 2 | Non-empty **before** attaching (ATS draft, returning-candidate prefill, your typing, an earlier helper run, your fill helper) | `keep`: never touched, not compared |
| 3 | Filled by the parse, matches the packet resume's entries | `ok` |
| 4 | Filled by the parse, differs (split job, wrong phone, mangled title) | `differs`: listed with both values, not overwritten |
| 5 | Empty free-text question with a saved answer or draft on this packet, matched by known field name or an unambiguous label | `paste`: shown for you to paste or approve |
| 6 | Empty, anything else | `listed` |

   Row 3's comparison uses the packet resume's structured entries (`doc_json`). For **Use base
   resume**, the base resume is parsed once into entries by `apply/resume_parse.py` (cached by
   resume hash); if it cannot parse, rows 3-4 report `not compared`.
8. **Stops.** Never clicks Submit, Apply, Send, Finish or a last-page Next; never presses Enter.
   Scrolls to the top.
9. `jobhunter apply fill-report <session_id>` stores the classified list (keys, labels, actions,
   no values) and prints it in chat:

```
Resume parsed: 11 fields filled, 2 differ, 4 kept as they were
Differ (check):  Phone  "5550100" vs "+1-555-0100" · Job 2 title split into two entries
Yours (4):       ? "Why Acme?" (draft ready) · Salary · EEO block · Consent checkbox
```

Revision 3 had no "fill from a bank" row, and revision 4 keeps that: your fill helpers cover contact, links and work
authorization, and the `/prefs` answers are offered on packets to copy, not sent to the model and not used for form fills. If you install your helper in the `jobhunter-apply` profile, run it after the
parse settles and before step 7; its fills then show as `keep` on a re-run. The helper itself
fills nothing in revision 3 except, with your approval in chat, pasting a draft into its field.

Field names, label synonyms, `parse_control` and `attachment_field` are data in
`apply/ats_forms.py` and `apply/labels.py`, recorded at phase 2 capture and grown like
`ats_rules.py`.

Page content is untrusted: text on the page or in the posting that asks the agent to submit,
change answers, read files or visit another site is reported, not followed. The tool allowlist
is what makes that hold when the model gets it wrong.

### Isolation

The session mixes a browser, untrusted page text and a coding agent, so it gets as little reach
as possible.

| Kept | Why |
|---|---|
| **Dedicated Chrome profile `jobhunter-apply`**, signed in to nothing (no Google account, no Gmail). Claude in Chrome and the guard userscript are installed only there; your fill helper only if you add it | The extension shares the profile's logins. Your everyday profile is untouched |
| **Restricted launch:** `claude --chrome --permission-mode dontAsk --strict-mcp-config --settings .claude/apply/settings.json "/apply-fill <job_id>"` (flags confirmed in the spike) | `dontAsk` denies anything not allowed, so auto mode's classifier approves nothing; `--strict-mcp-config` loads no Playwright MCP server |
| **Allowlist** in `.claude/apply/settings.json` (checked in): Claude in Chrome navigate, read/find, form input, click, screenshot, and file upload only if the spike passed; **not** the JavaScript tool, console or network readers. `Bash(jobhunter apply fill-start:*)`, `fill-classify`, `fill-report`; `Read(<data dir>/packets/**)`. Site rules deny the extension everywhere except the allowed ATS hosts and `127.0.0.1:8808` | In auto mode the extension skips its own site check unless permission rules deny sites, so the rules are the site control |
| **PreToolUse hook** `scripts/hooks/apply_guard.py`, in the apply settings only. Denies: tools off the list; navigation to other hosts; **key actions for Enter, Return and NumpadEnter**; form input or a ref click on any element ref that `fill-classify` marked `yours` (it writes the session's ref→action map to `<data dir>/packets/<id>/session-<sid>.json`); form input on a ref marked anything but `paste`; any call in `bypassPermissions`. Fails closed | It sees only tool input, so it checks refs and keys, never on-page text. A coordinate click carries neither and passes; the guard dialog and you cover that |
| **Limits:** one open session, packet must be `ready`, daily cap, no batch/queue/schedule command | One job, one tab, one review |

`fill-start` also refuses unless `JOBHUNTER_APPLY_SESSION=1`, which the launcher sets. That is a
convenience check, not a control: running the skill from your normal session is your choice and
outside these protections, and the skill says to use the launcher.

### The stop before Submit

Best effort, in layers. The guard dialog and you are the layers that hold when the model gets it
wrong.

| Layer | Stops | Gap |
|---|---|---|
| **Guard userscript** `jobhunter-guard.user.js`, apply profile only, the allowed ATS hosts, `@run-at document-start`. Capture-phase listeners on `window` for `submit` events (covers Enter), clicks on submit-type controls (`button` without a type or `type=submit`, `input[type=submit\|image]`) and on any `button`/`[role=button]` whose text, `value` or `aria-label` matches submit / apply / send / finish (en, es, fr, de, pt). Each calls `confirm("Submit this application? jobhunter listed N fields for you.")` synchronously; Cancel stops the event. Shows a "jobhunter: review, then submit" banner when installed | An unlabelled icon button that submits by `fetch` |
| **You** press Submit and answer the guard's dialog | |
| **Hook** (above): Enter keys, refs marked `yours`, form input outside `paste` | Coordinate clicks |
| **Skill rules** and the required `stopped_before_submit` outcome | Depends on the model |

Event listeners work from a userscript manager's isolated world as well as the page world, so
the guard needs no prototype wrapping, no world-specific install and no self-test.

### Captcha and challenges

Never solved, bypassed or avoided; no simulated mouse or typing cadence. A reCAPTCHA, hCaptcha,
email or SMS code, "verify you are human" page or login wall ends the session (`challenge`) and
the tab is yours. If confirmations from one ATS stop arriving after helped applications, set
`[apply.ats.<name>] fill = false` and use the checklist for it.

### Phase 2 data

`NNNN_fill_session.sql`, for the cap, the one-at-a-time rule, stats and the report view:

```sql
CREATE TABLE fill_session (
  id           INTEGER PRIMARY KEY,
  packet_id    INTEGER NOT NULL REFERENCES application_packet(id),
  host         TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  outcome      TEXT CHECK (outcome IN ('stopped_before_submit', 'challenge', 'aborted', 'error')),
  report       TEXT              -- JSON: keys, labels, actions. Never values
);
```

## Closing the loop

The packet page's **Open application** and the helper both go through `/apply/{id}`, so the
existing "Did you apply?" prompt ([015](015-apply-links.md#fresh-at-click-time)) appears on
return; the mail match proposes the confirmation as today.

The packet is attached in **`tracking.add_event`**, so every path behaves the same (the prompt,
an accepted mail proposal, which recorded 18 of your 20 applications, and a manual event): when
the event is `applied` and the application has a `ready` packet, it sets
`application.resume_version` to `packet:<id>/resume/v<n>` and `cover_letter_path` to the
rendered letter, and adds `attachment` rows for the rendered files (outside the tracked tree, as
`tracking.validate_attachment_path` requires), so `/pipeline/{id}` shows what was sent.
`answer_prompt` and the proposal accept path need no packet code of their own.

A packet `ready` for 7 days whose application has **no `applied` event** shows on `/followups`
as "ready, not applied?".

## Security and privacy

- **Console.** Still loopback; new `POST` routes inherit the console's same-origin middleware
  (`same_origin_writes`). No route sends `Access-Control-Allow-*`.
- **No credentials** stored or seen. You sign in; the helper stops at login pages.
- **Nothing personal in the repo.** Packets, saved answers and exports live under `<data dir>`
  and the database; fixtures are captured logged out and linted ([Testing](#testing)).
- **Logs** record ids, keys, labels, outcomes and costs, never answer values or document bodies.

| Data | Anthropic API (generation) | Claude in Chrome (phase 2) | The employer |
|---|---|---|---|
| Resume text, header included | yes, as Stage 2 already sends it | yes: the attached file if the helper attaches it, and the parsed values in page reads and screenshots | yes |
| Posting, fit evidence | yes | page contents | — |
| Saved answers and drafts | drafts are generated there; saved answers are never sent back | only the one you approve pasting (`fill-start` prints none) | when pasted |
| Employer notes, story facts | yes (letter, drafts) | no | in the letter or answer |
| Never-store list | not stored | not stored; may appear in screenshots if the form shows them | when you type them |
| OpenRouter, Jev, a local scorer | never | | |

## Compliance

| Rule in [008](008-compliance.md) | How |
|---|---|
| No automated submission | You submit; layered stop in phase 2; no flag changes it |
| No CAPTCHA solving | Stop and hand back |
| No login automation or credential storage | None; Workday and login.gov are yours |
| robots.txt; only `FetchContext` does network I/O | Generation fetches nothing. **Fetch posting text** goes through `FetchContext` per host robots. In phase 2 the agent opens one page in your browser, at your request, for the job you chose; jobhunter's own code makes no ATS request |
| Honest identity | The extension acts as you; no UA tricks or proxies |

**ATS terms.** We found no candidate-facing terms from the four ATSs about autofill in a
candidate's own browser; their terms and spam defenses target automated and bulk submission. One
person, one application, reviewed and submitted by hand is the posture least likely to conflict.
Not legal advice ([008](008-compliance.md#legal-note-briefly)).

### Deviations from earlier specs

Recorded on `7fa29db`; each needs your approval and an amendment:

| Spec | Today | Proposed | Why |
|---|---|---|---|
| [001](001-goals-and-scope.md#non-goals) | "Resume generation: ... It does not write your resume." | "No resume written from scratch. Tailored variants of your own resume, limited by the no-fabrication checker and your review (017), are in scope." | You asked for it; the checker and line-by-line evidence address the fabrication risk the non-goal guarded |
| [001](001-goals-and-scope.md#non-goals) | "Private-sector job boards (Indeed, LinkedIn, Greenhouse): ..." | "No *ingest* from private-sector boards. 017 may fetch one posting you paste and, after its gate, help with one form you chose in your own browser, never submitting." | That non-goal is about crawling for jobs; 017 crawls nothing |
| [015](015-apply-links.md) | "It never fills it in or submits it" | "The Apply button never fills or submits. Form help is a separate packet action (017, phase 2), and nothing in jobhunter submits." | The button keeps its behavior |
| [009](009-roadmap.md#m9--remainder-and-ops) | M9 "Remainder and ops", 0.5-1.5 agent-days; the whole roadmap 5.75-11 d | New **M10 Assisted apply** after M9: phase 1 ≈ 4.5-5.5 d, phase 2 ≈ 3.5-5 d after its gate | 017 alone is about the size of the original roadmap; folding it into M9 would hide that |
| [008](008-compliance.md#personal-data) | Personal data "never leave the machine except as LLM request bodies" | "...except as LLM request bodies, and, in a 017 phase 2 session you start, the form contents, parsed values and screenshots Claude in Chrome reads" | Phase 2 sends page reads and screenshots through the extension, which is not a jobhunter LLM request |

## Failure modes

| Failure | Behavior |
|---|---|
| Generator returns invalid JSON or times out | Nothing saved; retry; error on the page |
| Generator invents or inflates a fact | Badged by factcheck; Mark ready blocked until fixed or confirmed |
| Overstatement the lexicon misses | Not detected; the cited line beside every line, and the optional entailment pass |
| Over `[apply] daily_cap_usd` | Generate disabled; **Use base resume** still works; scoring unaffected |
| A question on the never-store list | No draft, no save, no fill: "This one is yours" |
| Behavioral question without story facts | No draft until you write them |
| Pasted posting duplicates a job | Existing group offered |
| Nightly merge absorbs a group or application with a packet | Packet re-pointed or `abandoned` under the winner; merge completes |
| Fetch refused by robots or page empty | Paste the text |
| Playwright missing | HTML for print-to-PDF |
| Already applied to this employer and title | Warning before Generate |
| *Phase 2:* guard banner missing | No filling |
| *Phase 2:* parse filled something wrongly | `differs`, listed, not overwritten |
| *Phase 2:* parse does not settle in 30 s | Reported; classified as is |
| *Phase 2:* ATS changed its form | Fewer known-name matches; more fields listed; fixture refresh bug |
| *Phase 2:* file rejected, extension disconnected | Session stops (`error` / `aborted`); tab left as is |
| *Phase 2:* challenge or login wall | `challenge`; tab is yours |
| *Phase 2:* prompt injection on the page | Reported, not followed; no JS, three commands, reads only packets, allowed hosts only, submit needs you |

## Testing

All offline; `tests/conftest.py`'s network guard stays as is. The Anthropic client is always
mocked.

| Area | How |
|---|---|
| Generator | Canned JSON from a mocked client; streaming; effort and thinking set; the request body contains no saved `packet_answer` values and no `answers:` content (seeded with a link, notice period and template; none appears in the body) |
| Factcheck | Table tests: new employer, changed date, invented number, unlisted skill, invented certification, "contributed" → "led", added team size, added "expert", employer sentence without a quote (with and without notes), bad posting quote, behavioral sentence without `S*`/`L*`, confirmed line |
| Editor | Sources and confirmations carry forward for unchanged items; an edited item is re-checked and keeps its badge if it still fails; a new item without a source blocks Mark ready |
| Never-store | One test per entry point: Save as answer, `save_packet_answer`, Promote to /prefs, `/prefs` answers save, loading a hand-edited `preferences.yaml` with a 'Desired salary' answer (model rejects it; packets do not offer it), question draft (no client call), `fill-classify` row 1 beats every other row |
| Spend | Packet spend ignored by `remaining_daily_budget` / `weekly_remaining`; `[apply] daily_cap_usd` refuses before the call |
| Paste | `/apply/new` URL-only, text-only, dedup, robots refusal (mocked `FetchContext`); job at `normalized` and skipped by a daily run; `description_rev` unchanged; Score this group now records the pass and scores one group; Open application hidden without a URL; Pasted Sankey node; cross-site POSTs refused |
| Merges | As in [Merges and undo](#merges-and-undo) |
| Loop | `add_event('applied')` attaches the packet from the prompt, the proposal accept path and a manual event; followups rule |
| Export | HTML render golden file; PDF when the `browser` extra is present (`e2e`) |
| *Phase 2:* classifier | `fill-classify` on before/after field-list JSON recorded from fixtures and the spike: prefilled kept, parser-filled compared, never-store first, base-resume entries, settle timeout |
| *Phase 2:* fixtures | `tests/fixtures/ats_forms/<ats>/`: rendered DOM of public, empty forms, plus small hand-written pages that mimic a resume-parse prefill, a returning-candidate prefill, a React-controlled input and a `fetch` submit from a labelled `type=button` |
| *Phase 2:* fixture lint | Fails on emails other than `@example.com`, phone numbers, long tokens in attributes, `<script>` in captured fixtures, hidden inputs |
| *Phase 2:* guard | `e2e` Playwright on the fixtures: dialog on a submit-type button, a labelled `type=button`, a localized label and Enter in a text field; Cancel blocks |
| *Phase 2:* session config and hook | Parse `.claude/apply/settings.json`: no JavaScript tool, no `mcp__playwright__*`, three Bash commands, reads only packets. Hook unit tests on recorded tool inputs: Enter/Return denied, form input on a `yours` ref denied, off-host navigation denied, fail-closed |

**Phase 2 acceptance per ATS: one supervised live dry run** on a posting you mean to apply to:
check every field, the attach and the guard dialog (Cancel), then submit it yourself or close the
tab. **Fixture capture** happens once, by hand, in a fresh profile with no ATS sessions or
extensions, direct board URLs only (Greenhouse `/embed/` and Ashby `/api/` are disallowed),
saved before any typing and stripped of scripts, hidden inputs and tokens.

## Phased rollout

Agent-effort estimates, in the style of [009](009-roadmap.md):

| Phase | Work | Effort |
|---|---|---|
| **1a** | Packet migration, merge and undo handling, Prepare (`p`, detail button), **New packet** (`paste-manual` at `normalized`, dedup, robots-aware fetch), Score this group now, Pasted Sankey node, packet page | **1-1.5 d** |
| **1b** | Generator (Opus 5, streaming, effort, caching, own cap, measured estimate), factcheck with claim strength and employer claims, structured cited-line editor, versions, cover letter, employer notes, question drafts with story facts, optional entailment pass, `/costs` line | **2-2.5 d** |
| **1c** | Export (PDF, text, markdown; `.docx` if chosen +0.5 d) | **0.5 d** |
| **1d** | Never-store table and its enforcement, saved answers with reuse, the `/prefs` Application answers section (small), checklists, loop in `add_event`, `/followups` item, `apply stats` | **0.5-1 d** |
| | *Gate: 4 weeks of use, plus the spike* | |
| **2a** | **Spike**: Claude Code driving Claude in Chrome in a dedicated profile; launch flags and site rules; guard dialog blocks the extension; PDF attach per ATS; ref stability; hook inputs | **0.5-1 d** |
| **2b** | Fixture capture and lint, `fill-classify`, base resume parse, `ats_forms.py` controls and label synonyms | **1-1.5 d** |
| **2c** | Apply launch and settings, `/apply-fill` skill, `fill-start`/`fill-report`, hook, `fill_session`, report view | **1-1.5 d** |
| **2d** | Guard userscript and its e2e tests; supervised dry run per ATS | **0.5-1 d** |
| | **Total** | **≈ 8-10.5 d** (phase 1 ≈ 4.5-5.5 d) |

Phase 1 is useful on its own and ships first.

## Decisions recorded on 7fa29db

- **Never-store list as data, checked first at every write, draft and fill.** Reason: stating
  the rule in Non-goals did not stop Save as answer, `packet_answer` or drafts from storing it.
- **Application answers on `/prefs`, in the preferences file under `answers:` (revision 4).**
  Reason: you said "Just add the answers stuff to a section under prefs". No separate file, page or
  deny rule; the never-store table is enforced on save. Reverses revision 3's deferral, narrowly.
- **Saved custom answers also live on packets and are reusable.** Reason: per-packet drafts and
  edits stay with their packet; reusable ones are promoted to `/prefs`.
- **Packets hang off `application`; merges re-point or abandon them.** Reason: a `job_group`
  reference would break the nightly merges as the `rejection` FK did (#78).
- **Pasted postings sit at `normalized` and are scored only on request.** Reason: the nightly run
  must not score them, and the normal path cannot score one that fails a prefilter rule.
- **Own `[apply] daily_cap_usd`.** Reason: packet spend and scoring spend must not block each
  other mid-application or mid-plan.
- **Structured editor; Mark ready gated on the current version's check alone.** Reason:
  citations must survive edits, and an edit must not clear a failing line.
- **Phase 2: snapshot, you attach, settle, snapshot, classify in code.** Reason: only a before
  snapshot separates parser output from your edits, and the code that runs must be the code
  tested.
- **Resume sent as Stage 2 sends it; no header placeholders, deny rule or sentinel test.**
  Reason: the resume header already goes to every scorer, so hiding it here protected nothing.
- **Packet attached in `tracking.add_event`.** Reason: the mail path records most of your
  applications.
- Carried: salary, EEO and self-ID never stored or filled; New packet standalone; Opus 5 for
  generation; claim-strength and employer-claims checks with the cited line beside every line;
  `paste-manual` source; USAJOBS evidence lines only; Open application and the helper through
  `/apply/{id}`; the scaled-down fill guard.

## Open questions

1. **Answer bank.** Answered: yes, as a section on `/prefs` (revision 4).
2. **Your helper in the apply profile.** Will you install your fill helper in the
   `jobhunter-apply` profile, so phase 2 only lists differences and offers drafts?
3. **Export format.** PDF only, or also `.docx` (some parsers read it better)? Any visual
   template the variant should match?
4. **Cover letters.** Only when you ask, or drafted with every packet?
5. **Entailment pass.** On by default (about $0.003 per packet)?
6. **`[apply] daily_cap_usd`.** Is $1.00 a day right?
7. **Phase 2 gate.** 8 ready packets in 4 weeks to the four ATSs: right threshold?
8. **Phase 2 hosts.** Start with the four public ATSs only, or also help on Workday and NEOGOV
   pages after you sign in yourself?
9. **Phase 2 setup.** Will you create a `jobhunter-apply` Chrome profile with Claude in Chrome
   and a userscript manager? Should Claude Code run on the host or in the dev container?
10. **Sharing with the browser agent.** OK that page contents, parsed values and screenshots of
    the form go to Anthropic through Claude in Chrome?
11. **Amend 001, 008, 009 and 015** as in [Deviations](#deviations-from-earlier-specs)?

## History

### Revision 2: user direction

On 2026-10-09 you said: *"The other fill helper tools I use don't enter salary or eeo questions
so not a problem."* *"Most websites now take a resume and fill themselves from it. So the
checker may be more of: if resume, add that first, see what fills, then help with remainder if
needed."* *"Targeted resume tools and cover letter tools may be helpful too."* Revision 2
removed the sensitive store (keyring, passphrase, redaction), the userscript filler and token
handoff, the page-world guard and the repo-wide hook; made phase 1 the documents; and made
phase 2 resume-first. Revision 1 was ≈ 11-15.5 d.

### Revision 3: review round 2

An adversarial review (2026-10-09) raised ten critiques, all taken; each change is listed under
[Decisions](#decisions-recorded-on-7fa29db). In short: the never-store list is enforced in code;
packets survive nightly merges; phase 2 classifies in tested code from a before and after
snapshot, and you attach the resume; edits keep citations; the answer bank is deferred; pasted
postings are scored only on request; packets have their own spend cap and a higher estimate; the
placeholder, deny-rule and sentinel plumbing is gone; the loop covers the mail path; and the
deviations now include 008 and 009. The branch was updated from `main` by a merge, not a rebase,
because it is pushed fast-forward only.

### Revision 4: answers on /prefs per user

You said: *"Just add the answers stuff to a section under prefs"*. Revision 4: answers on `/prefs` per
user. A narrow reversal of revision 3's deferral: an Application answers section on `/prefs`, stored under
`answers:` in the preferences file, never-store enforced on save. See
[Application answers](#application-answers).
