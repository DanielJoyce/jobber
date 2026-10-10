# 017 — Assisted apply: targeted resume, cover letter, resume-first fill

Status: **design, revision 2 (your direction of 2026-10-09), awaiting your approval.** Bug
`7fa29db`, milestone M9. No code yet. What changed from revision 1 and why is at the end
([Revision 2](#revision-2-user-direction)).

You asked: *"Is there any way to automate the application part of the process? Filling out web
forms?"* Then you added that most sites now parse an uploaded resume and fill themselves, that
your existing fill helpers already skip salary and EEO questions, and that targeted resume and
cover letter tools would help too. So this spec is mostly about the **documents**, with form
help as a later, smaller step:

| Phase | What you get | Works on |
|---|---|---|
| **1. Packet** | For any posting (a jobhunter job, a pasted URL, or pasted text): a **targeted resume** built only from facts in your resume, with the source line shown beside every line; a **cover letter**; drafts for custom questions; a small **answer bank**; copy panels and checklists; export | Every posting, with no browser automation at all |
| **2. Resume-first fill** (gated) | In a dedicated Chrome profile, Claude in Chrome uploads your resume, lets the site autofill from it, reads what is filled and what is empty, fills only the gaps it can match, lists what the parser got wrong, and **stops before Submit** | Public no-account ATS forms (Ashby, Greenhouse, Lever, Workable) first. Built only if the [gate](#phase-2-gate) passes |

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
2. **Form filling waits for evidence** that you apply on fillable forms often enough to be worth
   it, and that the resume parse leaves enough work behind.

## Goals

1. **Better applications in less time.** A targeted resume and a cover letter in a few minutes
   of review instead of 20-40 minutes of rewriting.
2. **Tailor without inventing.** The variant and the letter only select, reorder and rephrase
   facts in your resume, plus employer facts quoted from the posting or written by you. A
   deterministic checker flags mechanical fabrication; every generated line shows the resume line
   it cites so you can judge the rest ([no-fabrication rule](#no-fabrication-rule)).
3. **Enter repeated answers once**, for the non-sensitive questions forms keep asking.
4. **Close the loop** without updating the pipeline by hand.

## Non-goals

| Not doing | Why |
|---|---|
| **Submitting an application. Ever.** | See below |
| **Storing or filling salary, EEO or self-identification answers** (gender, race, ethnicity, veteran, disability, orientation), citizenship, street address, pronouns | You answer these by hand; your other fill helpers skip them too. Not storing them removes the need for an encrypted store, a passphrase and redaction plumbing |
| Social Security number, date of birth, ID numbers, passwords, security questions, payment details, criminal history, references' contacts | Never stored, never filled, always yours |
| Ticking consent, certification, signature or "information is true" boxes | Your legal attestations, not data entry |
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

## Phase 1: packets

### Flow

```
 Inbox / Detail / New packet        Packet page                              You
 ───────────────────────────        ───────────                              ───
 [p] Prepare, or [n] paste a  ──►   Generate (≈ $0.11)  resume v1, cover v1
     URL or posting text            source line beside every line
                                    checker flags 2 lines → you edit → v2
                                    [Mark ready] → export PDF / text
                                    copy panels + checklist ───────────────► upload resume first,
                                    [Open application] (via /apply/{id}) ──► let the site fill,
                                                                             fix gaps from panels,
                                                                             press Submit
 Pipeline: applied ◄── "Did you apply?" on return, or mail proposal ◄───────── confirmation email
```

| # | Step | Writes |
|---|---|---|
| 1 | **Prepare** (`p` in the inbox, or the button next to **Apply** on `/job/{id}`), or **New packet** (`n`) | `application` at `preparing`, `application_packet`; for a pasted posting also a `paste-manual` group |
| 2 | **Generate** resume variant and, if wanted, cover letter. Estimate shown first; nothing spent until you click | `packet_document` v1, `llm_spend` tier `packet` |
| 3 | **Review and edit** beside the base resume, cited line beside each generated line | new `packet_document` versions |
| 4 | **Mark ready.** Blocked while any **unsupported** line is neither edited nor confirmed | `status = 'ready'`, exports rendered |
| 5 | **Open application** through the existing `/apply/{id}` redirect, so 015's click log and "Did you apply?" prompt work unchanged | `apply_click` |
| 6 | You upload the resume, let the site fill, finish from the copy panels, and submit | nothing |
| 7 | "Did you apply?" *Yes*, or an accepted mail proposal | `application_event` `applied`, attachments |

### New packet from a URL or text

**New packet** on the inbox toolbar (`n`), route `GET/POST /apply/new`. It is the standalone
entry to the resume and cover letter tools: no ingest, no score, no autofill needed.

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

It writes, like `proposals.py` does for `email-manual` groups: a `job_group` with source
**`paste-manual`** (the dashboard's manual-source handling includes it), one `job` row holding
the description (same path as `detail.paste_description`), and an `apply_link` when a URL was
given. If the URL, or `employer_norm` + normalized title, matches an existing group, that group
is offered instead. **Score it** is offered, never automatic. **Prepare** on a group with no
description (today's 18 `email-manual` ones) asks for the posting text first.

### Targeted resume

| | |
|---|---|
| Model | `claude-opus-5`, configurable as `[apply] model`. A handful per week; wording quality is the point |
| API | Official `anthropic` SDK, synchronous `messages.create` (you are waiting), structured output via `output_config` |
| Caching | System prompt + numbered resume as the cached prefix |
| Prompt version | `apply-v1`, stored on every `packet_document` |
| Spend cap | Checked against the caps ([006](006-fit-scoring.md#cost)) before the call; refused, not truncated, when over |

**Sent:** your base resume as numbered lines (`L1`...`Ln`) with the contact header replaced by
placeholders (put back locally from the base resume after generation); `current_focus`,
`done_with` and `narrative.want`; the posting's title, employer and text; when the job is scored,
the verified evidence quotes, `tailoring_hints` and Stage 3 `requirement_gaps`
([006](006-fit-scoring.md)); for a cover letter or "Why us?" draft, your employer notes.

**Never sent:** the answer bank, salary preferences, filters and weights, other jobs, application
history, email content.

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
   {"text": "...", "resume_sources": ["L14"], "posting_quotes": ["verbatim posting text"],
    "uses_employer_notes": true}]}}
```

<a id="no-fabrication-rule"></a>**No-fabrication rule: only facts in the resume, the posting, or
your notes.** `apply/factcheck.py` runs on every generated and every edited version:

| Check | Fails when |
|---|---|
| Citation | a bullet, summary, skill or paragraph cites no resume line, or a line that does not exist |
| Structure | an entry's employer, title or dates differ from the cited line (after whitespace and case normalization) |
| Numbers | a number, percentage, dollar amount, year or "N+" is not in the cited lines |
| Skills | a skill or technology is not anywhere in the resume (token match, small synonym table such as `k8s` = `Kubernetes`) |
| Credentials | *certified*, *licensed*, *clearance*, *degree*, *PhD*, *MBA* and similar without the same term in a cited line |
| **Claim strength** | the output uses a word from a claim class that no cited line uses. Classes, as data in `apply/claims.py`: **leadership** (*led, managed, owned, headed, directed, supervised, spearheaded, drove*), **scope** (*architected, designed the, founded, built the team, company-wide*), **team size** (*team of N*, *N engineers*, *N reports*), **superlative** (*expert, best, first, sole, top, world-class*). "Contributed to the migration" (L14) rewritten as "led the migration" fails |
| **Employer claims** | a letter or draft sentence about the employer ("your team", "your mission") that is neither a passing posting quote nor supported by your employer notes |
| Posting quotes | a quote fails the existing verbatim check (`quote_found`, [006](006-fit-scoring.md)) |

**Every generated line shows its cited resume lines** beside it, not only failures. The checker
catches mechanical inventions; it cannot tell whether "improved deploy reliability" fairly
restates L14. That judgment is yours, and the evidence is in front of you for every line.
Failures are badged **unsupported**; **Mark ready** stays disabled until each is edited away or
you click **This is true, keep it** (recorded in `check_report`). Your own edits may add facts;
the checker still runs on them so a mistyped date shows.

**Advisory entailment pass** (`[apply] entailment_check`, default off): `claude-haiku-4-5` says
per line whether the cited lines support it (`yes` / `partly` / `no`). Advisory only: it never
clears an **unsupported** badge. About $0.003 per packet.

**Editing.** The variant sits beside the base resume as a line diff, with `omitted` lines
restorable in one click and the model's `change_notes`. Editing is a markdown textarea; **Save**
writes a new version (`origin = 'edited'`); nothing is overwritten. **Regenerate** takes an
optional one-line instruction ("shorter", "lead with the security work"). **Use base resume**
is always there and costs nothing.

### Cover letter and question drafts

Optional per packet. Before generating, the packet page asks for one to three lines of
**employer notes** in your words (why this employer, any history). Blank is fine; the letter
then says nothing about the employer beyond posting quotes. Same generator, same checker,
including the employer-claims check, same editor and versions.

**Question drafts:** paste a custom question ("Why are you interested in Acme?", "Describe a
time you...") and get a checked draft (`kind = 'question_draft'`). Numeric experience questions
("Years of Terraform") show the resume evidence, not a computed number.

### Export

| Format | How |
|---|---|
| **PDF** (default) | Markdown → one jinja HTML template → headless Chromium `page.pdf()` (the existing optional `browser` extra), networking disabled. Without Playwright, the HTML is offered for print-to-PDF |
| **Plain text** | Copy button, for "paste your resume" boxes and email |
| **Markdown** | Download, for your own editing |
| `.docx` | [Open question](#open-questions). Some ATS parsers read `.docx` more reliably than PDF, which matters more now that filling leans on the parse |

Files go to `<data dir>/packets/<packet_id>/` (`<data dir>` is `$XDG_DATA_HOME/jobhunter`,
[002](002-architecture.md#where-files-live), bug `e7da19f`), named for employers as
`<First>-<Last>-Resume.pdf` and `-Cover-Letter.pdf`.

### Answer bank

A small file you edit on a new **Application answers** section of `/prefs`, or by hand. A
[014](014-preferences-console.md#the-design-change-free-settings-vs-paid-settings) *free*
section: no model reads it, so saving costs nothing and changes no version hash.

| Key | Example (placeholder) | Notes |
|---|---|---|
| `name.legal_first`, `.legal_last`, `.preferred` | `Alex`, `Example` | Lever has one full-name field |
| `email`, `phone` | `<you>@example.com`, `+1-555-0100` | |
| `location.city`, `.state`, `.country` | `Denver`, `CO`, `US` | City level only |
| `links.linkedin`, `.github`, `.portfolio`, `.other[]` | `https://www.linkedin.com/in/<you>` | Resume parsers often miss these |
| `work_auth.us_authorized`, `.sponsorship_now`, `.sponsorship_future` | `true`, `false`, `false` | Yes/no questions only |
| `work_auth.clearance` | `none` | Only if it is on your resume |
| `availability.notice_weeks`, `.earliest_start` | `2` | |
| `relocation.willing`, `remote.preference`, `travel.max_percent` | `depends`, `any`, `25` | |
| `age_18_plus`, `heard_about` | `true`, `Company careers site` | |

Per-job overrides and saved answers to custom questions live in `packet_answer`. "Save as
answer" on a custom question adds it to `answers.yaml` under `custom:` with its label, so the
next form's copy panel shows it.

**Storage.** `<data dir>/profile/answers.yaml`, beside `preferences.yaml`, outside the repo;
round-trip `ruamel.yaml`, atomic write, mode `0600`, dated backup on each console save as with
preferences. No change log in SQLite: the values do not affect scoring and the file is yours to
diff.

**Kept away from models and agents:**

- `AnswerBank` (`apply/answers.py`) is not part of `Profile.scoring_inputs` and is not passed
  to the generator.
- A checked-in `.claude/settings.json` denies `Read`, `Edit` and `Write` on
  `**/profile/answers.yaml` and `Bash` commands naming `answers.yaml`, so dev agents in this repo
  do not read it by accident. Bash rules match prefixes and can be got around; with salary and
  EEO out of the file, accidental reads are the risk worth covering. A test asserts the rules
  are present.
- **Sentinel test:** Stage 2, Stage 3, the decisions scorer and the packet generator run against
  a mocked client with a sentinel `answers.yaml`; no sentinel value may appear in any captured
  request body or log record.

### Copy panels and checklists

The packet page lists the answers in groups (Contact, Links, Work authorization, Availability,
Documents, Custom questions) with a copy button each, plus the exported files. A per-board
checklist follows the resume-first order:

| Board | Checklist |
|---|---|
| **Any ATS** (default) | Upload the resume first and let the site fill; check the parsed work history and contact fields against your resume; fill gaps from the panels; answer custom questions (drafts available); salary, EEO, consent and signatures are yours; review; submit |
| **USAJOBS** | Sign in via login.gov; build or pick the resume in USAJOBS and check the announcement's rules (page limit, month/year dates, hours per week); attach listed documents; questionnaire. For each self-assessment question the panel shows **the resume lines that may be relevant, nothing else**, never a level |
| **Workday** (`*.myworkdayjobs.com`) | Sign in to the employer account (your password manager); upload; **check the parsed history** (Workday often splits or merges jobs); then My Information, Application Questions; disclosures yourself; review |
| **NEOGOV** (`governmentjobs.com`) | Profile fields from the panels; supplemental question drafts (checked like letters) |

### Cost

Opus 5 at $5 / $25 per million input / output tokens (`scoring/scorers.py`).

| Item | Input | Output | Cost |
|---|---:|---:|---:|
| Resume variant + cover letter | ~9,000 | ~2,500 | **≈ $0.11** |
| Regenerate (prefix cached) | ~9,000 (~3,500 cached) | ~2,500 | ≈ $0.10 |
| One question draft | ~5,000 (mostly cached) | ~200 | ≈ $0.01-0.03 |
| **Typical packet** (generate, regenerate, two drafts) | | | **≈ $0.25** |

Five packets a week is about **$1.25/week**, small against the ~$28/month in
[README](README.md). Once there is history, the estimate before **Generate** uses measured cost
from `llm_spend`. Scoring a pasted posting is the normal Stage 2 cost, shown separately.

### Data model

One migration, numbered next free at implementation time (highest today `0025`):
`NNNN_application_packet.sql`.

```sql
CREATE TABLE application_packet (
  id               INTEGER PRIMARY KEY,
  application_id   INTEGER NOT NULL UNIQUE REFERENCES application(id),
  job_group_id     INTEGER NOT NULL REFERENCES job_group(id),
  status           TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'ready', 'submitted', 'abandoned')),
  resume_doc_id    INTEGER REFERENCES packet_document(id),   -- the version to send
  cover_doc_id     INTEGER REFERENCES packet_document(id),   -- NULL = no cover letter
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  ready_at         TEXT
);

CREATE TABLE packet_document (
  id               INTEGER PRIMARY KEY,
  packet_id        INTEGER NOT NULL REFERENCES application_packet(id),
  kind             TEXT NOT NULL CHECK (kind IN ('resume', 'cover_letter', 'question_draft')),
  version          INTEGER NOT NULL,
  question_key     TEXT NOT NULL DEFAULT '',   -- question_draft: normalized label
  origin           TEXT NOT NULL CHECK (origin IN ('generated', 'edited', 'base')),
  parent_id        INTEGER REFERENCES packet_document(id),
  body_md          TEXT NOT NULL,              -- contact header as placeholders
  sources          TEXT,                       -- JSON: per line, cited resume line ids
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
  field_key        TEXT NOT NULL,     -- bank key, 'q:<normalized label>', or 'notes:employer'
  label            TEXT,
  value            TEXT,
  source           TEXT NOT NULL CHECK (source IN ('bank', 'override', 'draft', 'user')),
  UNIQUE (packet_id, field_key)
);
```

Fits existing tables: on *applied*, `application.resume_version` gets
`packet:<id>/resume/v<n>`, `cover_letter_path` the rendered letter, and `attachment` rows point
at the rendered files (outside the tracked tree, as `tracking.validate_attachment_path`
requires), so `/pipeline/{id}` shows what was sent. `llm_spend` gets `tier = 'packet'`, its own
line on `/costs`. No new `application_event.source`.

## Phase 2 gate

Phase 2 is built only when both hold:

1. **Use.** After 4 weeks of phase 1, `jobhunter apply stats` shows at least **8 packets marked
   ready** for postings on the four public ATSs ([open question](#open-questions) on the
   threshold), and you say the remainder after the resume parse still costs real time. Per-ATS
   support is built in order of count.
2. **Spike** (2a below) answers: can Claude Code drive Claude in Chrome in a dedicated profile
   from where it runs; do the restricted launch flags and site rules hold; does the guard's
   dialog block the extension. If the dialog does not block it, phase 2 does not ship.

If the gate fails, the copy panels and checklists remain the answer.

## Phase 2: resume-first fill

### What the helper does

`jobhunter apply session <job_id>` launches the `/apply-fill` skill in a restricted Claude Code
session ([isolation](#isolation)). The skill:

1. Runs `jobhunter apply fill-start <job_id>`, which refuses unless the packet is `ready`, the
   posting is not `expired`, no other fill session is open, and today's count is under the cap
   (10, hard cap 25). It prints the packet JSON (answers, drafts, resume path) and a session id.
2. Opens `http://127.0.0.1:8808/apply/{group_id}` in a new tab, so the existing redirect logs
   the click and "Did you apply?" works unchanged. Login wall, account wall or challenge:
   stop, outcome `challenge`.
3. Checks the guard banner is showing ([below](#the-stop-before-submit)). No banner, no filling.
4. **Uploads the resume first** (the packet's chosen export) unless one is already attached,
   and waits for the site's own autofill to settle.
5. **Reads the form** and sorts every field:

| State after the parse | What the helper does |
|---|---|
| Filled, matches resume or bank | Nothing |
| Filled, differs (split job, wrong phone, mangled title) | **Listed** with both values. Not overwritten |
| Empty, and a bank or packet value matches by known field name or an unambiguous label | Filled, then read back |
| Empty free-text question | Listed; the packet's draft, if any, is shown for you to paste or approve |
| Empty, unknown or ambiguous | Listed |
| Salary, EEO and self-ID, citizenship, address, pronouns, consent, certification, signature, any never-stored item | Not touched; listed as **yours** |

6. **Stops.** Never clicks Submit, Apply, Send, Finish or a last-page Next; never presses Enter
   in a text field. Scrolls to the top.
7. Runs `jobhunter apply fill-report <session_id>` with the list (keys, labels and states, no
   values) and prints the same list in chat:

```
Resume parsed: 11 fields filled, 2 differ, 3 filled by jobhunter
Differ (check):  Phone  "5550100" vs "+1-555-0100" · Job 2 title split into two entries
Yours (4):       ? "Why Acme?" (draft ready) · Salary · EEO block · Consent checkbox
```

Field names and label synonyms are data in `apply/ats_forms.py` and `apply/labels.py`, recorded
from fixtures at phase 2 capture and grown like `ats_rules.py`. Anything matched only by the
agent's own reading of a label is listed with a suggested value, never filled.

Page content is untrusted: text on the page or in the posting that asks the agent to submit,
change answers, read files or visit another site is reported, not followed. The tool allowlist
is what makes that hold when the model gets it wrong.

### Isolation

The session mixes a browser, untrusted page text and a coding agent, so it gets as little reach
as possible. What is kept is configuration and one small hook; what revision 1 had beyond that
is cut ([Revision 2](#revision-2-user-direction)).

| Kept | Why |
|---|---|
| **Dedicated Chrome profile `jobhunter-apply`**, signed in to nothing (no Google account, no Gmail). Claude in Chrome and the guard userscript are installed only there | The extension shares the profile's logins. Your everyday profile is untouched |
| **Restricted launch:** `claude --chrome --permission-mode dontAsk --strict-mcp-config --settings .claude/apply/settings.json "/apply-fill <job_id>"` (flags confirmed in the spike) | `dontAsk` denies anything not allowed, so auto mode's classifier approves nothing; `--strict-mcp-config` loads no Playwright MCP server |
| **Allowlist** in `.claude/apply/settings.json` (checked in): Claude in Chrome navigate, read/find, form input, click/type, file upload, screenshot; **not** the JavaScript tool, console or network readers. `Bash(jobhunter apply fill-start:*)`, `Bash(jobhunter apply fill-report:*)`, `Read(<data dir>/packets/**)`. Site rules deny the extension everywhere except the allowed ATS hosts and `127.0.0.1:8808` | In auto mode the extension skips its own site check unless permission rules deny sites, so the rules are the site control |
| **PreToolUse hook** `scripts/hooks/apply_guard.py`, in the apply settings only: denies tools off the list, navigation to other hosts, clicks whose target text matches the submit patterns, and any call in `bypassPermissions`. Fails closed | Catches a loosened rule or a mistaken labelled click |
| **Limits:** one open session, packet must be `ready`, daily cap, no batch/queue/schedule command | One job, one tab, one review |

`fill-start` also refuses unless `JOBHUNTER_APPLY_SESSION=1`, which the launcher sets. That is a
convenience check, not a control: running the skill from your normal session is your choice and
outside these protections, and the skill says to use the launcher.

### The stop before Submit

Best effort, in layers; you are the last one.

| Layer | Stops | Gap |
|---|---|---|
| **Guard userscript** `jobhunter-guard.user.js`, apply profile only, the allowed ATS hosts, `@run-at document-start`. Capture-phase listeners on `window` for `submit` events (covers Enter), clicks on submit-type controls (`button` without a type or `type=submit`, `input[type=submit\|image]`) and on any `button`/`[role=button]` whose text, `value` or `aria-label` matches submit / apply / send / finish (en, es, fr, de, pt). Each calls `confirm("Submit this application? jobhunter listed N fields for you.")` synchronously; Cancel stops the event. Shows a "jobhunter: review, then submit" banner when installed | An unlabelled icon button that submits by `fetch` |
| **Tool allowlist and hook** (above): no JavaScript, no dialog tool, labelled submit clicks denied | A coordinate click carries no target text; the guard's dialog covers that, and the spike confirms the dialog blocks the extension |
| **Skill rules** and the required `stopped_before_submit` outcome | Depends on the model |
| **You** press Submit (and answer the guard's dialog) | |

Event listeners work from a userscript manager's isolated world as well as the page world, so
the guard needs no prototype wrapping, no world-specific install and no self-test.

### Captcha and challenges

Never solved, bypassed or avoided; no simulated mouse or typing cadence. A reCAPTCHA, hCaptcha,
email or SMS code, "verify you are human" page or login wall ends the session (`challenge`) and
the tab is yours. If confirmations from one ATS stop arriving after helped applications, set
`[apply.ats.<name>] fill = false` and use the copy panels for it.

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
  report       TEXT              -- JSON: keys, labels, states. Never values
);
```

## Closing the loop

The packet page's **Open application** and the helper both go through `/apply/{id}`, so the
existing "Did you apply?" prompt ([015](015-apply-links.md#fresh-at-click-time)) appears on
return. *Yes* writes `applied`, sets `resume_version` and `cover_letter_path`, attaches the
rendered files and marks the packet `submitted`. Otherwise the existing mail match proposes the
confirmation as today. A packet `ready` for 7 days with no application event shows on
`/followups` as "ready, not applied?".

## Security and privacy

- **Console.** Still loopback; new `POST` routes inherit the console's same-origin middleware
  (`same_origin_writes`). No route sends `Access-Control-Allow-*`.
- **No credentials** stored or seen. You sign in; the helper stops at login pages.
- **Nothing personal in the repo.** `answers.yaml`, packets and exports live under
  `<data dir>`; fixtures are captured logged out and linted ([Testing](#testing)).
- **Logs** record ids, keys, labels, outcomes and costs, never answer values or document bodies.

| Data | Anthropic API (generation) | Claude in Chrome (phase 2) | Dev agents in the repo | The employer |
|---|---|---|---|---|
| Resume text | yes, contact header replaced | yes (upload, page contents) | as today | yes |
| Posting, fit evidence | yes | page contents | as today | — |
| Answer bank (name, email, links, work authorization) | **never** | yes, the values it fills | no (deny rule) | yes |
| Employer notes | yes (letter, drafts) | no | no | in the letter |
| Salary, EEO, self-ID, citizenship, address | not stored | not stored | not stored | when you type them |
| OpenRouter, Jev, a local scorer | never | | | |

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
| [001](001-goals-and-scope.md#non-goals) | "Private-sector job boards (Indeed, LinkedIn, Greenhouse): ..." | "No *ingest* from private-sector boards. 017 may fetch one posting you paste and, after its gate, help fill one form you chose in your own browser, never submitting." | That non-goal is about crawling for jobs; 017 crawls nothing |
| [015](015-apply-links.md) | "It never fills it in or submits it" | "The Apply button never fills or submits. Filling is a separate packet action (017, phase 2), and nothing in jobhunter submits." | The button keeps its behavior |

## Failure modes

| Failure | Behavior |
|---|---|
| Generator returns invalid JSON or times out | Nothing saved; retry; error on the page |
| Generator invents or inflates a fact | Badged by factcheck; Mark ready blocked until edited or confirmed |
| Overstatement the lexicon misses | Not detected; the cited line beside every line, and the optional entailment pass |
| Over the spend cap | Generate disabled; **Use base resume** still works |
| Pasted posting duplicates a job | Existing group offered |
| Fetch refused by robots or page empty | Paste the text |
| Playwright missing | HTML for print-to-PDF |
| Already applied to this employer and title | Warning before Generate |
| *Phase 2:* guard banner missing | No filling |
| *Phase 2:* parse filled something wrongly | Listed, not overwritten |
| *Phase 2:* ATS changed its form | Fewer known-name matches; more fields listed; fixture refresh bug |
| *Phase 2:* file rejected, extension disconnected | Session stops (`error` / `aborted`); tab left as is |
| *Phase 2:* challenge or login wall | `challenge`; tab is yours |
| *Phase 2:* prompt injection on the page | Reported, not followed; no JS, two commands, reads only packets, allowed hosts only, submit needs you |

## Testing

All offline; `tests/conftest.py`'s network guard stays as is. The Anthropic client is always
mocked.

| Area | How |
|---|---|
| Generator | Canned JSON from a mocked client; request body has placeholders for the contact header and no sentinel answer values |
| Factcheck | Table tests: new employer, changed date, invented number, unlisted skill, invented certification, "contributed" → "led", added team size, added "expert", employer claim without notes or quote, bad posting quote, confirmed line |
| Privacy | Sentinel `answers.yaml` across Stage 2, Stage 3, the decisions scorer and the generator: no value in any request body or log; `.claude/settings.json` deny rules present |
| Answer bank | Load, validate, round-trip save keeps comments, mode `0600`, backup written |
| Routes | `TestClient`: `/apply/new` URL-only, text-only, dedup, robots refusal (mocked `FetchContext`); cross-site POSTs refused |
| Export | HTML render golden file; PDF when the `browser` extra is present (`e2e`) |
| *Phase 2:* fixtures | `tests/fixtures/ats_forms/<ats>/`: rendered DOM of public, empty forms, plus small hand-written pages that mimic a resume-parse prefill, a React-controlled input and a `fetch` submit from a labelled `type=button`. Reader and classifier tested against them |
| *Phase 2:* fixture lint | Fails on emails other than `@example.com`, phone numbers, long tokens in attributes, `<script>` in captured fixtures, hidden inputs |
| *Phase 2:* guard | `e2e` Playwright on the fixtures: dialog on a submit-type button, a labelled `type=button`, a localized label and Enter in a text field; Cancel blocks |
| *Phase 2:* session config and hook | Parse `.claude/apply/settings.json`: no JavaScript tool, no `mcp__playwright__*`, two Bash commands, reads only packets. Hook unit tests on recorded tool inputs, fail-closed |

**Phase 2 acceptance per ATS: one supervised live dry run** on a posting you mean to apply to:
check every field, the upload and the guard dialog (Cancel), then submit it yourself or close the
tab. **Fixture capture** happens once, by hand, in a fresh profile with no ATS sessions or
extensions, direct board URLs only (Greenhouse `/embed/` and Ashby `/api/` are disallowed),
saved before any typing and stripped of scripts, hidden inputs and tokens.

## Phased rollout

Agent-effort estimates, in the style of [009](009-roadmap.md):

| Phase | Work | Effort |
|---|---|---|
| **1a** | Packet table migration, Prepare (`p`, detail button), **New packet** from URL or text (`paste-manual`, dedup, robots-aware fetch), packet page | **1-1.5 d** |
| **1b** | Generator (Opus 5, structured output, caching, spend cap, estimate), factcheck with claim strength and employer claims, cited-line editor, versions, cover letter, employer notes, question drafts, optional entailment pass, `/costs` line | **2-2.5 d** |
| **1c** | Export (PDF, text, markdown; `.docx` if chosen +0.5 d) | **0.5 d** |
| **1d** | Answer bank (model, YAML, `/prefs` section), deny rules, sentinel test, copy panels and checklists (any ATS, USAJOBS, Workday, NEOGOV) | **1 d** |
| **1e** | Loop: Open application via `/apply/{id}`, attachments on *applied*, `/followups` item, `apply stats` | **0.5 d** |
| | *Gate: 4 weeks of use, plus the spike* | |
| **2a** | **Spike**: Claude Code driving Claude in Chrome in a dedicated profile from host or container; launch flags and site rules; guard dialog blocks the extension | **0.5-1 d** |
| **2b** | Fixture capture and lint, form reader and field classifier, field names and label synonyms | **1-1.5 d** |
| **2c** | Apply launch and settings, `/apply-fill` skill, `fill-start`/`fill-report`, hook, `fill_session`, report view | **1-1.5 d** |
| **2d** | Guard userscript and its e2e tests; supervised dry run per ATS | **0.5-1 d** |
| | **Total** | **≈ 8-11 d** (phase 1 ≈ 5-6 d) |

Phase 1 is useful on its own and ships first. Revision 1 was ≈ 11-15.5 d.

## Decisions recorded on 7fa29db

- **Salary, EEO and self-ID, citizenship, address and pronouns are never stored or filled.**
  Reason: your direction; your fill helpers skip them, and dropping them removes the encrypted
  store, passphrase, keyring, `cryptography` dependency and redaction plumbing.
- **`answers.yaml` in `<data dir>/profile/`, mode `0600`, with a checked-in deny rule and a
  sentinel test.** Reason: what is left is non-sensitive; accidental agent reads and leaks to
  models are the remaining risks.
- **Targeted resume, cover letter and New packet are phase 1 and standalone.** Reason: your
  direction; they help on every posting, with or without filling.
- **Resume-first fill: upload, let the site fill, then fill gaps and list differences.**
  Reason: your direction; most ATSs parse the resume, so jobhunter only needs the remainder and
  must not overwrite what the parse did without showing you.
- **Fill guard scaled down** to a dedicated profile, a restricted launch with an allowlist and
  one hook, an event-listener guard script, skill rules and you. Reason: the agent has no
  JavaScript or dialog tool, so its only routes to a submit are clicks and Enter, which those
  cover; the rest of revision 1's machinery mostly served sensitive local fills.
- **Open application and the helper go through `/apply/{id}`.** Reason: the existing click log
  and "Did you apply?" prompt then work unchanged.
- Carried from revision 1: Opus 5 synchronous for generation; claim-strength and employer-claims
  checks with the cited line beside every line; `paste-manual` source; USAJOBS shows evidence
  lines only; deviations from 001 and 015 proposed for approval.

## Open questions

1. **Export format.** PDF only, or also `.docx` (some parsers read it better)? Any visual
   template the variant should match?
2. **Cover letters.** Only when you ask, or drafted with every packet?
3. **Entailment pass.** On by default (about $0.003 per packet)?
4. **Phase 2 gate.** 8 ready packets in 4 weeks to the four ATSs: right threshold?
5. **Phase 2 hosts.** Start with the four public ATSs only, or also help on Workday and NEOGOV
   pages after you sign in yourself?
6. **Phase 2 setup.** Will you create a `jobhunter-apply` Chrome profile with Claude in Chrome
   and a userscript manager? Should Claude Code run on the host or in the dev container?
7. **Sharing with the browser agent.** OK that your resume, the answers it fills and screenshots
   of the form go to Anthropic through Claude in Chrome?
8. **Amend 001 and 015** as in [Deviations](#deviations-from-earlier-specs)?

## Revision 2: user direction

On 2026-10-09 you said: *"The other fill helper tools I use don't enter salary or eeo questions
so not a problem."* *"Most websites now take a resume and fill themselves from it. So the
checker may be more of: if resume, add that first, see what fills, then help with remainder if
needed."* *"Targeted resume tools and cover letter tools may be helpful too."*

| Revision 1 had | Revision 2 | Why |
|---|---|---|
| Sensitive answers (salary, EEO, citizenship, address, pronouns) in the keyring or an encrypted file, passphrase unlock, `Sensitive[T]`, redacted change log, `answers_hash` rules | **Removed.** Never stored, never filled | You answer them by hand |
| Autofill driving the whole form, then reading back | Resume first; fill only gaps; list differences | Sites already fill from the resume |
| Phases: packet, copy-ready, autofill | Phase 1 documents, answer bank and copy panels; phase 2 resume-first fill | Documents are useful everywhere |
| Userscript filler (mode C), token handoff and token routes | **Removed**; copy panels are the no-LLM path | Its main job was filling sensitive fields locally |
| Page-world guard with `fetch`/XHR/beacon wrappers, endpoint allowlists, self-test, isolated-world and real-manager tests | Event-listener guard | The agent cannot run JS or accept dialogs; you are the last layer |
| Repo-wide hook blocking Playwright and ATS navigation during fills | **Removed** | Playwright MCP drives its own Chromium, not the apply profile; the launch is where the controls live |
| Per-employer cap, minimum gap, token limits | One session at a time, daily cap, no batch | Proportionate to one person |
| `did_you_apply()` extension and mail candidate-set boost | Go through `/apply/{id}` | Existing prompt and matching work unchanged |
| Effort ≈ 11-15.5 d | ≈ 8-11 d | |

**Review round 1** (adversarial, 2026-10-09) raised nine critiques. Kept from it: New packet from
a pasted posting with robots-aware fetch, the claim-strength and employer-claims checks with the
cited line beside every line, USAJOBS evidence lines only, the usage gate, a dedicated profile
and restricted launch, fixture capture hygiene and lint, and the recorded deviations. Its
critiques of the sensitive store, mode B/C and the token handoff no longer apply.
