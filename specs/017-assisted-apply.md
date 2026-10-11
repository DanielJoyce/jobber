# 017 — Assisted apply: targeted resume, cover letter, resume-first fill

Status: **design, revision 10, awaiting your approval.** Your open-question decisions of 2026-10-10 are folded in. Bug `7fa29db`. Proposed as a new
milestone M10 ([Deviations](#deviations-from-earlier-specs)). Phase 1a is merged (#85); no other
code yet. Revisions 2 to 10 and why are at the end ([History](#history)). Revision 5 replaced
phase 2's Claude in Chrome design with jobhunter's own Chrome extension and recorded your
drafting, export and cap decisions in phase 1; revision 6 takes the review of revision 5;
revision 7 takes the check of revision 6 and your answers, including coexistence with Jobright;
revision 8 adds [phase 1e, Capture](#phase-1e-capture-from-any-job-page): a **Send to
jobhunter** button in the extension, built first, which also carries the extension's plumbing;
revision 9 takes the review of revision 8; revision 10 takes the check of revision 9.

You asked: *"Is there any way to automate the application part of the process? Filling out web
forms?"* Then you added that most sites now parse an uploaded resume and fill themselves, that
your existing fill helpers already handle forms and skip salary and EEO questions, and that
targeted resume and cover letter tools would help too. So this spec is mostly about the
**documents** and the questions your helpers cannot answer, with form help as a later, smaller
step:

| Phase | What you get | Works on |
|---|---|---|
| **1. Packet** | For any posting (a jobhunter job, a pasted URL, or pasted text): a **targeted resume** built only from facts in your resume, with the source line shown beside every line; a **cover letter** when you ask for one; checked drafts for custom questions; saved custom answers you can reuse; checklists; export | Every posting, with no browser automation at all |
| **1e. Capture** | jobhunter's own **Chrome extension**, first slice. On a job page you are viewing, click **Send to jobhunter**: it reads that one page (structured job data first), and the console shows whether the job is already in jobhunter or adds it. **Score it** shows the estimate and runs only on your confirm; **Prepare packet** opens phase 1 | Any site, only the tab you clicked, only when you click |
| **2. Resume-first fill** (gated) | jobhunter's own **Chrome extension** talks to the local console. On your click it snapshots the form, attaches your targeted resume, lets the site parse it, then diffs the form against the packet: it fills the empty gaps it knows on your click, offers drafts for the unknown questions, and lists what is yours. It **never clicks Submit** and marks where it stops | Public no-account ATS forms (Greenhouse, Lever, Ashby, Workable) first; Workday and NEOGOV later, after you sign in. Built only if the [gate](#phase-2-gate) passes |

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
in phase 2, the field `name`/`id`, and for a choice group its option texts) **before anything
else** at every entry point. Normalization: Unicode case fold, NBSP and runs of whitespace to one
space, required-field asterisks and trailing punctuation dropped. Matching is **by whole word or
phrase** (word boundaries), never substring, so "managed", "language", "design", "generate",
"collaborate" and "trace" do not match `age`, `sign`, `rate` or `race`; those cases are negative
tests. There is one matcher, in Python; the extension never runs its own
([What leaves the machine](#what-leaves-the-machine)).

| Category | Label patterns (examples; the table grows like `ats_rules.py`) |
|---|---|
| Pay | salary, compensation, pay expectation, desired pay, desired wage, base pay, expected CTC, rate, current pay |
| EEO and self-ID | gender, sex, race, ethnicity, hispanic, veteran, disability, sexual orientation, transgender, LGBTQ; option texts "decline to self-identify", "prefer not to say", "I don't wish to answer" mark a whole choice group |
| Identity | citizenship, citizen, national origin, street address, address line, home address, mailing address, current address, zip, postal code, pronouns, date of birth, birthdate, age (except a yes/no "18 or older"), SSN, social security |
| Attestation | consent, certify, certification, attest, signature, sign, acknowledge, "information is true", agree to the terms |

| Entry point | On a match |
|---|---|
| **Save as answer** on a custom question | Refused: "This one is yours; jobhunter doesn't store it" |
| Any `packet_answer` write (one function, `apply/answers.py::save_packet_answer`, the only writer of `packet_answer`) | Raises; nothing written |
| **Promote to /prefs** on a saved packet answer, and the `/prefs` Application answers save | Refused with the same message; nothing written |
| Loading `preferences.yaml` (hand edits included) | The `answers:` model, loaded separately from `Profile`, drops the entry with a warning shown on `/prefs` and the packet page; the rest of `answers:` and the whole profile load normally, so the nightly run is never affected |
| **Question draft** | No generation, no spend; the page says "This one is yours" |
| Phase 2 snapshot (console) | Field descriptors arrive first, without values; the console answers which fields may send values, and drops any value that arrives for a never-store field anyway |
| Phase 2 classify (console) | Action `yours`, evaluated before every other row ([precedence](#field-classification)) |
| Phase 2 extension | Sends values only for fields the console cleared, and refuses any write to a field marked `yours` |

A false positive only means you answer by hand. One test per entry point, plus a vector file
(`tests/fixtures/never_store_vectors.json`: label, options, expected) with the negative cases
above and the EEO and address blocks from the captured fixtures.

## Application answers

A section on the existing `/prefs` page ([014](014-preferences-console.md)), not a new page or file. It uses the
014 conventions: the same form, live preview and save flow, and a `?` help text on each field. It is stored
in the existing preferences file under an `answers:` key. No `answers.yaml`, and no deny rule beyond what
the preferences file already has.

**Holds reusable, non-sensitive answers only:** links (portfolio, GitHub, LinkedIn), notice period,
relocation and remote preference, work authorization as yes/no (only if you want it there), and saved
custom answers such as "why this company" templates.

**The [never-store list](#never-store-list) still applies, at the model level.** The check lives in a
pydantic `Answers` model in `apply/answers.py`, **loaded separately from `Profile`** (which stays
`extra="ignore"` and never sees `answers:`), so a bad entry can never stop scoring or the nightly run. It runs
on every load and every save: a hand edit of `preferences.yaml` in the profile dir, the `/prefs` form save,
and promotion from a packet. On save, a matching answer is refused with a clear message (for example
"'Desired salary' is on the never-store list; answer it by hand") and nothing is written; on load it is
dropped with that warning. The `answers:` key
is not part of any scoring prompt.

**Two stores, no overlap.** **Save as answer** on a packet writes only `packet_answer` (per-packet, source
`user` or `draft`) through `apply/answers.py::save_packet_answer`. A separate **Promote to /prefs** action on a
saved packet answer copies it into `answers:` through the same model validation and the 014 ruamel round-trip
write. The `/prefs` form save is the other writer of `answers:`; there is no single writer of `answers:`,
which is why the check sits in the model.

**Packets read from it**: the packet page offers `answers:` entries to copy next to matching questions. They
are never sent to the model (see Never sent). In phase 2 (your decision) the extension also fills
empty mapped form fields from it, on your click, entirely on your machine
([Field classification](#field-classification), row 6).

## Phase 1: packets

### Flow

```
 Inbox / Detail / New packet        Packet page                              You
 ───────────────────────────        ───────────                              ───
 [p] Prepare, or [n] paste a  ──►   Generate resume v1 (cover letter on request)
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
| 2 | **Generate** the resume variant; **Add cover letter** only when you ask. Runner and estimate shown first; nothing runs until you click | `packet_document` v1, `llm_spend` tier `packet-cli` or `packet` |
| 3 | **Review and edit** beside the base resume, cited line beside each generated line | new `packet_document` versions |
| 4 | **Mark ready.** Enabled only when the **current version's** `check_report` has no unconfirmed item | `status = 'ready'`, exports rendered |
| 5 | **Open application** through the existing `/apply/{id}` redirect, so 015's click log and "Did you apply?" prompt work. Hidden when no URL was given | `apply_click` |
| 6 | You attach the resume, let the site and your helper fill, paste drafts, and submit | nothing |
| 7 | The application reaches `applied` by any path: "Did you apply?" *Yes*, an accepted mail proposal, or a manual event | `application_event` `applied`; the packet is attached by `apply/packets.py::attach_sent_packet`, called on every path ([Closing the loop](#closing-the-loop)) |

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
| Model | `[apply] cli_model = "opus"` (the CLI alias, whichever Opus your subscription serves) and `[apply] api_model = "claude-opus-5"` for the fallback: two keys, because one value cannot be both. A handful per week; wording quality is the point |
| Runner (your decision, 2026-10-10) | **The Claude Code CLI on your subscription first**, the official `anthropic` SDK with `ANTHROPIC_API_KEY` second ([CLI runner](#cli-runner)). `[apply] runner = "cli"` (default, your decision) or `"api"`. With `cli`, when the CLI is missing or fails, **the page offers the API run with its estimate and waits for your click**; it never switches silently |
| API fallback | `messages.stream`, structured output via `output_config`, adaptive thinking, **effort `medium`** (`[apply] effort`) |
| Caching | API fallback only: system prompt + numbered resume as the cached prefix. The 5-minute TTL usually expires while you review, so regenerate is costed uncached |
| Prompt version | `apply-v1`, stored on every `packet_document` with its `runner` (`cli` or `api`) and `model` (the model the response reports) |
| Spend cap | Its own **`[apply] daily_cap_usd = 1.00`**, applied to **API spend**, checked before the call; refused, not truncated, when over. Subscription calls cost no API credit: they are **counted and logged** in `llm_spend` as tier `packet-cli` with `cost_usd = 0` and the CLI's reported tokens, and `/costs` shows their count. Packet tiers (`packet`, `packet-cli`) are excluded from **every** scoring-cap comparison: `screen.remaining_daily_budget`, `weekly_remaining`, `pipeline/backfill.py::estimate` (`spent_today_usd`), the `/costs` totals shown against the scoring caps, the dashboard spend KPI and its `over_cap`, and `inbox.weekly_cost`. `/costs` shows packet spend on its own line against its own cap. One test per site |

#### CLI runner

```
claude -p --output-format stream-json --verbose
       --model opus --effort medium            (no --effort for the Haiku entailment call)
       --system-prompt <jobhunter's apply prompt> --json-schema <output schema>
       --tools '' --strict-mcp-config --setting-sources '' --safe-mode
       --disable-slash-commands --no-session-persistence --max-budget-usd 1.00
```

| | |
|---|---|
| Where | A subprocess with one fixed, empty working directory, `<cache dir>/apply-cli/` (created once, checked empty before each call), so Claude Code's per-project state in `~/.claude.json` gets one entry instead of one per call; the user message (numbered resume, posting, notes) on stdin; timeout 180 s |
| Environment | **Built explicitly, not inherited.** Passed through: `PATH`, `HOME`, `LANG`, `TMPDIR`, `XDG_*`, `CLAUDE_CONFIG_DIR` (where your login lives, if moved), `CLAUDE_CODE_OAUTH_TOKEN` (a subscription token from `claude setup-token`, so subscription auth), and the network settings `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`. Everything else is dropped, so `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` and `CLAUDECODE` (a console started from a Claude Code shell) never reach it. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is set, which turns off Claude Code's telemetry, error reporting and auto-update for the call. Without this, `jobhunter`'s `.env` loading would put the API key in the child, and the CLI would bill the API while we logged $0 |
| Customizations | `--safe-mode` turns off `CLAUDE.md`, skills, plugins, hooks, MCP servers, custom commands and agents while keeping normal auth, so your own Claude Code setup cannot change the prompt or act. `--setting-sources ''` and `--strict-mcp-config` stay as a second layer. The live smoke test confirms it |
| Auth check before the first call of a console process | `claude auth status` (JSON) must report a claude.ai login (`authMethod` claude.ai, `apiProvider` firstParty). Anything else (a third-party provider or a bearer token) turns the CLI runner off with the reason shown. Cached for the process |
| Output | `stream-json` lines, read as they arrive. The `system`/`init` line carries `apiKeySource`, `model`, `tools` and `mcp_servers`. `rate_limit_event` lines carry `rate_limit_info`. The final `result` line carries `is_error`, `structured_output` (the object `--json-schema` validated), `usage` (input, output and cache tokens), `modelUsage` and `total_cost_usd`. jobhunter validates `structured_output` with the same pydantic model as the API path |
| Checks on `init`, before the model is called | `apiKeySource` must be `none`, `tools` must be exactly `["StructuredOutput"]` (Claude Code adds that tool for `--json-schema` after `--tools ''` filtering; any other tool fails), and `mcp_servers` must be `[]`. On a mismatch jobhunter **kills the child at once**, before the request reaches the model, so nothing is billed; the CLI runner is turned off and the page says why |
| Paid usage | If a `rate_limit_event` reports `isUsingOverage` or `overageInUse` (your subscription's extra usage, which is paid), the call is treated as paid: its `total_cost_usd` is logged under tier `packet` against `[apply] daily_cap_usd`, and further CLI calls wait for your click with "subscription is on paid extra usage". If the cap is already reached when overage shows, the child is killed. While no hold is set, CLI calls not confirmed as paid run **one at a time** (a `flock` across the console and the CLI command, waiting up to 240 s), and each re-reads the hold when its turn comes: two documents drafted side by side would otherwise both be charged before either reported paid usage. A call that finds the hold set while it waited runs nothing and asks for the paid click |
| Stopping | Each child runs in its own session (a terminal's Ctrl-C does not reach it), so the console kills every child still running, with its process group, when it shuts down and at interpreter exit; the run then logs what the stream had reported. A hard kill of the console (SIGKILL, the OOM killer) cannot do this, and a request that child finishes is not logged |
| Result | Fails unless `is_error` is false and `structured_output` validates. Otherwise `total_cost_usd` (an API-equivalent figure) is shown on `/costs` for information only |
| Runner off | "CLI runner off" is stored in `<data dir>/apply-runner.json` (reason and time; there is no general key-value table in the database), shown on the packet page and `/costs` with **Turn back on**. It survives restarts until you clear it |
| Same request | `--system-prompt` replaces Claude Code's default agentic prompt, so both runners send jobhunter's system prompt, the same user message and the same schema. `--effort medium` matches the API path for Opus calls; the Haiku entailment call sets no effort on either runner (Haiku rejects it). The CLI may add a small context block of its own; the live smoke test records what reaches the model |
| Not proven yet | That `--safe-mode` keeps your user `CLAUDE.md` and auto-memory out while the subscription login works (`--bare` would skip them, but it cannot use the login), and that `--json-schema` works with `--tools ''`. 1b starts by checking both with the live smoke test, which also checks that `~/.claude.json` does not grow per call |
| Tests | No test runs the real binary. A fake `claude` script first on `PATH` asserts its argv, its cwd, and that none of the dropped variables are in its environment (the pass-through ones are), then prints a recorded `stream-json` transcript (init, rate-limit and result lines, captured once from the real CLI and stored as a fixture). Failure cases: missing binary, non-zero exit, timeout, invalid `structured_output`, `apiKeySource` not `none` (child killed before any result line is read), `tools` other than `["StructuredOutput"]`, an MCP server, overage reported, `auth status` not claude.ai. An **autouse conftest guard** makes the runner's binary lookup raise unless a test installed the fake, so a route test that forgets it cannot exec the real CLI; the guard has its own test. One `@pytest.mark.live` smoke test (opt-in, `JOBHUNTER_LIVE_TESTS=1`) runs the real CLI on a tiny schema and checks the envelope fields above |
| Command | `jobhunter apply draft <packet_id> [--letter] [--question "<text>"]` runs the same generator from the shell and writes the same `packet_document` rows. This is the canonical name; 018 aligns to it for container users, who run it on the host where the CLI and your login are |

**Sent** (the same request body on either runner): your base resume as numbered lines (`L1`...`Ln`), header included, exactly as Stage 2
already sends it ([008](008-compliance.md#personal-data)); `current_focus`, `done_with` and
`narrative.want`; the posting's title, employer and text; when the job is scored, the verified
evidence quotes, `tailoring_hints` and Stage 3 `requirement_gaps` ([006](006-fit-scoring.md));
for a cover letter or "Why us?" draft, your employer notes; for a behavioral draft, your story
facts.

**Never sent:** saved answers (`packet_answer` values other than notes and story facts), everything under
`answers:` on `/prefs` (links, notice period, work authorization, templates; it is shown to you to copy and, in
phase 2, filled locally by the extension on your click), salary
preferences, filters and weights, other jobs, application history, email content.

The model returns structure, not prose, so every line can be checked. The resume and the cover
letter are **separate calls with separate schemas** (`apply/schemas.py`: `ResumeOut`,
`LetterOut`, `DraftOut`):

```json
{"resume": {
   "summary":  {"text": "...", "sources": ["L4", "L12"]},
   "sections": [{"heading": "Experience", "entries": [
       {"source_line": "L10", "employer": "...", "title": "...", "dates": "...",
        "bullets": [{"text": "...", "sources": ["L14"]}]}]}],
   "skills":   [{"name": "...", "sources": ["L31"]}],
   "omitted":  ["L18", "L19"],
   "change_notes": ["Moved the migration bullet first: the posting asks for ..."]}}

{"cover_letter": {"paragraphs": [
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

**Advisory entailment pass** (`[apply] entailment_check`, **default on**, your decision): Haiku
(`--model haiku` on the CLI runner, `claude-haiku-4-5` on the fallback) says per line whether the
cited lines support it (`yes` / `partly` / `no`). Advisory only: it never clears an
**unsupported** badge. Free on the subscription; about $0.003 per packet on the API.

**Editing** is structured, so citations survive. The variant sits beside the base resume, with
`omitted` lines restorable in one click and the model's `change_notes`. Each summary, bullet,
skill and paragraph is its own edit box that keeps its `sources`; moving an item keeps them.
A new item needs a picked source line or **This is true**. A confirmation carries forward to the
next version only for an item whose text is unchanged. **Save** writes a new version
(`origin = 'edited'`); nothing is overwritten. **Regenerate** takes an optional one-line
instruction ("shorter", "lead with the security work"). **Use base resume** is always there and
costs nothing.

### Cover letter and question drafts

**On request only** (your decision): a packet has no letter until you click **Add cover
letter**. Before generating, the packet page asks for one to three lines of
**employer notes** in your words (why this employer, any history), numbered `N1`-`N3`. Blank is
fine; the letter then says nothing about the employer beyond posting quotes. Same runner,
checker, editor and versions, but a **letter-only call** (`LetterOut`): it reads the packet's
current resume version as context and never creates a new resume version, so a reviewed resume
stays reviewed. Adding a letter to a `ready` packet returns it to `draft` until the letter's own
`check_report` passes, because `ready` means every document in it is checked.

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

No `.docx` (your decision): PDF, text and Markdown cover it.

Files go to `<data dir>/packets/<packet_id>/`, named for employers as
`<First>-<Last>-Resume.pdf` and `-Cover-Letter.pdf`. `<data dir>` is
`settings.paths.data_dir`, which since `e7da19f` (#81) defaults to `$XDG_DATA_HOME/jobhunter`;
code resolves it through settings, never a repo-relative path.

Review follow-ups (019561b): a versioned file (`resume-v3.md`) is trusted as v3's export only
when its Markdown and HTML match the current v3 (after a database restore a new v3 can reuse an
old v3's number); otherwise nothing is offered or attached until you press Export again. A
question draft's **Copy** is gated like Export: hidden while a sentence is unsupported. A packet
a merge abandons (nightly dedupe, cross-state, **Link them**) loses its employer-named copies
right after the merge, not when its page is next opened.

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

On the CLI runner a packet costs no API credit; it uses your subscription's allowance. The
figures below apply to the API fallback: Opus 5 at $5 / $25 per million input / output tokens
(`scoring/scorers.py`); thinking tokens bill as output.

| Item | Input | Output (incl. thinking, effort medium) | Cost |
|---|---:|---:|---:|
| Resume variant | ~8,000 | ~3,000-5,000 | **≈ $0.12-0.17** |
| Regenerate (cache usually expired) | ~8,000 | ~3,000-5,000 | ≈ $0.12-0.17 |
| Cover letter, on request | ~9,000 | ~1,200-2,000 | ≈ $0.08-0.10 |
| One question draft | ~5,000 | ~600-1,000 | ≈ $0.04-0.05 |
| Entailment pass (Haiku) | | | ≈ $0.003 |
| **Typical packet** (generate, regenerate, two drafts, no letter) | | | **≈ $0.35-0.45** |

On the fallback, five packets a week is about **$2-3/week**, under the `[apply] daily_cap_usd` of
$1.00. These are estimates: phase 1b replaces them with `count_tokens` on your real resume and a
real posting, and once there is history the estimate uses the median `packet_document.cost_usd`
for the same `kind` on the API runner (not `llm_spend`, whose daily rows mix kinds). The button
says which runner will run ("subscription" or "API, ≈ $0.15"). Whether the subscription has
allowance left is only known by running it; a failed CLI run leads to the API offer, not to an
automatic API call. Scoring a pasted posting is the normal Stage 2 cost, shown
separately and charged to the scoring caps.

### Data model

Phase 1a's migration is `0029_application_packet.sql` (branch `bug/288631b-apply-1a`, with
`apply/packets.py`, `apply/paste.py`, `apply/score.py`, `core/manual_sources.py` and the
`/packet/{id}` routes); the DDL below matches it. Phase 1b adds `NNNN_packet_runner.sql`:
`ALTER TABLE packet_document ADD COLUMN runner TEXT CHECK (runner IN ('cli', 'api'))`
(NULL for `base` and `edited` versions).

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
`applied` or later. `llm_spend` gets tiers `packet` (API, charged) and `packet-cli`
(subscription, `cost_usd = 0`), both on their own `/costs` line. No new
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

## Phase 1e: Capture from any job page

You asked (2026-10-10): *"for the chrome plugin it might nice if I am on a job site viewing a job
and it's not in my system I can click a button to get the job description sent to jobhunter and
score it."* Phase 1e is that button. It is the first slice of the extension, needs no gate, and is
the plumbing spike for phase 2: the manifest, pinned ID, pairing, `/ext/v1/` security, versions
and the Playwright harness all land here, on a feature with no form writes, so phase 2 starts
from a working, tested channel.

**The rule that shapes 1e (revision 9): the extension reads, the console decides.** The
extension collects raw facts from the one page (JSON-LD objects as parsed, microdata, page text,
selection, URLs) and sends them. Mapping, URL choice, dedupe, and whether a capture is added at
once or previewed first are decided in tested Python (`apply/capture.py`), as classification is
in phase 2.

### What it does

```
 Any job page (your tab)          Extension                         Console (127.0.0.1:8808)
 ───────────────────────          ─────────                         ────────────────────────
 icon / Alt+Shift+J (opens the    activeTab granted for this tab
 popup), or right-click menu      host on the off list? stop
                                  executeScript(capture/extract.js)
                                  ◄── one plain object (raw facts)
                                  worker: POST /ext/v1/capture ──►  map, choose URL, then in
                                                                    one BEGIN IMMEDIATE:
                                                                    ├ found    → existing
                                                                    ├ unsure   → preview (nothing written)
                                                                    └ clear    → added (paste-manual,
                                  popup shows the result     ◄──      normalized, never scored)
 [Score it] → estimate + notice → [Confirm, spend ≈ $0.002] ──────► 202; console scores in the
                                  popup polls /status        ◄──      background (score.py)
 [Prepare packet]  ───────────────────────────────────────────────► packets.prepare → /packet/{id}
```

| # | Step | Writes |
|---|---|---|
| 1 | You click the toolbar icon or press `Alt+Shift+J` (declared as the `_execute_action` command, so both open the popup), or use the right-click **Send to jobhunter** item. Each is a user gesture, so Chrome grants `activeTab` for this tab | nothing |
| 2 | The worker reads the tab's URL (readable under `activeTab`). If its host is on the off list, or is LinkedIn or Indeed and you have not accepted the [one-time notice](#capture-compliance), it stops here, **before** any script runs in the page ([Popup state](#popup-state-and-triggers)) | nothing |
| 3 | The worker injects `capture/extract.js` into the top frame ([Extraction](#extraction)); it returns one plain object | nothing |
| 4 | `POST /ext/v1/capture` with a fresh `action_id`. The console maps and checks the facts, then decides ([Add or preview](#add-at-once-or-preview)) | a `paste-manual` group and `capture_log`, or only `capture_log` |
| 5 | The popup shows the result: title, employer, location, salary as stated, posted date, description length and first lines, the method, and **Already in jobhunter → Open** (with bucket and verdict when scored), **Added → Open**, **Looks like #123 → Open, Add this description, or Add as new**, or the **preview** to check and edit before **Add** | nothing |
| 6 | **Score it** on a group that is not scored: the estimate with the scorer's privacy notice, then **Confirm** ([Score it](#score-it)) | as Score this group now |
| 7 | **Prepare packet**: `packets.prepare`, then the packet page opens in a new tab | `application` at `preparing`, `application_packet` |

**Never automatic.** Capture adds; it never scores, prepares, fetches or re-scores anything.
Scoring is step 6 only, behind an estimate and your confirm, exactly as **Score this group now**
on the packet page. A captured group is marked **scored on request** on the group itself
([Scored on request](#scored-on-request-a-group-flag)), so the nightly prefilter, screen and
re-score plans never pick it up, whichever job is its canonical. A later nightly copy of the same
posting joins it without a second paid score ([Later ingest](#a-later-ingest-of-a-captured-posting)).

### Permissions: `activeTab` is now allowed

| Permission | 1e | Why |
|---|---|---|
| `activeTab` | **yes (new)** | The narrowest way to read a page on *any* site: access to **the one tab you acted on**, granted by your click, shortcut or menu choice, and **gone when the tab navigates to another document or closes**. No install warning, no standing access, nothing on page load. The alternative, `<all_urls>`, would let the extension read every page at any time. The grant covers the top frame's origin only, so a cross-origin iframe is never read |
| `scripting` | yes | `chrome.scripting.executeScript` into that tab, under the `activeTab` grant |
| `storage` | yes | The pairing token (trusted contexts only), the LinkedIn and Indeed notice flags (`storage.local`), the per-tab popup state (`storage.session`) |
| `contextMenus` | yes | The right-click **Send to jobhunter** item (contexts `page` and `selection`; **no `link` context**, so capture never follows a link) |
| `host_permissions` | `http://127.0.0.1/*` only | The console. No site hosts in 1e |
| `content_scripts` | **none** | No script runs on any site except the injection after your click |
| Still forbidden | | `<all_urls>` and any host wildcard, `tabs`, `webNavigation`, `cookies`, `webRequest`, `declarativeNetRequest`, `debugger`, `tabCapture`, `desktopCapture`, `downloads`, `history`, `clipboardRead`, `notifications`, `externally_connectable`, `web_accessible_resources`. The manifest lint checks the exact list |

`tabs.onRemoved` (used to clear popup state) needs no permission. Phase 2 adds `sidePanel`, the
ATS hosts and their content scripts on top of this list ([Phase 2 architecture](#architecture)).

### Popup state and triggers

The worker keeps one entry per **capture key** = tab id + the page URL with tracking parameters
removed, in `chrome.storage.session`: state (`in flight`, `result`, `error`), the current
`action_id`, the `documentId` from the `InjectionResult`, the group and job ids of the answer, and
the time. The tracking-parameter list is not copied into JavaScript: `POST /ext/v1/version`
returns it (from `dedupe_url._TRACKING_PARAMS` and the `utm_` prefix), so it cannot drift.
Entries hold **no description or preview text** (`storage.session` has a 10 MB quota): a preview
lives in the open popup only, and reopening a preview captures again, which is free because a
preview writes nothing. At most 50 entries are kept, oldest dropped. Entries for a tab are
dropped on `tabs.onRemoved`.

| Trigger | What happens |
|---|---|
| **Popup opens** (icon, shortcut, or `openPopup()` after a menu capture) | The worker reads the tab URL and looks up the key. An entry **in flight** (for at most 60 seconds; older counts as lost and is resent with the same `action_id`), or a result less than 10 minutes old, is shown, refreshed from the console with `POST /ext/v1/groups/{id}/status` when it names a group; **no new capture**. Otherwise it captures. The popup always has **Capture again**, which is a new action |
| **Single-page boards** | On LinkedIn, Indeed or Workday search pages, choosing another job changes `currentJobId`, `vjk` or the path by `pushState`, with the same tab and no navigation. The URL, and so the key, changes, so the next open captures the new job. When the page URL names a board id, the console also checks that the captured posting is that job ([Add or preview](#add-at-once-or-preview)) |
| **Right-click menu** | Captures, using `info.selectionText` when the click was on a selection ([Extraction](#extraction)), stores the result under the key, sets the badge (✓ or !), then calls `chrome.action.openPopup()`. The popup finds the entry and shows it without capturing again. If Chrome refuses `openPopup()` from a menu click (a 1e spike item), you click the icon to see it |
| **Two triggers at once** (menu then popup, a double shortcut) | **Single-flight in the worker**: a second capture for a key already in flight waits for the first and shows its result. On the console, every request that can write carries an **`action_id`**, a random UUID the worker mints **per user action** (a capture, Add, Add as new, Add this description, Link them, Capture again, a menu selection after a popup capture). It is reused only when the worker resends the same request after losing the answer. The console looks it up inside the same `BEGIN IMMEDIATE` as the write: a repeat of a write action returns its stored answer and writes nothing; a repeat of a capture that wrote nothing (preview, `possible`, `same_job`) is simply evaluated again. Duplicate inserts are prevented by the dedupe running under that same lock, not by the id ([Routes](#routes-and-actions)) |
| **Phase 2, a supported ATS frame in the tab** | The popup does **not** capture on open: it asks the frames first, and when one answers it shows **Fill this form** and a **Send to jobhunter** button instead. So phase 2's "the top page's content is not read" holds unless you press that button |

The worker does every console request, so closing the popup does not cancel one. Every `/ext/v1/`
route answers within **20 seconds** (Chrome stops a service worker whose `fetch` waits more than
30 seconds); work that can take longer returns 202 and runs on the console ([Score it](#score-it)).
If the worker is stopped anyway, the console's own state is the truth: the popup rebuilds from
`status`, not from what the worker remembered.

**Fail closed.** With no paired console, or `version` unreachable, nothing is injected anywhere
(there would be nowhere to send it). The LinkedIn and Indeed notice hosts are also built into the
extension, so the notice applies even before the first `version` answer. Until the notice for a
host is accepted, a menu click there discards `info.selectionText` and sends nothing.

### Extraction

`capture/extract.js` is **one file whose whole body is a single arrow-function expression,
called at once** (`(() => { ... })()`), so it declares nothing at the top level and leaves
nothing in the isolated world. A second injection into the same document (a repeat click) or
phase 2's content scripts in the same world cannot collide with it. The site table lives inside
it. A lint test parses the file and fails on any top-level declaration. It runs in the isolated
world, top frame only, and its result is the value of that expression.

It **collects raw facts only**; the console maps them:

| Fact | Collected as |
|---|---|
| **JSON-LD** | Every `script[type="application/ld+json"]`, parsed with `JSON.parse` in a `try`; every object whose `@type` is or includes `JobPosting`, found in arrays and `@graph`, is sent **as parsed**, up to 5 objects, each re-serialized and capped at 400,000 characters (a larger one is dropped and flagged). The extension does not pick one or read its fields |
| **Microdata** | For an element with `itemtype` `schema.org/JobPosting`: each `itemprop`'s `content` attribute or text, as name and value pairs |
| **Site extractor** | A table inside the file: host suffix → selectors for title, employer, location and description. **Empty at launch.** A row is added only for a site you capture from that has no structured data, with a fixture and a test. No rows for LinkedIn or Indeed ([Compliance](#capture-compliance)) |
| **Page text** | From `main`, else `article`, else `[role=main]`, else `body`: a **recursive walker** over the live tree with no DOM writes. It skips `nav`, `header`, `footer`, `aside`, `form`, `dialog`, `script`, `style`, `template`, `input`, `textarea`, `select` and any element for which `checkVisibility()` is false. It takes text node data from what remains and adds line breaks at block elements. Hidden text and form values are never collected. Plus `og:title`, `og:site_name` and `document.title` |
| **Selection** | `getSelection().toString()` in the top frame, at least 40 characters, ignored when inside an editable element. From the menu, `info.selectionText` instead, which also covers a selection made inside a cross-origin iframe (plain text; Chrome may collapse line breaks) |
| **URLs** | `location.href`, `link[rel=canonical]`, and the `src` of every iframe with an http(s) source (at most 10) |
| **Apply control** (LinkedIn and Indeed only) | The one board rule: in the job details, the apply control's kind, read from its text and `aria-label` (**Easy Apply** / Indeed's **Apply now** → `easy_apply`; **Apply**, **Apply on company site** → `offsite`; anything else `unknown`) and, when it is an `a`, its `href` attribute. Read only: never clicked, focused or hovered ([below](#linkedin-and-indeed-the-apply-control)) |
| **Trigger** | `menu-selection`, `menu-page`, or `popup` |

**Size limits in the extension**: page text and selection 100,000 characters
(`detail.MAX_PASTE_CHARS`), other strings 1,000, anything cut flagged `truncated`. The console
enforces its own limits (pydantic constraints) and refuses a body over 1 MB with 413.

**Read-only, proved by behavior, not only by grep.** The extraction tests record a main-world
`MutationObserver` and `document.documentElement.outerHTML` before and after on every fixture and
assert no change. A lint also fails on DOM-writing and event APIs in `capture/` (`appendChild`,
`append(`, `prepend(`, `before(`, `after(`, `insertBefore`, `insertAdjacent`, `remove(`,
`removeChild`, `replaceChild`, `replaceChildren`, `replaceWith`, `setAttribute`,
`removeAttribute`, `toggleAttribute`, assignment to `textContent`, `innerText`, `nodeValue`,
`data`, `value`, `checked` or `hidden`, `classList`, `style.`, `addEventListener`, `setTimeout`,
`setInterval`, `MutationObserver`, `.click(`, `focus(`, `scrollIntoView`, `removeAllRanges`,
`dispatchEvent`, `fetch`, `XMLHttpRequest`, `WebSocket`, `sendBeacon`, `window.open`,
`location =`) and on any `.value` or `.checked` read (the extension no longer needs them, since
JSON-LD fields are read in Python).

**Pages it cannot read**: `chrome://`, the Chrome Web Store, Chrome's PDF viewer (many government
postings are PDFs) and other restricted pages make `executeScript` fail. The popup says so and
offers **Open New packet** with the tab's URL filled in (`/apply/new?url=...`, a GET that only
pre-fills the form; 1e adds the parameter).

**Embedded forms**: the ATS iframe on an employer page is not read (`activeTab` covers the top
frame only). When the page gave under 200 characters of description, the console looks at the
reported iframe sources and, for one that `match_ats` maps to a supported ATS over https, offers
**Fetch from job-boards.greenhouse.io**: 1a's **Fetch posting text** on the direct board URL,
through `FetchContext` and robots.txt, on your click (`POST /ext/v1/capture/fetch`, which
**re-checks `match_ats` and https on the server** and refuses anything else with 422, and gives
up with "slow; select the text instead" after 15 seconds). For Ashby, whose robots.txt disallows
the fetch, the popup says **right-click your selection, then Send to jobhunter**, which works
through `info.selectionText`.

### Console side

`apply/capture.py`, with pydantic request and response models (`extra = "forbid"`, size limits
as field constraints) exported to `extension/api/v1.schema.json`.

1. **Map.** From the JSON-LD `JobPosting` chosen below (or microdata, or the site row): title,
   `hiringOrganization.name`, locations (each `address`; `TELECOMMUTE` adds "Remote"),
   `baseSalary` as stated text ("USD 120,000-150,000 per YEAR" style, which `normalize_job`'s
   salary parser reads), `datePosted`, `validThrough`, `employmentType` mapped to
   `models.EmploymentType` (`FULL_TIME`, `PART_TIME`, `CONTRACTOR`, `TEMPORARY`, `INTERN`,
   `PER_DIEM`, lists; anything else `unknown`). The description HTML (decoded once more when it
   is entity-encoded and holds no tags) goes through `core/textnorm.html_to_text` (selectolax
   parsing, no rendering) and is stored the way 1a stores pasted text, `detail.pasted_html(text)`.
   **Captured HTML is never stored and never rendered**, in the console or in the popup (which
   uses `textContent` only). Page-text title and employer: `og:title` or `document.title` with
   the 1a `guess_from_page_title` rule; `og:site_name` is **ignored on board hosts** (LinkedIn,
   Indeed, and a short list in `capture.py`), where it names the board.
2. **Choose the URL.** A JSON-LD `url` is resolved against the page URL, then used only when it
   **identifies one posting** (`dedupe_url._identifies_job`, or the ATS rule's `is_posting`) and
   its host is the page's host or a supported ATS host. Else the canonical URL under the same
   test, else the page URL. Then `paste.direct_board_url`. **Query parameters are kept**, minus
   the tracking ones `normalize_apply_url` already drops (`utm_*` and `_TRACKING_PARAMS`):
   many career systems carry the posting id only in the query (`jobId=`, `sjobid=`, `j=`,
   `JobId=`, `adid=`, Taleo `job=`, PeopleSoft `JobOpeningId=`, SuccessFactors
   `career_job_req_id=`, UKG `opportunityId=`). A URL is reduced to its id only for LinkedIn and
   Indeed (`board_key`) and for ATS rules that define one.
3. **Choose the posting.** With several JSON-LD postings, the one whose `url` matches the page
   URL, the canonical URL or the page's board id. On a page whose URL names a board id
   (`currentJobId`, `vjk`), a posting whose `url` and `identifier` do not contain that id is
   **stale** (a single-page board that kept the first job's JSON-LD) and is not used.
4. **Locations now.** Inside the insert transaction, `locations.derive_locations` plus the
   `job_locations` write for this one job (what the nightly `apply_locations` would do), so the
   first bucket and the scorer's "Location:" line are right without waiting for a nightly run.
   `insert_pasted_posting` gains optional `salary_raw`, `location_raw`, `posted_at`,
   `closes_at` and `employment_type` (default `None`, so `/apply/new` is unchanged).

#### Add at once or preview

The console, not the extension, decides. **Added at once** (one click) only when all hold:

- the description came from JSON-LD, microdata or a site row and is at least 200 characters,
  or the trigger was `menu-selection`;
- the title and employer came from structured data or a site row (a menu selection on a page
  without them is previewed, so you can type them);
- exactly one posting was chosen in step 3, and it is not stale (on a page with no board id, a
  single posting whose `url` identifies a posting but matches neither the page URL nor the
  canonical URL counts as stale: a single-page app may keep the first job's JSON-LD);
- dedupe found no candidate.

Otherwise the answer is a **preview**: no job, group or link is written, only a `capture_log`
row (`previewed`). The preview shows the console's cleaned text in an **editable text box**, with
editable title and employer, a picker when there were several postings, **Use the page's
description** or **Use my selection** when both exist (a selection from the icon or shortcut is
never applied silently: a triple-clicked paragraph must not replace a full description), and
**Use a selection instead** (select on the page and right-click). **Add** is a new action
(`capture/add`, [Routes](#routes-and-actions)) carrying the edited fields.

#### Dedupe on capture

`paste.find_duplicates` (normalized URL, or `employer_norm` + normalized title), plus a **board
id** key from `apply/capture.py::board_key(url)`, applied to **both sides** inside
`find_duplicates`:

| Board | Forms recognized | Key |
|---|---|---|
| LinkedIn (`*.linkedin.com`) | `/jobs/view/<id>`, `/jobs/view/<slug>-<id>`, `/comm/jobs/view/<id>`, `currentJobId=<id>` on `/jobs/search`, `/jobs/collections` | `linkedin:<id>` |
| Indeed (`*.indeed.com`, country hosts) | `jk=<id>` or `vjk=<id>` on any path | `indeed:<id>` |
| Everything else | `normalize_apply_url` as today (tracking parameters dropped, others kept) | its normalized URL |

The chosen URL **and** the page URL are both looked up. `dedupe_url.normalize_apply_url` is
not changed, so nightly URL merges keep their keys. The JSON-LD `identifier` is **not** a dedupe
key (no column holds it, and requisition ids like `R12345` recur across employers); it is used
only in step 3.

**The `action_id` lookup, the check and the insert run in one `BEGIN IMMEDIATE` transaction**
(`db.transaction`; `insert_pasted_posting` opens none of its own), with the `capture_log` row, so
two concurrent captures of the same posting cannot both insert, whatever their ids. A test fires
two identical captures with different `action_id`s at once and expects one group. Board ids
resolve through `job_board_ref` to the job's current group; a row whose job is not grouped yet
(`job.job_group_id` NULL, between ingest and grouping) is skipped, and `group_pending` handles it
([Later ingest](#a-later-ingest-of-a-captured-posting)).

| Match | Response | Writes |
|---|---|---|
| Same board id, or same URL **that identifies one posting** | `existing`: the group, title, employer, bucket and verdict when scored, live packet if any | `capture_log` |
| Same URL that does **not** identify one posting (a careers home), or same employer and title only | `possible`: the candidates; the popup offers **Open** or **Add as new** (`capture/add` with `force_new`) | no job, group or link until you choose; a `capture_log` row |
| Several groups | URL matches first, as `/apply/new` lists them | |
| A candidate (`existing` or `possible`) that is a **manual** group (`paste-manual` or `email-manual`) with empty `description_text` | **Add this description** as well | on your click (`groups/{id}/describe`), `paste.store_posting_text`, which gains the same optional fields and fills only empty ones; no `description_rev` bump, so no re-score. This is how the 18 `email-manual` groups (URL `gmail:...`, matched by employer and title only) get their posting |
| An ingested group with a partial description | `existing`; the popup links to its job page, where pasting queues the usual nightly re-score (012). Capture never does that itself, because it would spend | nothing |

"No description" means empty `description_text`, never `description_completeness` (the
`email-manual` rows carry the column default `full`).

#### Scored on request: a group flag

1a keys "scored only on request" on the **canonical job's** `source_key`. That breaks once a
captured group gains members: `dedupe._refresh_group` picks the canonical by completeness and
length, and every ingested job in your database is `full`, so a full ingested copy would become
canonical and the nightly screen would pay for a posting you chose not to score; a partial copy
(a LinkedIn alert snippet) leaves the pasted job canonical and the group is never scored even
when it should be. 1e moves the rule to the group:

- `NNNN_group_score_on_request.sql`: `ALTER TABLE job_group ADD COLUMN score_on_request INTEGER
  NOT NULL DEFAULT 0`, backfilled to 1 for groups whose canonical job is `paste-manual` or
  `email-manual`. `insert_pasted_posting` and the `email-manual` creation set it.
- The flag, not the source, is what the nightly code checks: the prefilter
  (`prefilter.py`'s `j.source_key != 'paste-manual'`), `screen._ELIGIBLE` (`:only IS NOT NULL OR
  g.score_on_request = 0`, replacing the `MANUAL_SOURCES_SQL` test), re-score plans (which build
  their own group lists and must exclude flagged groups), and the console's "pasted posting"
  checks (`detail`, `packets`). `MANUAL_SOURCES` stays only for grouping and display (the
  dashboard's **Pasted** node).
- **Canonical picks content, not scoring.** `_refresh_group` keeps choosing the best text;
  whether the group is scored nightly is the flag alone. **Score it** (`only = {group}`) works on
  a flagged group whichever job is canonical.

Tests: a `full` ingested copy joins a flagged, unscored, application-free group and the daily run
scores it once (the flag was cleared, below); the same copy joins a flagged, scored group and the
daily run makes no `llm_spend`; a partial copy joins an unscored captured group and **Score it**
still works.

#### A later ingest of a captured posting

Without a rule, a posting you captured and later receive from a mail alert, a state bank or NLx
becomes a second group and is scored again. 1e lets `dedupe.group_pending` match ingested jobs to
**`paste-manual` jobs** as well, by its existing tests plus two keys:

| Match | How |
|---|---|
| URL key | The ingested job's URL or apply URL, by `normalize_apply_url`, equal to the captured job's, **only when the key identifies one posting** (`_identifies_job`); the captured job's page URL and chosen URL are both tried |
| Board id | The ingested job's `board_key`, resolved through `job_board_ref` to **any** group (not only captured ones), and through stored URLs |
| Near duplicate | `group_pending`'s existing test (title similarity at least 92 and description shingle Jaccard at least 0.85 within the state and employer block) now also considers captured jobs as candidates. This is what catches NLx, whose jobs carry `usnlx.com` and `nlx.jobsyn.org` URLs (1,002 of 1,065 in your database), not the employer's |

Employer and title alone never join. On a match the ingested job joins the captured group:

| The captured group | Result |
|---|---|
| Is scored, **or has a live Score now claim or an open batch item**, or has an application or packet | Joins as a member; `score_on_request` stays 1, so **no nightly score and no second payment**; `_refresh_group` may make the fuller ingested text canonical for display |
| None of these | Joins, and `score_on_request` is cleared, so the nightly run scores the group **once**, as it would have without the capture |

`430a0e7`'s parser looks up `job_board_ref` first and inserts with `ON CONFLICT DO NOTHING`, so
an alert for a LinkedIn id that a capture already recorded joins that job's group instead of
failing. Tests: capture, Score it, then ingest the same posting (a mail-alert fixture by board
id, an NLx-shaped fixture by near duplicate, a state-bank fixture by URL) and run the daily run:
one group, one `fit_score`. The same without Score it: one group, scored once. A careers-home URL
shared by two postings joins neither.

#### LinkedIn and Indeed: the Apply control

You noted (2026-10-10) that LinkedIn lets you apply on its own site only through **Easy Apply**;
otherwise its Apply button sends you to the employer's ATS (Ashby, Greenhouse and so on). So a
LinkedIn posting is often "really" a Greenhouse or Ashby posting jobhunter may already have, and
LinkedIn alert emails (bug `430a0e7`) carry only linkedin.com links, which jobhunter never
fetches. Capture is where the two are joined. Indeed works the same way (Indeed Apply versus
**Apply on company site**).

**Reading the control.** The [Apply control](#extraction) fact is the one LinkedIn and Indeed
rule in `extract.js`, kept to that control: no other selectors for those sites, nothing clicked.
The console decides what the `href` means, locally:

| `href` | Result |
|---|---|
| A direct http(s) URL off the board's hosts | The destination |
| A board redirect wrapper with the destination in a query parameter (LinkedIn's `/redir?url=`, already a `Redirector` in `ats_rules.py`; other wrappers such as an `externalApply` path with `url=` are added as `Redirector` rows with a fixture once seen) | Decoded by `ats_rules.unwrap`, **never requested**: jobhunter does not fetch linkedin.com or indeed.com, not even a redirect |
| Absent (a `button`; the destination is only revealed by clicking), or decodes to another board URL | No destination; see [click-through linking](#click-through-linking) |

**Board hosts are never fetched, by any path.** `linkedin.com` and `indeed.com` (all their
hosts) go on a no-fetch list in `pipeline/applylink.py`: `resolve_group` and the 015 refresh skip
them without a request, not even for robots.txt. Today `resolve_group` would send a robots.txt
request to linkedin.com for a group whose apply link starts there, which this fixes; an
`apply_link` starting on a board host stays `unresolved` until a capture supplies the
destination.

The result is stored in `job_board_ref` ([Data](#data)): the board, its job id, `apply_mode`
(`easy_apply`, `offsite`, `unknown`) and the decoded `apply_url`. **Easy Apply** is recorded so
phase 2 knows there is no external form to fill (the packet checklist says "apply on LinkedIn";
the extension never acts on linkedin.com).

**Dedupe with the destination.** `find_duplicates` also looks up the decoded `apply_url`
(`normalize_apply_url`, ATS rules included) and the board id through `job_board_ref`. Then:

| What matched | Response | Writes |
|---|---|---|
| The ATS URL matches a group (an ingested Greenhouse posting, a pasted one), the URL **identifies one posting**, and the titles agree (equal after `normalize_title`, or similarity at least 92) | `existing`: "This LinkedIn job is *Title* at *Employer*, from Greenhouse" | `job_board_ref` mapping the LinkedIn id to that group's canonical job (`linked`) |
| The ATS URL identifies one posting but the titles disagree, or it is a generic careers path on the employer's site (not an ATS posting URL) and the titles agree | `same_job` offer, on your click only | nothing until you click |
| A careers home (LinkedIn's off-site link often is), or a generic careers path whose titles disagree (one shared apply path would otherwise offer every later job of that employer, 56d7fcf) | no match; added or previewed as usual | as usual |
| The LinkedIn id matches a group (an alert group from `430a0e7`, or an earlier capture) | `existing`; the group's `apply_link` is written from the decoded destination when it has none or its row is `unresolved` or `blocked` (never over a `live` or `expired` one; `status = 'unresolved'`, no request made), so its Apply button goes straight to the ATS | `job_board_ref`, `apply_link` |
| Both, and they are **different groups** | `same_job`: both shown, **Link them** on your click ([below](#linking-two-groups)) | nothing until you click |
| Neither | added or previewed as usual, with the `job_board_ref` row; when the destination is a supported ATS, the popup also offers **Fetch from greenhouse.io** (1a's Fetch posting text, robots.txt decides) for the employer's own text | the new group, `job_board_ref` |

#### Click-through linking

When the destination is not in the page, you click LinkedIn's **Apply** yourself, the ATS page
opens, and you capture it. The worker knows the LinkedIn capture: its popup state holds captures
from the last 30 minutes, with `apply_mode = offsite` and no destination. With the ATS capture it
sends the most recent such capture from the same window, or from the tab that opened this one
(`openerTabId` and `windowId` need no `tabs` permission). The console checks that the employer or
title roughly agree and answers with an offer: **"Same job as the LinkedIn posting you captured 3
minutes ago (Senior Platform Engineer at Acme)? Link them / Not the same."** Nothing is linked
unless you confirm. **Link them** (`POST /ext/v1/link` with both group ids, the board capture's
`job_board_ref` key and a new `action_id`) sets that `job_board_ref.apply_url` to the ATS URL
and, when the two captures made two groups, links them as below. **Not the same** writes only a
`capture_log` row (`not_same`), so the offer is not made again for that pair. No guessing from timing alone, and no
automatic link.

#### Linking two groups

`apply/capture.py::link_same_job(conn, a, b)`, only on your click (`POST /ext/v1/link`, or
**Link them** on `/job/{id}`), in one `BEGIN IMMEDIATE` transaction. It is built on the nightly
merge code (`dedupe_url._absorb` and `_merge_application`, so packets are re-pointed or abandoned
as in [Merges and undo](#merges-and-undo)). Jobs, applications and `job_board_ref` rows follow
the kept group. No score is redone.

**Refused (409, nothing written) while either group has a live Score now claim or an open batch
item**: the background score would otherwise write to a group the merge deletes, its spend record
would roll back with the failed write, and you would pay again. The popup says "scoring in
progress; link when it finishes".

| Groups | Kept | `score_on_request` after |
|---|---|---|
| Exactly one is scored | The scored one | as the kept group's (so an unscored ingested group absorbed into a scored capture is not scored again) |
| Both scored, or neither, and one is ingested | The ingested one | 0, its own |
| Neither is ingested (two captures, or a capture and an `email-manual` group) | The scored one; else the one with an application or packet; else the ATS capture; else the older | 1 |

This differs on purpose from [A later ingest of a captured posting](#a-later-ingest-of-a-captured-posting),
where an applied capture stays on request: there the ingested job had no group yet, so keeping
the flag prevents a new nightly score. Here the unscored ingested group already existed with flag
0 and was in the nightly queue before the link, so a link of an applied, unscored capture with it
gives flag 0 and the nightly run scores the kept group once, as it would have without the link
(review follow-up 56d7fcf).

After that, the LinkedIn alert email, the LinkedIn page and the ATS posting are one job.

#### Score it

Reuses `apply/score.py`, with the console doing the waiting:

- `POST /ext/v1/groups/{id}/estimate` returns `score.estimate(...)` (scorer, cost per job and its
  source, estimate, remaining scoring cap, batch or not, `refusal`, token) **plus** the scorer's
  `privacy_notice(est.scorer, scoring)` (for Jev, OpenRouter and Decisions scorers it says the
  resume-derived profile and the posting go to openrouter.ai and the model provider; a
  `ScorerError` there becomes a refusal) and the line "It skips your prefilter rules, because you
  chose this posting." The popup shows both above **Confirm**, as `_packet_score.html` does.
- `POST /ext/v1/groups/{id}/score` with the token and an `action_id`. `score.py` splits
  `score_now` into `start(...)` (estimate check and the claim, in its `BEGIN IMMEDIATE`) and
  `run_claimed(...)` (the scorer call, the write or the restore); `score_now` stays their
  composition for the packet page and the CLI. The route calls `start` **synchronously**, so
  409 with the new estimate on `EstimateChanged`, and 409 when another Score now holds the
  claim, come back before anything is spent; then it returns **202** and runs `run_claimed` on
  a **daemon thread with its own connection** from the app's `conn_factory`, as
  `rescore_routes._work` does. A synchronous scorer such as Jev takes up to its 180-second
  timeout, longer than a service worker may wait.
- `POST /ext/v1/groups/{id}/status`, checked in this order: **scored** (`is_scored`: bucket and
  one-line verdict); **submitted** (an open batch item, with the batch id); **in progress** (a
  `user-requested` claim younger than the 15-minute `CLAIM_TTL`, with its start time);
  **stopped** (a claim older than that with no score: "the score did not finish, perhaps
  because the console restarted; you can retry now"); the last error from this console process,
  if any; else **not scored**. After a restart a running score shows **in progress** until its
  claim expires, then **stopped**; the text says so. The popup polls every 2 seconds while open
  and rebuilds from `status` whenever it opens.

The confirm button names the amount ("Confirm, spend ≈ $0.0021") and there is no "don't ask
again". Charged to the scoring caps, like any Score this group now. **Which scorer**: Score it
uses `[scoring] screen_scorer`, as the packet page does. Measured on 2026-10-10: every screen
score in your database (3,296) was made by `jev:typesafe/jev-1.13` through explicit `--scorer`
runs, but your `config.toml` does not set `screen_scorer`, so today Score it would use the
default, an Anthropic Haiku batch on your prepaid credits, with the result after the nightly
collect and on a different scorer's scale from your inbox. The popup names the scorer before you
confirm. On 2026-10-10 you were asked to set `screen_scorer = "jev:typesafe/jev-1.13"`, which
makes captures score immediately on the same scale. Both paths (synchronous and batch) are
specified and tested.

**Prepare packet** is `POST /ext/v1/groups/{id}/prepare` (`packets.prepare`); a group with no
description asks for the text first, as in 1a.

#### Routes and actions

Every `/ext/v1/` route is a POST with the rev 7 checks. Each user action is its own route, with a
fresh `action_id` minted by the worker (reused only to resend after a lost answer) and its own
`capture_log` outcome:

| Route | Action | Writes | `capture_log` outcome |
|---|---|---|---|
| `capture` | the click: map, dedupe, add at once or answer | a group when clear | `added`, `existing`, `linked`, `possible`, `same_job`, `previewed` |
| `capture/add` | **Add** after a preview (edited fields, chosen posting, chosen description), or **Add as new** (`force_new`) | a group | `added` |
| `capture/fetch` | **Fetch from** a supported ATS | nothing; returns text for the preview | `fetched` |
| `groups/{id}/describe` | **Add this description** | the description and empty fields | `description_added` |
| `link` | **Link them** or **Not the same** | `job_board_ref`, merged groups | `linked_groups`, `not_same` |
| `groups/{id}/estimate`, `score`, `status`, `prepare` | Score it and Prepare packet | as above | none |
| `pair`, `version` | pairing, versions, host lists, tracking parameters | `extension.json` | none |

A repeated `action_id` on a write route returns the stored answer; on `capture` with a non-write
outcome it is evaluated again.

**Gone groups.** A group id the popup holds may be merged away by a nightly run or by **Link
them**. Group routes then answer **404** with `{"gone": true, "current_group": <id or null>}`,
where `current_group` is the group the capture's job (kept in `capture_log.job_id` and in the
popup entry) now belongs to. The popup follows it, or says the posting was removed.

#### Where captured jobs show

A captured group has no score and no application until you act, so the inbox (scored groups)
does not show it yet. 1e adds:

- **`/job/{id}`** for a group with `score_on_request` gets **Score this group now** (the same
  estimate partial as the packet page, notice included) and **Prepare**, and shows any `same_job`
  candidate with **Link them**;
- **`/captured`**, linked from the inbox toolbar beside New packet: `paste-manual` groups from
  capture and New packet, newest first, with title, employer, host, captured date, and bucket or
  "not scored". Once scored, a group also appears in the inbox and flows through the dashboard's
  **Pasted** node, as in 1a.

#### Data

`NNNN_capture_log.sql`:

```sql
CREATE TABLE capture_log (
  id            INTEGER PRIMARY KEY,
  action_id     TEXT NOT NULL UNIQUE,  -- random per user action, minted by the worker
  route         TEXT NOT NULL,    -- capture, capture/add, capture/fetch, describe, link
  job_group_id  INTEGER,          -- no REFERENCES: an 'existing' match may be an ingested group
                                  -- that a nightly merge later deletes (see #78); a log may dangle
  job_id        INTEGER,          -- the job added or matched, to follow merges (Gone groups)
  captured_at   TEXT NOT NULL,
  host          TEXT NOT NULL,    -- page host only, never the full URL
  method        TEXT CHECK (method IN ('jsonld', 'microdata', 'site', 'page', 'selection',
                                     'fetch')),          -- NULL for link
  outcome       TEXT NOT NULL CHECK (outcome IN ('added', 'existing', 'possible', 'previewed',
                                                 'description_added', 'linked', 'same_job',
                                                 'fetched', 'linked_groups', 'not_same')),
  response      TEXT,             -- for write outcomes: the JSON answer, returned again for a
                                  -- repeated action_id; NULL for previewed, possible, same_job
  ext_version   TEXT
);
```

It tells which sites need a site row (many `page` or `previewed` captures from one host),
answers repeated `action_id`s, and is counted by 1d's `apply stats`. It stores no page text:
`response` holds ids, titles, employers and the outcome, not the description. `linked` and
`same_job` are the [Apply control](#linkedin-and-indeed-the-apply-control) outcomes.

```sql
CREATE TABLE job_board_ref (
  board       TEXT NOT NULL CHECK (board IN ('linkedin', 'indeed')),
  board_id    TEXT NOT NULL,
  job_id      INTEGER NOT NULL REFERENCES job(id),  -- a job, not a group: merges move jobs
                                                    -- between groups, so the link follows
  apply_mode  TEXT NOT NULL DEFAULT 'unknown'
              CHECK (apply_mode IN ('easy_apply', 'offsite', 'unknown')),
  apply_url   TEXT,              -- the decoded off-site destination, never fetched from the board
  seen_at     TEXT NOT NULL,
  PRIMARY KEY (board, board_id)
);
```

`find_duplicates` resolves a board id through this table (to the job's current group) as well
as through stored URLs. `430a0e7`'s parser writes the same rows for alert emails, so an alert,
a captured page and an ATS posting meet on the same key.

### Extension in 1e

```
extension/
  manifest.json          permissions above, pinned key, no content_scripts,
                         commands: _execute_action (Alt+Shift+J)
  background.js          service worker: menu, off-list check, injection, single-flight,
                         every console request, badge, popup state
  capture/extract.js     one self-invoking expression; site table inside
  popup/                 index.html, popup.js: result, preview, Score it, Prepare packet;
                         textContent only
  options/               pairing code, console port
  api/v1.schema.json     generated from the console's models; a test keeps it current
```

- **Opening the console**: **Open** uses `chrome.tabs.create` with a console URL
  (`http://127.0.0.1:<port>/job/{id}` or `/packet/{id}`), which needs no `tabs` permission.
- **Off list and notices**: `POST /ext/v1/version` also returns `[capture] disabled_hosts` and
  the notice hosts (LinkedIn, Indeed). The worker checks the tab URL against them before
  injecting. The "notice accepted" flag per host is in `storage.local`.
- **Phase 2 coexistence**: the action stays the popup; on a page with a supported ATS frame it
  shows **Fill this form**, which opens the side panel (`chrome.sidePanel.open` from that click),
  and does not capture on open ([Popup state](#popup-state-and-triggers)).

**Shared with phase 2, built here**: pairing, token storage with `setAccessLevel`, the `/ext/v1/`
checks (POST only, bearer token, exact `Origin`, loopback `Host` even with `--allow-remote`),
CORS only if a preflight shows up, the middleware branch that sends `/ext/` to these checks
instead of `same_origin_writes`, versions with `X-Jobhunter-Ext` and 426, the pinned `key` and
its gitleaks allowlist line, load unpacked, and the lint over HTML sinks and external messaging.
All of it is specified in phase 2's [Local API security](#local-api-security) and
[Distribution and versions](#distribution-and-versions) and lands in 1e unchanged. The 1e routes
are `pair`, `version`, `capture`, `capture/fetch`, `groups/{id}/estimate`, `groups/{id}/score`,
`groups/{id}/status` and `groups/{id}/prepare`. 1e does not depend on 1d's loopback-Host check
for the other routes; the `/ext/` branch has its own.

### What leaves the machine (1e)

| Data | Goes to | When |
|---|---|---|
| The captured facts (JSON-LD objects, page text or selection, URLs) | the console on 127.0.0.1 only | on your click |
| The description, with your resume-derived profile as for any posting | the configured screen scorer (for Jev: openrouter.ai and the model provider), as the notice says | only on **Score it**, after you confirm |
| Page HTML, screenshots, other tabs, iframes, form values, cookies, history | nowhere; never read | never |

Page-text capture can pick up page furniture: a greeting with your name, or "Meet the hiring
team" with other people's names. That is why page text is always previewed in an editable box
(or replaced by a selection) before it is stored or can be scored. Structured capture reads only
the posting's own data.

### Capture compliance

- **Not crawling.** You open the page; you click; the extension reads that one page once. It
  never follows a link (no `link` menu context), paginates, opens or reloads tabs, scrolls,
  expands "show more", or calls the site's APIs, and nothing runs in the background or on a
  schedule. jobhunter's Python makes no request to the site, except **Fetch from** a supported
  ATS on your click, through `FetchContext` and robots.txt ([008](008-compliance.md)).
  robots.txt governs crawlers, not a page you are reading in your own browser.
- **No page changes.** The capture script writes nothing to the DOM (tested), so there is
  nothing for a site to detect beyond the extension's existence, and there are no
  `web_accessible_resources` for a site to probe.
- **LinkedIn and Indeed.** As we read them (to recheck before 1e ships), LinkedIn's User
  Agreement forbids software "including ... browser plugins and add-ons" that scrape or copy its
  data, and Indeed's terms forbid automated means of collecting its content. A click that copies
  the one posting you are reading, for your own use, is close to copy and paste, but it is a
  plugin reading their page, and the risk is to your account. So: **no site rows for either**;
  capture there uses whatever structured data or page text the page has, or your selection; the
  first capture on each host shows a one-time notice and **nothing is read until you accept
  it**; and `[capture] disabled_hosts` (default empty) turns capture off per host, checked in the
  worker before injection. **Your decision (2026-10-10): capture on LinkedIn and Indeed is
  allowed after the one-time notice.** The one board rule is reading the Apply control,
  read-only ([above](#linkedin-and-indeed-the-apply-control)). Better still, when the posting links to the employer's own
  page or ATS, capture that page.
- **001 non-goal** "Private-sector job boards" is about ingesting from them; see the
  [deviation](#deviations-from-earlier-specs) row, whose capture clause you approved on 2026-10-10.

### Tests (1e)

All offline, as in [Testing](#testing). No test scores for real; the scorer and client factories
are fakes that fail a test if called when they should not be.

| Area | How |
|---|---|
| Map and sanitize | Vector file `tests/fixtures/capture/jsonld_vectors.json`: arrays, `@graph`, `@type` lists, an entity-encoded description, a `<script>` and an `onerror` inside the description (gone in the stored text), each `baseSalary` shape (`value`, `minValue`/`maxValue`, `unitText` HOUR/YEAR), several locations, `TELECOMMUTE`, each `employmentType` value and a list; oversized fields refused with 413/422; `og:site_name` ignored on board hosts; `job_locations` written at insert |
| URL choice | A generic JSON-LD `url` (careers home) not used; a relative `url` resolved; a `url` on another host not used; query ids kept for `jobId=`, `sjobid=`, `j=`, `JobId=`, `adid=`, `job=`, `JobOpeningId=`, `career_job_req_id=`, `opportunityId=`, while `utm_*` and tracking parameters are dropped |
| Board ids | `board_key` vectors for every LinkedIn and Indeed form above, plus non-matches (`/jobs/search` with no id, a profile URL) |
| Add or preview | Each condition of [Add at once or preview](#add-at-once-or-preview) on its own: page text previewed; several postings with no match previewed with a picker; stale JSON-LD (page `currentJobId` differs) previewed; an icon selection previewed with both choices; a menu selection without structured title previewed; a clear structured capture added |
| Dedupe | existing by URL, by board id (stored `/jobs/view/123?trk=x`, captured `currentJobId=123`), by the page URL when the chosen URL differs; a careers-home URL match gives `possible`; employer and title gives `possible`, nothing written; `force_new`; Add this description on an `email-manual` group with a `gmail:` URL and empty `description_text` (fields filled, no `description_rev` change); an ingested partial group (no write); `normalize_apply_url` output unchanged on its existing vectors; **two concurrent identical commits give one group**; a repeated `action_id` on a write returns the first answer |
| Apply control | Fixtures (synthetic, hand-written in the shape of the board pages, fixture lint applies): an Easy Apply page (`apply_mode = easy_apply`, no destination), an off-site `a` with a direct ATS `href`, one with a `/redir?url=` wrapper (decoded; the `context.route` counter for linkedin.com stays at zero), one with only a `button` (no destination), an Indeed **Apply on company site** button. Console: the decoded Greenhouse URL matches an ingested group (`existing`, `job_board_ref` written), the LinkedIn id matches an alert-style group (`apply_link` written when absent, never overwritten), both match different groups (`same_job`, nothing written until **Link them**); `link_same_job` keeps the ingested group, re-points the application and packet, redoes no score; click-through: an ATS capture from a tab whose opener holds an off-site LinkedIn capture gets the offer, a capture from an unrelated window does not, **Not the same** writes nothing; no request to a board host in any test |
| Later ingest | As in [Later ingest](#a-later-ingest-of-a-captured-posting): scored capture plus ingest gives one group and one `fit_score`; unscored capture plus ingest gives one group scored once |
| Group flag | As in [Scored on request](#scored-on-request-a-group-flag); every nightly site (prefilter, `_ELIGIBLE`, re-score plan building) skips a flagged group whose canonical is an ingested `full` job; Score it works on it |
| Actions | Each route's repeat with the same `action_id` returns the stored answer and writes nothing; Add after a preview, Add as new, Add this description, Link them and Capture again each succeed after an earlier capture of the same page (different ids); a repeated preview capture is re-evaluated; group routes on a merged group give 404 with `current_group` |
| Link refusal | **Link them** while one group holds a Score now claim (fake scorer blocked) gives 409, nothing written; after the score finishes it succeeds and the score survives on the kept group; each row of the kept-group table |
| Board hosts | `resolve_group` and the 015 refresh make no request (robots.txt included) for an apply link on `www.linkedin.com` or `uk.indeed.com`; a capture replaces an `unresolved` or `blocked` apply link, never a `live` one |
| Never auto-score | After every capture outcome: no `fit_score`, no `prefilter_result`, no `llm_spend`, job at `normalized`, scorer factory not called; a daily run afterwards leaves the group unscored (absent a later ingest) |
| Score it | estimate includes the privacy notice and the prefilter line, and a `ScorerError` notice is a refusal; a stale token or a held claim gives 409 **before** the 202; score returns 202 and status moves from `in progress` to the bucket with a fake synchronous scorer that blocks until released (the background thread uses its own connection); a claim older than `CLAIM_TTL` with no score shows **stopped**; the batch path shows `submitted`; a stale token gives 409 and nothing written; a second confirm while the claim is held gives 409; a scorer failure shows the error in status and leaves nothing waiting |
| capture/fetch | a non-ATS host, an `http://` URL and an unsupported ATS are refused with 422 and `FetchContext` is never called; robots refusal (mocked) shows the paste message |
| API contract | Rev 7's list ([Testing](#testing), *API contract*) on every 1e route; every route answers within its deadline; the schema file matches the models |
| Extension lint | Manifest permissions and hosts exactly as in [Permissions](#permissions-activetab-is-now-allowed), pinned `key`, no `content_scripts`, no `link` menu context, `_execute_action` declared; `capture/extract.js` is one expression statement with no top-level declaration; the deny-list above; rev 7's sink and external-messaging rules everywhere |
| Extraction | `capture/extract.js` evaluated with Playwright on hand-written fixture pages under `tests/fixtures/capture/`, with a main-world `MutationObserver` and `outerHTML` compared before and after on each (no change): JSON-LD posting, `@graph` posting, microdata posting, no structured data (page text; `nav` and a "Hi, Pat" header excluded), hidden text (`display:none`, a visually hidden span: absent), a page with a selected paragraph, a selection inside a `textarea` (ignored), a form with typed values (not captured), an employer page with a Greenhouse iframe (iframe URL reported, iframe text absent), a 2 MB description (truncated), two postings, a stale single-page board. Synthetic employers and figures only; the fixture lint applies |
| Extension e2e (`e2e` marker) | Rev 7's harness: `launch_persistent_context`, `--load-extension`, `--host-resolver-rules` blocking every non-loopback host, `context.route` serving the fixtures, the console in-process on a loopback port, paired through the options page. A test cannot click the toolbar, so the e2e loads **a test copy of the manifest** in a temp dir with **only each fixture's top-level host** added to `host_permissions` (never the iframe's host, so the iframe stays unreadable as in real use), and sends the worker the popup's capture message; the lint confirms the real manifest has no such hosts. If the 1e spike shows the shortcut can fire `_execute_action` under Playwright, the e2e uses it and the test manifest goes. Covers capture added, capture existing, **two captures on the same document without a reload (the second says Already in jobhunter)**, menu capture then popup open (one request), a `pushState` job change (a new capture), the preview path with an edited description, Score it with confirm through 202 and status, Prepare packet, the popup closed mid-request, the worker stopped mid-score (status still reaches the bucket on reopen), the off list (no injection happens), the LinkedIn notice (no injection before accept), an Apply control fixture read without any click, focus or mutation, a restricted page |

**Acceptance (live, with you, no spend):** on postings you are viewing, one each from Greenhouse,
Lever or Ashby, Workday, an employer careers page without structured data, a menu selection
capture (including one inside an embedded form), and LinkedIn and Indeed if you keep them on:
the popup's fields match the page, a second click says **Already in jobhunter**, and nothing was
scored. Score it once only if you choose to.

### Spike at the start of 1e

The offline plumbing part of the old 2a spike moves here, minus what needs content scripts. It
proves, before the feature work: the unpacked extension loads with its pinned ID; pairing works;
the console records the request headers the service worker sends on a POST and on a GET
(`Origin`, `Sec-Fetch-Site`) and whether Chrome sends a preflight; whether Local Network Access
asks before the worker reaches loopback; whether `setAccessLevel` hides the token on
`storage.local`; that an ordinary page on another origin cannot reach any `/ext/` route; how
long a worker `fetch` may wait before Chrome stops the worker (the 20-second deadline assumes
30); whether `chrome.action.openPopup()` works after a context-menu click; and whether
Playwright's keyboard can fire `_execute_action` in a headed browser. **If a page can reach the
API, 1e does not ship.**

### Effort

**≈ 3.5-4 d**: spike 0.25 d; console (`apply/capture.py` with mapping, URL choice, add-or-preview
and dedupe, routes with 202 and status, pairing, `/ext/` middleware branch, versions,
`capture_log`, the later-ingest rule in `group_pending`, `/captured`, Score on `/job/{id}`, API
tests) 1-1.25 d; the Apply control, `job_board_ref`, click-through offer and `link_same_job`
0.5 d; the group flag across the nightly sites, the `group_pending` candidate change, the
`score.py` split, action routes and the board-host no-fetch list 0.5 d; extension (manifest, worker with popup state and single-flight, extraction
walker, popup with preview, options) 0.75 d; fixtures, lint and e2e 0.5-0.75 d. It depends on 1a
only, and is built alongside 1b (your decision, 2026-10-10).

## Phase 2 gate

Phase 2 is built only when both hold:

1. **Use.** After 4 weeks of phase 1, `jobhunter apply stats` shows at least **8 packets marked
   ready** for postings on the four public ATSs (threshold confirmed 2026-10-10), and you say the remainder after the resume parse and your fill helpers still
   costs real time. Per-ATS support is built in order of count.
2. **Spike** (2a below), in two parts. Phase 1e has already proved the channel: the pinned ID,
   pairing, the headers Chrome sends on a GET and a POST and any preflight, Local Network
   Access, `setAccessLevel`, that a page cannot reach `/ext/`, versions and 426, and the
   Playwright harness with the network blocked ([1e spike](#spike-at-the-start-of-1e)). 2a
   covers only what content scripts and form writes add.
   - **Offline**, on hand-written pages served by `context.route` on real ATS URLs (no
     network). It proves: the declared content scripts run on the ATS hosts, including inside an
     embedded Greenhouse iframe on another host, and report their frame URL; that with 1e's
     `activeTab` grant from the popup click, re-injection with `allFrames` reaches both the
     employer's top frame and the ATS iframe after an extension reload; `setAccessLevel` hides
     the token from those content scripts (1e had none to test against); the popup's
     **Fill this form** opens the side panel for the tab; the resume arrives in the frame
     byte-identical; the vetted dispatch helper refuses every submit-like target on the
     fixtures. Hand-written pages encode our own model of an ATS, so this part decides nothing
     about real ATS behavior.
   - **Live, with you**, on real postings you mean to apply to, stopping before Submit. Per ATS,
     it passes when: the automatic attach is accepted (the ATS's own filename or parse indicator
     shows); on **Lever or Ashby** (which parse uploads) the parse fires and the `parse_done`
     signal recorded for `ats_forms.py` is seen; on **`job-boards.greenhouse.io`** (React) a
     filled field keeps its value after blur and after the next re-render; a checkbox and a
     select take their values; and the outline sits on the real Submit control and nothing
     else. Classic `boards.greenhouse.io` forms are jQuery and are checked for attach and fill
     only.

   If a page can reach the API, phase 2 does not ship. An ATS whose attach fails uses manual
   attach; one whose fills do not stick has fill limited to the inputs that did.

If the gate fails, the drafts and checklists remain the answer.

## Phase 2: resume-first fill with the jobhunter extension

### Why our own extension

You asked: *"What if we had a chrome plugin that could talk locally to job hunter?"* Revisions
2-4 drove Claude in Chrome from a restricted Claude Code session. That needed a dedicated
profile, a launch with an allowlist, a PreToolUse hook, a guard userscript and a confirm dialog,
all because a model with browser tools was acting on untrusted pages and sent screenshots and
page reads to Anthropic. A small extension of our own replaces all of that:

| | Claude in Chrome (revision 4) | jobhunter extension (revisions 5-6) |
|---|---|---|
| Who acts in the page | A model, through tools | Deterministic code, on your click |
| What leaves the machine | Page reads and screenshots | For unknown fields you click **Draft** on: their label, help text and options, through jobhunter's normal drafting call. Nothing else, never screenshots |
| Submit stop | Layers around a model that might click it | One vetted dispatch helper that refuses submit targets, checked by lint and by e2e counters on every submit path |
| Resume attach | Uncertain without a native file chooser | `DataTransfer` on the file input, per-ATS fallback |
| Setup | Profile, launch flags, settings, hook, userscript | Load unpacked once, pair once |

### Architecture

```
 Chrome (your profile)                                        Console (127.0.0.1:8808)
 ─────────────────────                                        ────────────────────────
 ATS tab ── content script (isolated world, every frame       /ext/v1/*  POST only:
            on an allowed ATS host)                             token + Origin + Host
              │  chrome.runtime messages only                 apply/fill_classify.py
              ▼                                               apply/ats_forms.py, labels.py
 service worker (stateless relay) ── fetch, bearer token ──►  drafting (CLI runner / SDK)
              ▲                                               packets, resume PDF, fill_session
 side panel ──┘  session state; Filled / Needs you / Drafted
```

| Part | Does | Does not |
|---|---|---|
| **Content script** (`extension/content/`), isolated world, `all_frames: true`, on the allowed ATS hosts only | On a message for its own document: reports field descriptors, then the values the console cleared; writes one approved value into one field through the vetted helpers in `extension/content/act.js`; sets the resume file input; outlines the Submit control | Talk to the console or any network (`fetch`, XHR, WebSocket, EventSource, `sendBeacon` are lint failures); act on page load or on page messages; expose anything to page scripts; decide never-store |
| **Service worker** (`extension/background.js`) | The only part that talks to the console. **Stateless:** it reads the token from storage on each request and relays messages; MV3 kills an idle worker after about 30 s, so it holds nothing that matters | Keep session state in globals; run model calls itself |
| **Side panel** (`extension/panel/`, `chrome.sidePanel`) | Holds the session (session id, tab id, the `documentId`, frame id and selector per field key, the classification) in page memory, mirrored to `chrome.storage.session` so a panel reload recovers it. Bound to the tab it was opened on (`?tab=<tabId>`), never "the active tab". Shows the packet, the fields in three groups, and the buttons | Run in the page; it is an extension page no site can reach |
| **Options page** | Pairing code entry, console port (default 8808), optional hosts | |
| **Console** `console/ext_routes.py` | The `/ext/v1/` API; classification, mapping, never-store and drafting stay in tested Python | Send `Access-Control-Allow-*` to any origin but the paired extension |

Plain JavaScript modules, no build step, no npm dependency. Classification, field mapping and the
never-store check live only in Python (`apply/fill_classify.py`, `apply/ats_forms.py`,
`apply/labels.py`), so the code the tests cover is the code that decides. The extension has no
copy of the never-store patterns.

**Hosts.** `host_permissions` are the hosts `pipeline/ats_rules.py` maps to the four supported
ATSs, plus `http://127.0.0.1/*` for the console: today `boards.greenhouse.io`,
`job-boards.greenhouse.io`, `job-boards.eu.greenhouse.io`, `jobs.lever.co`, `jobs.eu.lever.co`,
`jobs.ashbyhq.com` and `apply.workable.com`. `ats_rules.py` has no Workable rule today; phase 1d
adds one, so New packet and `apply stats` count Workable too. A test checks that the manifest's
hosts equal the `ats_rules` hosts for the supported ATSs, so the two cannot drift. Embedded forms
(Greenhouse `/embed/job_app`, Ashby embeds) load in an iframe from these hosts, so the frame's
content script handles them on any employer page; the employer's own top page is never read.
Workday (`*.myworkdayjobs.com`, `*.myworkdaysite.com`) and NEOGOV (`governmentjobs.com`,
`www.governmentjobs.com`, `schooljobs.com`, `www.schooljobs.com`) are
`optional_host_permissions`, granted later by your click on **Enable for this site** after you
have signed in yourself. Permissions are 1e's `activeTab`, `scripting`, `storage` and
`contextMenus` ([1e permissions](#permissions-activetab-is-now-allowed)), plus `sidePanel`;
`scripting` also registers content scripts for granted optional hosts and re-injects into open
tabs after a reload. Phase 2 adds the ATS `host_permissions` and their `content_scripts`, and
nothing from 1e's forbidden list: no `<all_urls>`, `tabs`, `webNavigation`, `cookies`,
`webRequest`, `debugger`, `tabCapture`, `downloads` or `externally_connectable`, and no
`web_accessible_resources`.

### Flow

1. **Open application** on the packet page (through `/apply/{id}`, so 015's click log and "Did
   you apply?" work), or open the posting yourself.
2. Click the extension's icon. The popup (1e) shows **Fill this form** when an ATS frame
   answered; that click opens the side panel for this tab. Each ATS frame's content script
   announces itself with `chrome.runtime.sendMessage`, so the worker learns its `frameId`,
   `documentId` and frame URL from `sender`. The packet is matched on **the ATS frame URLs**,
   normalized as in 015 (an embed's `for` and `token`, or a `gh_jid`), against `apply_link` and
   `job.url`, and also on the tab's URL, which the icon click's `activeTab` grant makes readable
   (no `tabs` permission; the top page's content is not read in phase 2); with no match, your
   `ready` packets are offered to pick. No ATS frame answers (for example after the extension
   was reloaded): the worker injects the content script into the tab's frames with
   `chrome.scripting.executeScript({allFrames: true})` and asks again. The `activeTab` grant
   should let this reach an employer page's top frame and so its ATS iframe (the 2a spike
   confirms); if no frame answers after injection, the panel says **Reload this page to
   continue** (before anything was typed, nothing is lost; the panel warns that typed values
   may be).
3. **Start.** The console opens a `fill_session` only if the packet is `ready` with a rendered
   resume PDF ([Resume attach](#resume-attach)), the posting is not `expired`, and today's count
   is under the cap (10, hard cap 25). An earlier session still open (a crash, a closed panel, a
   fetch-based submit that left the thank-you view up) is shown and, on your click, closed as
   `aborted`; any session idle for 2 hours is closed as `aborted` by the console. The session
   records each frame's `documentId` and URL. Login wall, account wall or challenge on the page:
   the panel says so and stops (`challenge`). If the console restarts mid-session, the in-memory
   snapshots are gone: the next request gets `409 session lost`, the session is closed as
   `aborted`, and the panel offers **Start again** (a new before-snapshot; fields already filled
   then count as `keep`).
4. **Snapshot before, in two steps.** Each frame sends **descriptors without values** (frame,
   selector, id, name, label, help text, type, option texts) with a **field key** that does not
   depend on generated ids (React `:r3:`-style ids, numeric suffixes): the `ats_forms.py` field
   name when known, else `name`, else a hash of the normalized label plus type plus its position
   among fields with the same label. The selector is kept only for writing, per document. They go
   to
   `POST /ext/v1/sessions/{sid}/fields`. The console runs the never-store match on label, name,
   id and, for a choice group, its option texts, and answers with the field keys that may send
   values. The frames then send `value_present` for every field and the value only for the
   cleared keys (for a choice group, the selected or checked option counts as the value) to
   `POST .../snapshot` with `phase = before`. The console drops any value that arrives for a
   field it did not clear, and keeps snapshots **in memory for the session only**, never in
   SQLite or logs.
5. **Attach resume** ([below](#resume-attach)). Or you attach it yourself and click **Attached**.
6. **Settle.** Wait for the ATS's `parse_done` signal from `ats_forms.py`. Signals are of three
   kinds, each with a named mechanism: a **request finished** (a `PerformanceObserver` on
   `resource` entries whose URL matches the ATS's upload or parse path; the content script sees
   its own frame's requests without any extra permission), an **element gone or present** (a
   `MutationObserver` on the form for a named spinner or chip), or a **named field populated**
   (the same observer plus `input` events). For an ATS with no parse (classic Greenhouse), there
   is nothing to wait for. Without a known signal: wait for the first change to a non-file field,
   up to 15 seconds. Then wait until no value has changed for 3 seconds (value polling every
   250 ms plus the `MutationObserver`), with a 45-second cap; on the cap the panel says `parse
   did not settle` and continues. The file input's own value (`C:\fakepath\...`) is never
   counted as a change. Fields that **appear** during the parse (an extra work-history block)
   are treated as empty before the attach.
7. **Snapshot after** (same two steps, `phase = after`). The console diffs the two and returns
   an action per field ([Field classification](#field-classification)).
8. The panel shows **Filled** (`ok`, `fill`, `keep`), **Needs you** (`yours`, `differs`,
   `listed`) and **Drafted** (`paste`, `draft`). You click **Fill gaps** for the ticked `fill`
   rows, **Draft** on an unknown question, **Insert** on a draft you accept, and **Use packet
   value** or **Restore previous value** on a `differs` row. Every write is one field, one value,
   sent with `{documentId}` so it can only reach the document the session snapshotted. Before
   writing, the content script checks that its URL still matches the session's frame URL and
   re-reads the field: a `fill`, `paste` or Insert target that is no longer empty is not
   written and is reclassified. After writing it re-reads on the next animation frame and again
   after `blur`; a value the page rejected or reformatted makes the field `listed`.
9. **Stop.** The Submit control is outlined with "jobhunter stops here. You submit." The
   extension never clicks it. The session ends on **Done**, on the tab closing, on navigation of
   a session frame to another URL (writes stop at once), on a `submit` event in a session frame,
   or on the ATS's confirmation view (`ats_forms.py` `confirmation` selector) appearing. The last
   two record `submit_seen`. The report holds keys, labels and actions, no values.

```
jobhunter · Acme · Senior Platform Engineer                     packet v3 · ready
Filled (15)     11 by the resume parse, 2 kept as they were, 2 gaps filled (LinkedIn, Notice period)
Needs you (5)   Phone changed by the parse: "+1-555-0100" → "5550100" [Restore] ·
                Job 2 split in two · Salary · EEO block · Consent checkbox
Drafted (2)     "Why Acme?" draft ready [Insert] · "Kubernetes in production?" [Draft]
                                       Submit is outlined on the page. You submit.
```

### Field classification

`POST .../snapshot` with `phase = after` runs `apply/fill_classify.py`. First matching row wins:

| # | Field | Action |
|---|---|---|
| 1 | Label, name, id or (for a choice group) option texts match the [never-store list](#never-store-list) (includes consent, certification, signature) | `yours`: never written, listed |
| 2 | Non-empty **before** the attach (ATS draft, returning-candidate prefill, your typing, your other fill helper, an earlier session) and **unchanged** after | `keep`: never written, shown under Filled |
| 3 | Non-empty before the attach and **changed by the parse** | `differs`: before, after and packet values shown; **Restore previous value** or **Use packet value** on your click |
| 4 | Empty before, filled by the parse, matches the packet resume's entries | `ok` |
| 5 | Empty before, filled by the parse, differs (split job, wrong phone, mangled title) | `differs`: both values listed; **Use packet value** writes one field on your click |
| 6 | Empty, mapped deterministically by `ats_forms.py` field names or `labels.py` synonyms to a value in the packet or `/prefs` answers (your decision) (links, notice period, relocation, work authorization yes/no), and settable by a vetted helper | `fill`: ticked in the panel, written on **Fill gaps** |
| 7 | Empty free-text question with a saved answer or draft on this packet, matched by known field name or an unambiguous label | `paste`: **Insert** on your click |
| 8 | Empty, unknown free-text question, or an unknown choice field | `draft`: **Draft** on your click sends it to jobhunter ([below](#what-leaves-the-machine)) |
| 9 | Anything else (extra file inputs, a widget no vetted helper can set) | `listed` |

Rows 4-5 compare against the packet resume's structured entries (`doc_json`). For **Use base
resume**, the base resume is parsed once into entries by `apply/resume_parse.py` (cached by
resume hash); if it cannot parse, rows 4-5 report `not compared`. Field names, label synonyms,
the parse control, the attachment field, the attach method, `attach_ok`, `parse_done`,
`confirmation` and the Submit selector are data in `apply/ats_forms.py` and `apply/labels.py`,
recorded at fixture capture and grown like `ats_rules.py`. The extension fetches them per
session. **That data only names targets and which vetted helper to use; it never names event
types or sequences** ([Fill mechanics](#fill-mechanics)), so no data can create a click path.

This is resume-first, as you asked: the site's own parse goes first, your other helper's fills
are kept (and shown if the parse overwrote them), and the extension only fills or drafts what is
left.

### What leaves the machine

Snapshots go only to the console on 127.0.0.1, values only for fields the console cleared, held
in memory for the session; the stored report keeps keys, labels and actions, never values. Page
content reaches a model only when you click **Draft** on a row-8 field, and then only through
jobhunter's normal question-draft call (same runner, cap, factcheck and editor as phase 1): the
field's label, help text, type and options, plus the packet data phase 1 already sends (resume
lines, posting, notes, story facts). Not the page HTML, other fields' values, the URL beyond the
posting already in the packet, or any screenshot; the extension has no capture permission. A
choice-field draft is one of the given options with its cited lines, checked like any draft,
and applied only on your **Insert** (your decision); a numeric-experience question shows the evidence lines
instead, as in phase 1. The never-store check runs again on the Draft request. This retires
revision 4's open question 10.

### Resume attach

Phase 2 needs the packet's **rendered PDF**: Start refuses without one and links to the packet
page's export (which needs the `browser` extra, or 018's Chromium image target). If you only
printed the HTML yourself, choose **I'll attach it myself** at Start; the session runs with
`attach = manual`.

**Attach resume** asks the service worker for the PDF with
`POST /ext/v1/packets/{id}/resume` (the packet's rendered PDF, named
`<First>-<Last>-Resume.pdf`, with its SHA-256 in the `X-Resume-SHA256` header). Chrome's extension
messaging serializes to JSON, so the worker passes the bytes to the frame **base64-encoded**,
and the content script decodes them and checks size and hash before building the `File`. It
then sets the control `ats_forms.py` names: `new DataTransfer()`, add the `File`, assign
`input.files`, dispatch `input` and `change`. Where an ATS has two file controls, `ats_forms.py`
names which one: on **Ashby**, the "Autofill from resume" box (`parse_control`) gets the file,
because only it triggers the parse, and the Resume field (`attachment_field`) is checked
afterwards, since Ashby normally fills it from the same upload; if it stays empty the panel
offers **Attach to Resume field too**. The live spike confirms this per ATS. The method is per
ATS:

| `attach` | Used when |
|---|---|
| `datatransfer` (default) | A real `<input type=file>` that the site reads on `change` |
| `drop` | A dropzone that ignores the input: a synthetic `dragenter` / `dragover` / `drop` with the same `DataTransfer`, through the vetted helper |
| `manual` | The site rejects or ignores both (seen at fixture capture or live): the panel shows the file and its path, you attach it with the site's own button, then click **Attached** |

**Success is judged per ATS**, by its `attach_ok` signal in `ats_forms.py` (the ATS's own
filename chip, an upload request finished, or the parse starting), not by a generic field diff.
If `attach_ok` is not seen within 15 seconds, the panel falls back to `manual` for this session
and records it on the session (`attach_failed`), for you to confirm before `ats_forms.py` is
changed. A failed fetch of the PDF is shown as such and never recorded against the ATS. Do not
re-attach after correcting fields: most ATSs re-parse and overwrite your corrections (the
checklist says so too).

### Fill mechanics

All page writes go through the vetted helpers in `extension/content/act.js`; nothing else in
`extension/` touches field values or dispatches events.

- **Text, textarea, select (`setValue`):** the native `value` setter **of the element's own
  prototype** (`HTMLInputElement`, `HTMLTextAreaElement` or `HTMLSelectElement`; one prototype's
  setter on another element throws), then bubbling `input` and `change`, then `blur`. React and
  Angular see the change because their value trackers live on the page world's wrapper, not on
  the native property. **No keyboard events**, ever: Enter in a text field can submit a form.
- **Checkbox and radio (`setChecked`):** if the current state already equals the target, do
  nothing. Otherwise dispatch **one** `click` through `safeDispatch` and nothing else: the click's
  own activation toggles a checkbox or selects a radio and fires `input` and `change`, and React
  listens to that click. (Setting `checked` first and then clicking would toggle it back.)
- **Custom widgets (`pickOption`):** for comboboxes such as react-select on
  `job-boards.greenhouse.io`. It may dispatch `mousedown` and `click` only on an element with
  `role=combobox`, `role=option` or inside a `role=listbox`, through `safeDispatch`, and only
  where a fixture and the live check showed it works. Ashby's yes/no questions are `<button
  type=button>` pairs; buttons are never a dispatch target, so they are `listed` for you
  (your decision: widgets that cannot be set this way are yours).
- **`safeDispatch(target, type)`**, the one function that may construct `MouseEvent`,
  `PointerEvent` or `DragEvent`, refuses (and logs) any target that is or sits inside: a
  submitter (`button` with no type or `type=submit`, `input[type=submit|image|button|reset]`),
  any `button` or `a[href]` except a `role=option` inside a listbox, anything matching the
  ATS's Submit selector or the outlined control, and any target whose own text, `value` or
  `aria-label`, or that of its nearest enclosing `button`, `a` or `[role=button]`, matches submit
  / apply / send / finish / next / continue / review (en, es, fr, de, pt). The word rule looks at
  the target and that one enclosing control only, never at other ancestors, so an option inside
  a listbox labelled "Next steps" is not refused. `type` must be one of `click`, `mousedown`,
  `dragenter`, `dragover`, `drop`.

**Synthetic events are `isTrusted = false`.** A page can tell them apart, and some widgets ignore
them. When a field ignores the write (the re-read shows the old value), it becomes `listed` for
you. We do not work around a site that checks: `chrome.debugger` (which can send trusted input)
is **out of scope**. It needs the `debugger` permission, shows Chrome's "is debugging this
browser" banner, gives full control of every page, and trusted synthetic input is close to the
"humanizing" 008 rules out. Revisit only with a recorded decision if a core ATS cannot be helped
any other way.

### The stop before Submit

- The extension's code has **no path to Submit**. Outside `act.js`, a lint test fails on
  `.click(`, `.submit(`, `requestSubmit`, `prototype.submit`, `SubmitEvent`, `Event('submit'`,
  `new Event(` with a click or submit type, `MouseEvent`, `PointerEvent`, `DragEvent`,
  `KeyboardEvent`, `CustomEvent`, `new Event('click'`, `.click.call(`, `prototype.click`,
  `["click"]`, `dispatchEvent(`, `form.action`, `location =`, `location.assign` and
  `location.replace`. Inside `act.js`, only `safeDispatch` constructs mouse, pointer or drag
  events, and it refuses submit targets as above. Widget behavior is code in the repo, reviewed
  like any other change; console data only picks a target and a helper.
- **e2e proof, on every fixture,** including those that submit by `fetch` from a labelled
  `type=button` (no `submit` event): a capture-phase listener counts `click`, `mousedown` and
  `pointerdown` on every Submit candidate, a listener counts `submit` events, and
  `context.route` counts requests to each fixture's submit endpoint. After a full session (attach,
  Fill gaps, Insert, Use packet value) all three counters must be zero. The test then clicks
  Submit itself and asserts the counters move, so a broken counter cannot pass. A unit-level
  e2e calls `safeDispatch` on each submit-like fixture element and asserts it refuses.
- The Submit control (the `ats_forms.py` selector, else submit-type controls and buttons with
  the submit words above) gets a visible dashed outline and a label, "jobhunter stops here. You
  submit.", drawn in a closed shadow root.
- No model acts in the page, so revision 4's guard userscript, confirm dialog, hook, allowlisted
  launch and skill rules are gone. You press Submit.

### Local API security

**Built in phase 1e** ([1e](#phase-1e-capture-from-any-job-page)); phase 2 adds routes, not
controls. The console has no auth today. `same_origin_writes` refuses cross-site writes: a state-changing
request from a browser must name a loopback `Host` and come from the console's own origin. GETs
are not checked, so a DNS-rebinding page (a hostile name that resolves to 127.0.0.1) can read
any console page today. 017 adds packets, letters, notes and story facts to those pages, so
**phase 1d extends the loopback-`Host` check to every method on every route** when not
`--allow-remote` (a small change to `cross_site_reason`, with tests). The rule keeps today's
shape: it applies to any request that carries browser fetch metadata (`Sec-Fetch-Site`, which
every current browser sends on every request, a rebinding page's GET included, or `Origin`).
Requests with neither (curl, the CLI, FastAPI's `TestClient` with its `testserver` Host) pass as
today, so existing tests need no change; new tests that send `Sec-Fetch-Site` set
`Host: 127.0.0.1:8808` explicitly, and one test checks that `testserver` with `Sec-Fetch-Site`
is refused. `/ext/v1/` adds an authenticated door for one client:

| Control | How |
|---|---|
| **POST only** | Every `/ext/v1/` route is a `POST`, including reads (version, packet lookup, resume PDF). Chrome adds `Origin` to non-GET requests from an extension; on a GET to a host in `host_permissions` it most likely sends none, which would make an Origin check refuse every read. The spike records the headers on both |
| **Pairing, once per install** | **Pair browser extension** on `/prefs` (a same-origin console POST) or `jobhunter ext pair` creates a one-time code of **10 base32 characters (50 bits)**, valid 10 minutes, shown once. Its hash and expiry are written to `<data dir>/extension.json` (mode 0600), because the CLI and the console are separate processes (in container mode, run the CLI inside the container or use `/prefs`). You type it on the extension's options page; the service worker sends it to `POST /ext/v1/pair`. The console accepts only the **pinned extension ID** (the one the manifest `key` produces; a test ties the two), with `Origin` equal to `chrome-extension://<that id>`; five wrong codes void the pending code. It returns a random 256-bit token and stores only its SHA-256 and the ID in `extension.json`, removing the pending code. Pairing again revokes the old token; `jobhunter ext unpair` revokes it |
| **Token storage in Chrome** | `chrome.storage.local`, with `setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})` called on worker start so content scripts cannot read it (a lint check requires the call). If the spike shows Chrome does not support that on `local`, the token goes in `storage.session` too and you pair again after a browser restart; the decision is recorded |
| **Every `/ext/v1/` request** | `Authorization: Bearer <token>`, compared in constant time; `Origin` exactly the paired `chrome-extension://<id>`; `Host` exactly `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`, where `<port>` is the port the browser connects to (the console's own port, or in a container the published host port, set by `[console] public_port`, default 8808), **even with `--allow-remote`**, so a DNS-rebinding page under its own name is refused. Missing or wrong: 401/403, logged without the token |
| **CORS** | The service worker's fetches to a host in `host_permissions` are not subject to CORS, so no `Access-Control-Allow-*` should be needed. If the spike shows a preflight, `/ext/v1/` answers it for the paired origin only, never `*`, never with credentials: `Access-Control-Allow-Origin: chrome-extension://<id>`, `Access-Control-Allow-Methods: POST`, `Access-Control-Allow-Headers: Authorization, Content-Type, X-Jobhunter-Ext`, `Access-Control-Expose-Headers: X-Resume-SHA256`, `Vary: Origin`. Never `Access-Control-Allow-Private-Network`. Every other console route still sends none |
| **Web pages** | Chrome already limits public-page requests to loopback; the token, Origin and Host checks hold regardless. The token is never in a URL, cookie or page |
| **Content script boundary** | Content scripts never use the network. Messaging is `chrome.runtime` only; no `window.postMessage`, no listeners for page messages, no `externally_connectable`, and no `runtime.onMessageExternal` or `onConnectExternal` listener (lint). A content script receives one value at the moment you approve writing it into one field, and keeps nothing. Nothing is written to the DOM except form values you approved and the outline and label, which carry no data |
| **Side panel rendering** | Labels, help text, options and drafts are untrusted page or model text, rendered with `textContent` only. Lint fails on `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` and `srcdoc` anywhere in `extension/` |
| **Existing routes** | `same_origin_writes` (with the extended Host check) is unchanged outside `/ext/`. Under `/ext/` the stricter check above replaces it: a request from the console's own pages or from curl, without the token, is refused |

### Coexisting with Jobright

Your fill helper is **Jobright** (2026-10-10). It fills only when you click **Autofill** in its
sidebar, reuses your answers to earlier questions, and puts a **Customize** button in the corner
of resume and cover-letter upload widgets. jobhunter's extension is built to sit beside it:

- **Click-only, like Jobright.** Neither acts on page load, so they never race.
- **Order: the site's parse, then Jobright, then jobhunter.** The panel's checklist says: attach
  (jobhunter or by hand), let the parse settle, click Jobright's Autofill, then click **Start**
  in jobhunter's panel, or **Re-snapshot** if a session is already open. Fields Jobright filled are
  non-empty in the snapshot and so count as filled (`keep`); jobhunter fills only what both left.
  If Jobright fills after jobhunter's before-snapshot, those fields are re-read before any write
  and are not touched (Flow step 8).
- **Corner buttons on upload widgets, mirroring Jobright's pattern.** On the resume and
  cover-letter file widgets that `ats_forms.py` names, jobhunter adds a small button (closed
  shadow root, offset from Jobright's corner so neither covers the other; placement confirmed in
  the live spike): **jobhunter resume** attaches the packet's targeted PDF to that widget, and
  **jobhunter letter** attaches the packet letter, or, when there is none, opens the packet page
  to **Add cover letter** (a click, with its runner shown). Both only on your click, through the
  same attach path and checks.
- **Fixtures** include a Jobright-like mimic: a sidebar button that, when clicked, fills contact
  and link fields with `isTrusted = false` events after a short delay and adds a corner button
  to upload widgets. The e2e runs it before Start and between snapshot and Fill gaps, and asserts
  jobhunter never overwrites its values and that the two corner buttons do not overlap.

### Multi-step forms (prerequisite for Workday and NEOGOV)

The four public ATSs are single-page forms. Workday and NEOGOV spread the application over
several steps behind **Next**, which the extension never clicks. Before their optional hosts are
added, a short design is due (its own bug) covering: the session surviving **your** Next click
within the same ATS flow (a navigation or view change to a URL that `ats_forms.py` marks as the
same application keeps the session; anything else ends it); classification per step, with the
before-snapshot taken when a step appears and no attach after the first step; the attach on the
step that holds the upload; and the confirmation view as the end. Until then, Workday and
NEOGOV get the checklist only.

### Chrome profile

**A dedicated profile is optional, and the default is your everyday profile.** Revision 4
required one because Claude in Chrome shared every login in the profile with a model. The
extension has host access only to the ATS hosts and the console, acts only on your click, sends
no screenshots or page reads anywhere, and sits next to the fill helper you already use there,
which is what resume-first needs. If you prefer separation, load it in another profile; nothing
changes.

### Distribution and versions

**Built in phase 1e**, as below.

- **Load unpacked** from `extension/` at the repo root (`chrome://extensions`, Developer mode).
  No Web Store. Updates are `git pull` and **Reload**. Reloading orphans the content scripts in
  open ATS tabs; Start re-injects them (Flow step 2), so a reload does not cost you the form.
- **Pinned ID:** `manifest.json` carries a public `key`, so the extension ID is the same in every
  clone and path and pairing survives a move. The key pair is generated in a scratch directory
  and the private half discarded: nothing is packed or signed. gitleaks flags the public key as
  `generic-api-key` (verified by the review); the 1e worker allowlists exactly that file and
  line in `.gitleaks.toml` with a comment saying it is a public key that pins the extension ID.
- **Versions:** the manifest `version` follows the extension; the API path is `/ext/v1/`.
  `POST /ext/v1/version` returns the API version, the console version and `min_extension`. The
  service worker sends `X-Jobhunter-Ext: <version>`; below `min_extension` the console answers
  426 and the panel says "Reload the extension from your checkout". A breaking change is
  `/ext/v2/`, served beside v1 for one release.

### Running the console in a container

Spec 018 (in progress) may run the console in a rootless podman container. The extension needs
only `127.0.0.1:8808` published on the host (`-p 127.0.0.1:8808:8808`), never `0.0.0.0`. Inside
the container the server may bind all interfaces; the loopback-`Host` check (on every route once
1d extends it, and on `/ext/` always), not the bind address, is what refuses rebinding pages.
`extension.json` and the packet PDFs live in the data dir, on the container's data volume. The
CLI runner is not in the container: drafting there is the API on your click, or
`jobhunter apply draft` on the host ([CLI runner](#cli-runner)).

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
  frame_url    TEXT,             -- the ATS frame the session is bound to
  ext_version  TEXT,
  attach       TEXT,             -- datatransfer, drop, manual
  attach_failed INTEGER NOT NULL DEFAULT 0,
  started_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,    -- for the 2-hour idle close
  finished_at  TEXT,
  outcome      TEXT CHECK (outcome IN ('stopped_before_submit', 'submit_seen', 'challenge',
                                       'aborted', 'error')),
  report       TEXT              -- JSON: keys, labels, actions. Never values
);
```

Snapshots are never stored. `submit_seen` means a session frame saw your submit event or the
ATS confirmation view; it is a hint for "Did you apply?", not an `applied` event.

## Closing the loop

The packet page's **Open application** goes through `/apply/{id}`, so the existing "Did you
apply?" prompt ([015](015-apply-links.md#fresh-at-click-time)) appears on return; the mail match
proposes the confirmation as today. A phase 2 session ending in `submit_seen` only makes that
prompt more likely to be answered; it records nothing by itself.

The packet is attached by one function, **`apply/packets.py::attach_sent_packet(conn, app_id)`**,
called inside the same transaction as the `applied` event on **every** path. Today those paths
do not share code: the mail proposal accept and manual events (`tracking_routes`) go through
`tracking.add_event`, but the "Did you apply?" *Yes* (`console/detail.py::answer_prompt`) writes
its own `UPDATE application` and `INSERT INTO application_event` in its own transaction and never
calls `add_event`. Phase 1d therefore calls `attach_sent_packet` from `add_event` **and** from
`answer_prompt` (or moves `answer_prompt` onto `add_event`, whichever keeps its transaction and
event note intact). When the event is `applied` and the application has a `ready` packet, it sets
`application.resume_version` to `packet:<id>/resume/v<n>` and `cover_letter_path` to the
rendered letter, and adds `attachment` rows for the rendered files (outside the tracked tree, as
`tracking.validate_attachment_path` requires), so `/pipeline/{id}` shows what was sent.
Tests go through the routes, not the function: `POST` the prompt's *Yes*, accept a mail
proposal, and add a manual event, and each must leave `resume_version` and the `attachment` rows
set.

A packet `ready` for 7 days whose application has **no `applied` event** shows on `/followups`
as "ready, not applied?".

Review follow-ups (950d5eb): files exported **after** the application was marked applied are
attached on that export (`packets.attach_late_export`): the resume version the `packet:` ref
names, and the letter while the packet is still `ready` with that resume (a version made later is
never attached; which letter version went out is not recorded otherwise). "Did you apply?" is not
asked again for an application already at `applied` or later, and a *Yes* from a stale banner
leaves such an application's status, applied date and events alone.

## Security and privacy

- **Console.** Still loopback. New packet `POST` routes inherit `same_origin_writes`, and phase
  1d extends its loopback-`Host` check to GETs, so a DNS-rebinding page cannot read packets. Only
  `/ext/v1/` accepts the paired extension, with its own token, Origin and Host checks
  ([Local API security](#local-api-security)); no other route sends `Access-Control-Allow-*`.
- **Capture (1e)** reads only the tab you clicked, once, under `activeTab`, with read-only code
  in the isolated world; what it captures goes to 127.0.0.1 and reaches a scorer only when you
  confirm **Score it** ([What leaves the machine (1e)](#what-leaves-the-machine-1e)).
- **No credentials** stored or seen. You sign in; the extension stops at login pages. The
  pairing token is the one secret: hashed on the console side, never in a page, URL or log.
- **The CLI runner** gets no tools, no MCP servers, no settings, no session saved, an empty
  working directory and a scrubbed environment with non-essential traffic off, so a
  prompt-injected posting cannot make it read or write files, and it cannot bill an API key.
- **Subscription terms.** Calls on the CLI runner fall under your Claude subscription's
  consumer terms, not the API's commercial terms; the resume, header included, goes with every
  packet call. You said on 2026-10-10 that this is fine, training included, so `cli` is the
  default.
- **Nothing personal in the repo.** Packets, saved answers, exports and `extension.json` live
  under `<data dir>` and the database; fixtures are captured logged out and linted
  ([Testing](#testing)).
- **Logs** record ids, keys, labels, outcomes, runner and costs, never answer values, field
  values or document bodies.

| Data | Anthropic (CLI runner under subscription terms, or API) | Extension and console (local) | The employer |
|---|---|---|---|
| Resume text, header included | yes, as Stage 2 already sends it | the PDF goes from the console to the file input | yes |
| Posting, fit evidence | yes | — | — |
| Page form fields | only label, help text and options of a field you click **Draft** on | descriptors, then cleared values, to 127.0.0.1, in memory for the session; report keeps no values | — |
| `/prefs` answers, saved answers and drafts | drafts are generated there; saved and `/prefs` answers are never sent | written into a field on your click | when filled |
| Claude Code telemetry | off for the CLI runner (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`) | — | — |
| Employer notes, story facts | yes (letter, drafts) | no | in the letter or answer |
| Never-store list | not stored, never sent | not stored; values never leave the tab (the console clears fields before values are sent, and drops any that slip through) | when you type them |
| A captured posting (1e) | its description, only on **Score it** after you confirm | to 127.0.0.1 on your click; stored as text | — |
| Screenshots | never | never taken | — |
| OpenRouter, Jev, a local scorer | never | | |

## Compliance

| Rule in [008](008-compliance.md) | How |
|---|---|
| No automated submission | You submit. The extension has no code path to Submit, enforced by lint and e2e tests; no flag changes it |
| No CAPTCHA solving | Stop and hand back; no trusted-input tricks (`chrome.debugger` out of scope) |
| No login automation or credential storage | None; Workday and login.gov are yours |
| robots.txt; only `FetchContext` does network I/O | Generation fetches nothing. **Fetch posting text** goes through `FetchContext` per host robots. jobhunter's Python makes no ATS request in phase 2. robots.txt governs crawlers, not a page you open in your own browser, so it does not apply to the extension; it still never fetches ATS URLs itself, and Greenhouse `/embed/` is touched only when an employer page you opened embeds it |
| Honest identity | The extension acts in your browser as you; no UA tricks or proxies |
| No crawling (1e capture) | Capture reads the one page you are viewing, once, on your click; it never follows links, paginates, opens tabs or runs in the background, and makes no DOM changes. jobhunter's Python fetches nothing from the site except **Fetch from** an embedded ATS on your click, through `FetchContext` and robots.txt. LinkedIn and Indeed terms: [Capture compliance](#capture-compliance) |

**ATS terms.** We found no candidate-facing terms from the four ATSs about autofill in a
candidate's own browser; their terms and spam defenses target automated and bulk submission.
Browser autofill extensions are common and act like this one: one person, one form, filled on
click, reviewed and submitted by hand. That is the posture least likely to conflict. Workday's
and NEOGOV's candidate terms are checked before their optional hosts are added. Not legal advice
([008](008-compliance.md#legal-note-briefly)).

### Deviations from earlier specs

Recorded on `7fa29db`. **You approved all of them on 2026-10-10**; the amendments to 001, 002,
008, 009 and 015 and a note on the `CLAUDE.md` SDK exception land with phase 1 (in the bug that
first needs each):

| Spec | Today | Proposed | Why |
|---|---|---|---|
| [001](001-goals-and-scope.md#non-goals) | "Resume generation: ... It does not write your resume." | "No resume written from scratch. Tailored variants of your own resume, limited by the no-fabrication checker and your review (017), are in scope." | You asked for it; the checker and line-by-line evidence address the fabrication risk the non-goal guarded |
| [001](001-goals-and-scope.md#non-goals) | "Private-sector job boards (Indeed, LinkedIn, Greenhouse): ..." | "No *ingest* from private-sector boards. 017 may fetch one posting you paste, record one posting you are viewing when you click **Send to jobhunter** (1e), and, after its gate, help with one form you chose in your own browser, never submitting." | That non-goal is about crawling for jobs; 017 crawls nothing. The capture clause, new in revision 8, **you approved on 2026-10-10** |
| [015](015-apply-links.md) | "It never fills it in or submits it" | "The Apply button never fills or submits. Form help is a separate extension action (017, phase 2), and nothing in jobhunter submits." | The button keeps its behavior |
| [009](009-roadmap.md#m9--remainder-and-ops) | M9 "Remainder and ops", 0.5-1.5 agent-days; the whole roadmap 5.75-11 d | New **M10 Assisted apply** after M9: phase 1 ≈ 9-11 d (with 1e), phase 2 ≈ 5-7 d after its gate | 017 alone is about the size of the original roadmap; folding it into M9 would hide that |
| [002](002-architecture.md#repository-layout) | Python package, tests, specs; no JavaScript outside console static files | Adds `extension/` at the repo root: a Manifest V3 extension in plain JS modules, no build step, loaded unpacked. It now lands in phase 1e, not phase 2 | It runs in Chrome, not in the Python process; keeping it outside `src/` keeps it out of the wheel |
| `CLAUDE.md` "official `anthropic` SDK only" | SDK for every model call | Packet drafting calls `claude -p` on your subscription first, the SDK as fallback; scoring is unchanged | Your decision (2026-10-10); recorded on `7fa29db`. The CLI runs with no tools, MCP, settings or session, in a scrubbed environment |
| [008](008-compliance.md#personal-data) | The resume goes "to the configured provider and nowhere else"; "No analytics, no telemetry, no outbound reporting" | "...the configured provider, which for 017 packet drafting may be your Claude subscription through the Claude Code CLI, under its consumer terms. The CLI runs with non-essential traffic (telemetry, error reporting, auto-update) turned off." | The CLI is a different channel and terms from the API; telemetry is off by environment, but the terms difference needs your approval |

Revision 4's 008 deviation (page reads and screenshots through Claude in Chrome) is
**withdrawn**: page content leaves the machine only inside jobhunter's own drafting request.
The 008 row above is new in revision 6 and is about the CLI runner, not the extension.

## Failure modes

| Failure | Behavior |
|---|---|
| Generator returns invalid output or times out | Nothing saved. CLI runner: the page shows the reason and offers **Run on the API (≈ $0.15)**, which waits for your click. API: retry; error on the page |
| `claude` not on `PATH`, not logged in, or over the subscription's limit | Known only by running it (a missing binary is known up front and the button says so). The page offers the API run on your click; with no `ANTHROPIC_API_KEY` either, Generate is disabled with the reason |
| CLI init line shows API-key auth (`apiKeySource` not `none`), other tools than `StructuredOutput`, or an MCP server | Child killed before the model is called, so nothing is billed; the CLI runner is turned off until you look; the page says why |
| Subscription on paid extra usage (`isUsingOverage` / `overageInUse`) | That call is logged as paid (tier `packet`, apply cap); further CLI calls wait for your click; over the cap the child is killed |
| `claude auth status` is not a claude.ai login | CLI runner off with the reason; the API offered on click |
| Console restarts mid-session | `409 session lost`; session `aborted`; **Start again** |
| Extension reloaded on an employer page with an embedded form | Re-injection may not reach the iframe; the panel says **Reload this page to continue** |
| Generator invents or inflates a fact | Badged by factcheck; Mark ready blocked until fixed or confirmed |
| Overstatement the lexicon misses | Not detected; the cited line beside every line, and the entailment pass |
| Over `[apply] daily_cap_usd` on the API fallback | API generation disabled; the CLI runner and **Use base resume** still work; scoring unaffected |
| A question on the never-store list | No draft, no save, no fill: "This one is yours" |
| Behavioral question without story facts | No draft until you write them |
| Pasted posting duplicates a job | Existing group offered |
| Nightly merge absorbs a group or application with a packet | Packet re-pointed or `abandoned` under the winner; merge completes |
| Fetch refused by robots or page empty | Paste the text |
| Playwright missing | HTML for print-to-PDF |
| Already applied to this employer and title | Warning before Generate |
| *1e:* page has no structured data | Page-text capture, previewed in an editable box before it is stored; or right-click a selection |
| *1e:* restricted page (`chrome://`, Web Store, PDF viewer) | Popup says Chrome does not allow reading it and offers New packet with the URL filled in |
| *1e:* posting is in an embedded ATS iframe | Popup offers **Fetch from** a supported ATS on your click (server re-checks the host; robots.txt decides), or right-click a selection in the iframe |
| *1e:* single-page board kept the first job's JSON-LD | Stale posting detected against the page's board id; previewed, not added |
| *1e:* second click on the same page | Same capture key: the stored result is shown (Already in jobhunter); **Capture again** re-reads without a reload |
| *1e:* menu capture then popup, or a double shortcut | Single-flight in the worker, and dedupe under one `BEGIN IMMEDIATE` on the console: one group |
| *1e:* **Link them** while a score is running | 409, nothing written; link after it finishes |
| *1e:* the popup's group was merged away | 404 with the current group; the popup follows it |
| *1e:* host on the off list, or LinkedIn/Indeed notice not accepted | Nothing injected; the popup says why |
| *1e:* no title or employer found | Previewed; you type them before Add |
| *1e:* the job is already in jobhunter | **Already in jobhunter → Open**; nothing written (a `capture_log` row only) |
| *1e:* same employer and title as an existing job, different URL, or a URL that names no single posting | `possible`: **Open** or **Add as new** (and **Add this description** on an empty manual group); nothing written until you choose |
| *1e:* the nightly run later ingests a captured posting | Joins the captured group by URL key or board id; no second paid score ([Later ingest](#a-later-ingest-of-a-captured-posting)) |
| *1e:* popup closed mid-request | The worker finishes; the result waits in `storage.session` and the badge; reopening the popup shows it |
| *1e:* worker stopped mid-request, or a slow synchronous score | Score runs on the console (202); the popup rebuilds from `status` when reopened. A write whose answer was lost is resent with the same `action_id` and gets the first answer |
| *1e:* Score it estimate changed before confirm | 409 with the new estimate; nothing run; confirm again |
| *1e:* a hostile page's JSON-LD (huge, script in the description, false employer) | Size limits and 413; text only, never rendered; you see the fields in the popup before scoring; no write is possible without the token |
| *Phase 2:* extension not paired, token revoked, or console down | Panel says so and links to pairing or `jobhunter console`; the page is untouched |
| *Phase 2:* extension older than `min_extension` | 426; panel asks you to reload it from your checkout |
| *Phase 2:* request from a web page, wrong Origin or Host | Refused and logged; no data returned |
| *Phase 2:* no rendered resume PDF | Start refuses and links to export, or you choose to attach it yourself |
| *Phase 2:* site ignores the automatic attach (`attach_ok` not seen in 15 s) | Falls back to manual attach for the session; `attach_failed` recorded for you to confirm before `ats_forms.py` changes |
| *Phase 2:* parse filled something wrongly, or overwrote a value that was there | `differs`, listed; **Use packet value** or **Restore previous value** only on your click |
| *Phase 2:* parse does not settle in 45 s | Reported; classified as is |
| *Phase 2:* service worker killed while you review | Nothing lost: the panel holds the session; the next click wakes a fresh worker that reads the token from storage |
| *Phase 2:* extension reloaded with an ATS tab open | Start re-injects the content script into the tab's frames |
| *Phase 2:* tab navigates to another posting during a session | Writes are bound to the snapshotted `documentId` and frame URL, so none reach the new page; the session ends |
| *Phase 2:* a session left open (crash, closed panel, fetch-based submit) | Shown at the next Start and closed on your click; closed by the console after 2 idle hours |
| *Phase 2:* resume bytes corrupted in transit | Size and hash check fails before the `File` is built; nothing attached; error shown |
| *Phase 2:* a write does not stick (`isTrusted` check, custom widget) | Field becomes `listed` for you |
| *Phase 2:* ATS changed its form | Fewer known-name matches; more fields listed; fixture refresh bug |
| *Phase 2:* your other helper fills after Start | Its values show on the next re-read; every write re-checks that its target is still empty, and the extension never overwrites a non-empty field except by **Use packet value** or **Restore previous value** |
| *Phase 2:* challenge or login wall | `challenge`; tab is yours |
| *Phase 2:* prompt injection in page text | No model reads the page; a field label sent to **Draft** is data in the question slot, the runner has no tools, and the draft goes through factcheck and your **Insert** |

## Testing

All offline; `tests/conftest.py`'s network guard stays as is. Phase 1e's tests are in
[Tests (1e)](#tests-1e); the API contract, extension lint and e2e harness rows below are built in
1e and extended in phase 2. The Anthropic client is always
mocked. No test runs the real `claude` binary: the CLI runner is tested against a fake `claude`
script put first on `PATH` in a temp dir, and an autouse guard makes the binary lookup raise
otherwise ([CLI runner](#cli-runner)). The Python socket guard does not cover the Chromium
subprocess, so the extension e2e blocks the network at the browser level (below). No test
spends credits or subscription allowance; the one live CLI smoke test is `@pytest.mark.live`.

| Area | How |
|---|---|
| Generator | Canned JSON from a mocked client; streaming; effort and thinking set; resume and letter are separate calls with separate schemas, and Add cover letter creates no resume version; the request body contains no saved `packet_answer` values and no `answers:` content (seeded with a link, notice period and template; none appears in the body) |
| CLI runner | As in [CLI runner](#cli-runner): exact argv including `--output-format stream-json --verbose --system-prompt --effort medium --safe-mode --max-budget-usd` (and no `--effort` on the Haiku call); the fixed cwd is empty before each call; the pass-through variables arrive and the child environment lacks `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` and `CLAUDECODE` (the test sets all of them in the parent first) and has `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; the recorded `stream-json` transcript is parsed for `structured_output`, `usage` and `model`; `apiKeySource` other than `none`, `tools` other than exactly `["StructuredOutput"]` or a non-empty `mcp_servers` kills the child before any further line is read; an overage `rate_limit_event` logs the call as paid; a non-claude.ai `auth status` turns the runner off and the state persists in `apply-runner.json`; on any failure the page offers the API run and **no SDK call happens without the click**; logs tier `packet-cli` at `cost_usd = 0`; the autouse guard has its own test |
| Factcheck | Table tests: new employer, changed date, invented number, unlisted skill, invented certification, "contributed" → "led", added team size, added "expert", employer sentence without a quote (with and without notes), bad posting quote, behavioral sentence without `S*`/`L*`, confirmed line |
| Editor | Sources and confirmations carry forward for unchanged items; an edited item is re-checked and keeps its badge if it still fails; a new item without a source blocks Mark ready; adding a letter to a `ready` packet returns it to `draft` |
| Never-store | One test per entry point: Save as answer, `save_packet_answer`, Promote to /prefs, `/prefs` answers save, loading a hand-edited `preferences.yaml` with a 'Desired salary' answer (entry dropped with a warning, `Profile` and a daily run unaffected, packets do not offer it), question draft (no client call), the `/ext/v1` fields step (no value requested), a value sent anyway (dropped), classify row 1 beats every other row, a Draft request for a never-store label. The vector file runs word-boundary negatives ("managed", "language", "design", "generate", "collaborate", "trace") and option-text groups ("Decline to self-identify") |
| Spend | Packet tiers excluded at every site: `remaining_daily_budget`, `weekly_remaining`, `backfill.estimate`, `/costs` totals against the scoring caps, the dashboard KPI and `over_cap`, `inbox.weekly_cost`; `[apply] daily_cap_usd` refuses before the API call |
| Paste | `/apply/new` URL-only, text-only, dedup, robots refusal (mocked `FetchContext`); job at `normalized` and skipped by a daily run; `description_rev` unchanged; Score this group now records the pass and scores one group; Open application hidden without a URL; Pasted Sankey node; cross-site POSTs refused |
| Merges | As in [Merges and undo](#merges-and-undo) |
| Loop | Through the routes: the "Did you apply?" *Yes* POST, a mail proposal accept and a manual event each set `resume_version` and add the `attachment` rows; followups rule |
| Console Host check | A GET of `/packet/{id}` with `Host: evil.example:8808` is refused without `--allow-remote`, allowed with it |
| ATS hosts | Workable rule in `ats_rules.py`; the manifest's `host_permissions` and optional hosts equal the `ats_rules` hosts for the supported ATSs |
| Export | HTML render golden file; PDF when the `browser` extra is present (`e2e`) |
| *Phase 2:* API contract | FastAPI `TestClient` on every `/ext/v1/` route: GET refused (POST only), no token, wrong token, wrong Origin, an `https://` page Origin, no Origin, rebinding Host (`evil.example:8808`), `--allow-remote` still refusing a non-loopback Host, preflight answered only for the paired origin, 426 below `min_extension`; pairing: code single-use, expiring, five failures void it, a code made by the CLI process accepted by the console process, an extension ID other than the pinned one refused, re-pair revokes. Request and response models exported to `extension/api/v1.schema.json`; a test fails if the checked-in file differs |
| *Phase 2:* classifier | `fill_classify` on before/after field lists recorded from fixtures and the spike: prefilled and unchanged kept, prefilled and overwritten by the parse `differs`, parser-filled compared, gaps mapped, never-store first, base-resume entries, settle timeout; snapshots never written to SQLite |
| *Phase 2:* fixtures | `tests/fixtures/ats_forms/<ats>/`: rendered DOM of public, empty forms, plus small hand-written pages that mimic a resume-parse prefill (with a delayed parse), a parse that overwrites a prefilled value, a returning-candidate prefill, a React-controlled input, a checkbox, a react-select combobox, Ashby-style yes/no buttons, a dropzone, an `isTrusted` check, an employer page embedding a Greenhouse iframe, an EEO radio group whose options are "Female" / "Male" / "Decline to self-identify", a Jobright-like click-time filler with corner buttons, React-generated ids that change on re-render, a field that appears during the parse, and a `fetch` submit from a labelled `type=button` |
| *Phase 2:* fixture lint | Fails on emails other than `@example.com`, phone numbers, long tokens in attributes, `<script>` in captured fixtures, hidden inputs |
| *Phase 2:* extension lint | Parses `manifest.json`: permissions and hosts exactly as derived above, pinned `key`, no `externally_connectable` or `web_accessible_resources`. Scans `extension/` for the submit-path list in [The stop before Submit](#the-stop-before-submit) outside `act.js`, and for mouse, pointer or drag event construction in `act.js` outside `safeDispatch`; for `postMessage`, `onMessageExternal`, `onConnectExternal`, `chrome.debugger`, `eval`, `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` and `srcdoc` anywhere; for `fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource` and `sendBeacon` in content scripts; and requires the `setAccessLevel` call in the worker |
| *Phase 2:* extension e2e (`e2e` marker) | Playwright `launch_persistent_context` with `channel="chromium"` (headless extensions need it; the existing session `browser` fixture cannot load extensions), `--disable-extensions-except` / `--load-extension`, and `--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"`, so any non-loopback request from a page, the service worker or Chromium itself fails at the browser. `context.route` serves fixtures on their real ATS URLs. The side panel is driven as `chrome-extension://<id>/panel/index.html?tab=<tabId>` in an ordinary tab, bound to the fixture's tab. The console runs in-process on an ephemeral loopback port with a mocked drafting client, paired through the options page. Covers pairing, the two-step snapshot (the EEO group's value never reaches the console), attach by `DataTransfer` and by drop with the received file's size and hash equal to the packet PDF, a delayed parse settled by `parse_done`, fill on a React input and a checkbox (ends checked), Ashby-style buttons `listed`, no write to `yours` or `keep` fields, a write refused after the field filled itself, a write refused after navigation, the iframe case matched by frame URL, the worker stopped mid-session (CDP `ServiceWorker.stopAllWorkers`) before Fill gaps, re-injection after an extension reload, the Submit outline, and the **submit counters** of [The stop before Submit](#the-stop-before-submit) at zero across a full session on every fixture, then moving when the test submits |

**Phase 2 acceptance per ATS: one supervised live dry run** on a posting you mean to apply to,
with the pass criteria of the [live spike](#phase-2-gate): check every field, the attach and the
Submit outline, then submit it yourself or close the tab. **Fixture capture** happens once, by
hand, in a fresh profile with no ATS sessions or extensions, direct board URLs only (Greenhouse
`/embed/` and Ashby `/api/` are disallowed to crawlers), saved before any typing and stripped of
scripts, hidden inputs and tokens. Captured fixtures carry no behavior; behavior comes from the
hand-written mimic pages and the live runs.

## Phased rollout

Agent-effort estimates, in the style of [009](009-roadmap.md):

| Phase | Work | Effort |
|---|---|---|
| **1a** | Packet migration `0029`, merge and undo handling, Prepare (`p`, detail button), **New packet** (`paste-manual` at `normalized`, dedup, robots-aware fetch), Score this group now, Pasted Sankey node, packet page (in progress on `bug/288631b-apply-1a`) | **1-1.5 d** |
| **1b** | Generator: CLI runner (env allowlist, `--safe-mode`, `auth status` check, `stream-json` parsing with init-line kill and overage handling, runner-off state, autouse guard, recorded transcript, live smoke test, `apply draft` command) and API fallback on click (schema validation, effort, caching, own cap), `packet_runner` migration, two model keys, separate resume and letter calls, measured estimate per kind; factcheck with claim strength and employer claims; structured cited-line editor; versions; cover letter on request; employer notes; question drafts with story facts; entailment pass on by default; packet tiers excluded at every scoring-cap site; `/costs` lines | **3-3.5 d** |
| **1c** | Export: PDF, text, Markdown | **0.5 d** |
| **1d** | Never-store table (word-boundary matcher, vector file) and its enforcement, `Answers` model loaded apart from `Profile`, saved answers with reuse, the `/prefs` Application answers section, checklists, `attach_sent_packet` on every applied path including `answer_prompt`, loopback-Host check on every method, Workable rule in `ats_rules.py`, `/followups` item, `apply stats` (counting `capture_log` too when 1e has landed) | **1-1.5 d** |
| **1e** | **Capture** ([above](#phase-1e-capture-from-any-job-page)): plumbing spike; extension skeleton (pinned key, manifest with `activeTab` and `_execute_action`, worker with popup state, single-flight and off-list check, popup with editable preview, options, context menu); extraction (one self-invoking file: raw JSON-LD, microdata, site table, visibility-aware page-text walker, selection); pairing on `/prefs` and CLI, `/ext/v1/` security and middleware branch, versions and 426; `apply/capture.py` (map, URL choice, add-or-preview, board ids, dedupe under `BEGIN IMMEDIATE`, locations at insert), `capture`, `capture/fetch`, `estimate`, `score` (202), `status` and `prepare` routes; `capture_log` with `action_id` and per-action routes; `job_group.score_on_request` across the nightly sites; the `score.py` start and run split; the board-host no-fetch list; LinkedIn and Indeed Apply control, `job_board_ref`, click-through offer and `link_same_job`; the later-ingest rule in `group_pending`; `/captured`; Score this group now on `/job/{id}`; API contract tests, extension lint, extraction fixtures with mutation checks, Playwright e2e harness; live check with you | **3.5-4 d** |
| | *Gate: 4 weeks of use, plus the spike* | |
| **2a** | **Extension spike**, what 1e did not prove: content scripts on ATS hosts and in iframes, frame URL, re-injection under the `activeTab` grant, `setAccessLevel` against content scripts, side panel from the popup, base64 transfer, `safeDispatch` refusals; then the live check with you on Lever or Ashby and `job-boards.greenhouse.io` | **0.5-1 d** |
| **2b** | Console side: fill routes on 1e's `/ext/v1/`, `fill_session` with idle close, two-step snapshot held in memory, `fill_classify`, base resume parse, `ats_forms.py` mappings, attach methods and signals, Draft for row-8 fields, API contract tests for the new routes | **1.5-2 d** |
| **2c** | Extension: panel-held session bound to its tab, **Fill this form** in the popup, content scripts across frames with re-injection and the reload fallback, stable field keys, `act.js` helpers and `safeDispatch`, attach with fallbacks, settle observers, `documentId`-bound writes, Submit outline, corner buttons beside Jobright's, optional hosts | **2-2.5 d** |
| **2d** | Fixture capture and mimic pages, fixture lint, extension lint for content scripts, e2e with submit counters on 1e's harness; supervised dry run per ATS | **1-1.5 d** |
| | **Total** | **≈ 14-18 d** (phase 1 ≈ 9-11 d, phase 2 ≈ 5-7 d) |

Phase 1 is finished before any phase 2 work starts (your decision). 1e needs only 1a, and it
is built alongside 1b (your decision, 2026-10-10). Workday support comes
after the four public ATSs, then NEOGOV (your decision), each as its own bug after the
multi-step design, once you have signed in and a fixture can be captured. The per-ATS order is
recounted from `apply_link` once packets resolve real apply URLs: today's database holds
aggregator URLs only, so it cannot rank them yet.

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
- **Resume sent as Stage 2 sends it; no header placeholders, deny rule or sentinel test.**
  Reason: the resume header already goes to every scorer, so hiding it here protected nothing.
- **Packet attached by `attach_sent_packet` on every applied path, including `answer_prompt`
  (revision 6).** Reason: the mail path records most of your applications, and the "Did you
  apply?" *Yes* does not go through `tracking.add_event` today.
- **Drafting runs `claude -p` on your subscription first, the SDK as fallback (revision 5).**
  Reason: your decision; packets then cost no API credit. `[apply] daily_cap_usd = 1.00` applies
  to API spend; subscription calls are counted and logged. The CLI runs with no tools, MCP,
  settings or session so a posting cannot steer it into your files.
- **Export PDF, text and Markdown; no `.docx`. Cover letters on request only. Entailment pass on
  by default.** Reason: your decisions (2026-10-10).
- **Phase 2 is our own Manifest V3 extension, not Claude in Chrome (revision 5).** Reason: you
  proposed it; deterministic code on your click replaces a model acting in the page, so the
  restricted launch, hook, guard userscript, confirm dialog and dedicated profile are no longer
  needed, and page content leaves the machine only as field labels and options inside
  jobhunter's own drafting call, never screenshots.
- **Resume-first, gaps only.** The site parses your attached resume first; fields that were
  filled before or by your other helper are kept; the extension fills mapped gaps and offers
  drafts on your click. Reason: your direction, and you already use other fill helpers.
- **Classification and mapping stay in Python; the extension asks the console.** Reason: one
  tested copy of the rules, including never-store.
- **`/ext/v1/` behind a paired per-install token, the extension's Origin and a loopback Host,
  even with `--allow-remote`; content scripts never talk to the console.** Reason: the console
  had no auth, and any page or rebinding host must stay out.
- **No path to Submit in the extension's code, enforced by lint and e2e; Submit visibly
  marked.** Reason: the never-auto-submit rule, now provable in code rather than layered around
  a model.
- **No `chrome.debugger`.** Reason: broad power, a visible debugging banner, and trusted
  synthetic input is close to evading bot checks. Fields that need trusted input are yours.
- **Dedicated Chrome profile optional; default is your everyday profile.** Reason: the extension
  only acts on allowed hosts on your click and sends no page captures; your other helper is
  there.
- **Load unpacked from `extension/`, pinned ID via a public manifest `key`, API versioned at
  `/ext/v1/` with `min_extension`.** Reason: single user, no Web Store review, and pairing must
  survive moving the checkout.
- **Revision 6, from the review of revision 5** (all nine blocking and all minor findings
  taken, two of them in part as noted):
  - **CLI runner hardened:** explicit child environment without API keys, base URLs, cloud
    provider switches or `CLAUDECODE`; non-essential traffic off; `stream-json` output parsed
    for `apiKeySource`, `structured_output`, `usage` and `total_cost_usd`; API-key auth is a
    failure and is charged to the apply cap; `--system-prompt` and `--effort medium` so both
    runners send the same request; `--max-budget-usd` as a backstop. Reason: an inherited
    `ANTHROPIC_API_KEY` would have billed the API while we logged $0.
  - **API fallback only on your click.** Reason: subscription failures (limit, login) are known
    only after running, and 018 requires no silent switch; a click is also what the cap assumes.
  - **Two model keys, a `runner` column, separate resume and letter calls, estimates from
    `packet_document.cost_usd` per kind.** Reason: one key cannot hold a CLI alias and an API
    ID; Add cover letter must not replace a reviewed resume.
  - **`/ext/v1/` is POST only.** Reason: Chrome most likely sends no `Origin` on an extension's
    GET to a permitted host; the spike records the headers either way.
  - **One `safeDispatch` helper, widget behavior as code in `act.js`, data names only targets;
    lint over every submit and synthetic-event path; e2e counters on clicks, submit events and
    submit endpoints, with a positive control.** Reason: the revision 5 lint and its "no submit
    event" test passed even if the extension clicked a fetch-based Apply.
  - **Ashby-style button pairs are `listed`.** Reason: buttons are never a dispatch target; you
    confirmed in revision 7 that such widgets are yours.
  - **Settle on a per-ATS `parse_done` signal, attach judged by `attach_ok`, the file input never
    counts as a change, writes re-check emptiness.** Reason: server-side parses finish after a
    3-second quiet window, and a generic diff misreads both attach success and failure.
  - **Two-step snapshot; never-store decided only in Python; snapshots in memory only.** Reason:
    a JS copy of the matcher would drift, and a choice group's selection is a value too.
  - **Row 2 split into `keep` (unchanged) and `differs` (overwritten by the parse).** Reason: a
    parse that overwrote your helper's value was otherwise invisible.
  - **Stateless worker, session in the side panel bound to its tab, writes bound to
    `documentId` and frame URL, sessions closed on navigation, at the next Start or after 2 idle
    hours, re-injection after reload, packet matched on ATS frame URLs.** Reason: MV3 lifecycle,
    SPA navigation and embedded forms.
  - **Pairing bound to the pinned ID, 50-bit code, five tries, pending code on disk; token storage
    restricted to trusted contexts; lint over HTML sinks, network APIs and external-message
    listeners.** Reason: another extension or a local brute force must not pair, and untrusted
    text must not render as markup in the token-holding panel.
  - **Loopback-Host check on every console route (phase 1d).** Reason: packets add sensitive
    pages that a rebinding page could otherwise read with a GET.
  - **Manifest hosts derived from `ats_rules.py`, Workable rule added.** Reason: EU hosts were
    missing and Workable was not counted.
  - **Never-store matching by word boundary with a vector file; option-text markers; more
    address and pay labels; `Answers` loaded apart from `Profile`.** Reason: substring matching
    refused most behavioral questions, and a bad hand edit must not stop the nightly run.
  - **Also:** packet tiers excluded at every scoring-cap site; an 008 deviation for the CLI
    runner's terms; `jobhunter apply draft` for 018; e2e network blocked at the browser; phase 2
    requires a rendered PDF or manual attach; the gitleaks allowlist for the manifest key;
    data-dir text updated for `e7da19f`; the checkbox helper clicks once instead of
    set-then-click; resume bytes base64 with a hash check; the spike split into offline
    plumbing and a live check with criteria.
  - **Taken in part:** the review's "Greenhouse may not parse uploads" is unverified, so the
    parse check moves to Lever or Ashby rather than dropping Greenhouse; "use `setAccessLevel`
    on `storage.local`" depends on Chrome support, so the spike decides between that and
    session storage.
- **Revision 7, from the check of revision 6 and your answers (2026-10-10):**
  - **The `init` check requires `tools == ["StructuredOutput"]` exactly and `mcp_servers == []`,
    and kills the child on any mismatch or on `apiKeySource` other than `none`, before the model
    call.** Reason: `--json-schema` adds that one tool after `--tools ''`, so "empty" would fail
    every call; killing on `init` means a misconfigured auth bills nothing.
  - **Overage counts as paid; `claude auth status` must show a claude.ai first-party login;
    `--safe-mode` added; Haiku gets no `--effort`; one fixed empty cwd; the env allowlist passes
    `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN` (subscription auth) and proxy and CA
    settings; runner-off state in a data-dir file.** Reason: the check's minors; `apiKeySource`
    alone does not catch third-party providers or paid extra usage.
  - **`runner = "cli"` by default.** Reason: you said subscription data use, training included,
    is fine.
  - **`jobhunter apply draft <packet_id>` is the canonical host command; 018 aligns.**
  - **Host check on every method applies to requests with browser fetch metadata; `TestClient`
    is unaffected.** Reason: keeps today's rule shape and existing tests, while a rebinding
    page's GET always carries `Sec-Fetch-Site`.
  - **`/ext/v1` Host allowlist adds `[::1]` and uses the published port; CORS headers named;
    `safeDispatch`'s word rule looks only at the target and its enclosing control; lint adds
    other click forms; settle mechanisms named; appearing fields count as empty; Ashby attaches
    to the Autofill box; stable field keys; console restart and re-injection fallbacks; a
    multi-step design is a prerequisite for Workday and NEOGOV.**
  - **Coexist with Jobright:** click-only, run after Jobright, treat its fills as filled, and add
    jobhunter corner buttons on resume and cover-letter widgets beside Jobright's Customize.
    Reason: you use Jobright, which fills on click and reuses earlier answers.
  - **Your answers:** fill empty mapped fields from `/prefs` answers on click; Draft suggests an
    option for unknown selects and radios, applied on click; widgets that ignore synthetic events
    are listed and `chrome.debugger` stays out; Workday before NEOGOV, recounted once packets hold
    real apply URLs; pairing by one-time code; phase 1 finished before phase 2; gate of 8 ready
    packets in 4 weeks; the deviations approved.
- **Revision 8, phase 1e Capture (2026-10-10):**
  - **A Send to jobhunter button, built in phase 1 without a gate, as the extension's first
    slice.** Reason: you asked for it; it needs no form writes, and it carries the pairing,
    `/ext/v1/` security, pinned ID, versions and Playwright harness that phase 2 would otherwise
    build in its spike, so those are proven on a low-risk feature first.
  - **`activeTab` allowed; still no `<all_urls>`, `tabs`, `debugger` or standing content
    scripts.** Reason: reading a job page on any site needs host access to it; `activeTab` gives
    exactly the clicked tab, only after your click, only until it navigates, with no install
    warning. `contextMenus` added for right-click capture, with no `link` context.
  - **Extraction order JSON-LD, microdata, a site table (empty at launch), page text; your
    selection overrides the description.** Reason: most ATSs and many career pages publish
    `JobPosting` data for search engines; selectors are added only where measured.
  - **Structured and selection captures add in one click; page-text captures are previewed
    first.** Reason: one click is what you asked for, and page text may carry page furniture
    (your name in a header) that should not be stored or sent to a scorer unseen.
  - **Captured HTML is converted to text on the console and never stored or rendered.**
    Reason: page content is untrusted.
  - **Reuse `apply/paste.py` (`paste-manual` at `normalized`) and `apply/score.py` (estimate
    token and claim) unchanged; capture never scores, prepares or queues a re-score.** Reason:
    1a already guarantees a pasted posting is scored only on request; a capture is the same kind
    of row.
  - **No application or packet on capture; `/captured` and Score on `/job/{id}` so an unscored
    capture is findable.** Reason: you want to collect and score, not necessarily to apply; an
    application at `preparing` would clutter the pipeline.
  - **LinkedIn and Indeed ids as dedupe keys inside `find_duplicates` only; nightly
    `normalize_apply_url` unchanged.** Reason: a LinkedIn page you view carries `currentJobId`
    while a stored link has `/jobs/view/<id>`; changing nightly keys would risk merges.
  - **No site extractors for LinkedIn or Indeed, a one-time notice there, and a per-host
    off switch.** Reason: their terms restrict plugins and automated collection; the click-only,
    one-page design is the most defensible posture; you allowed it on 2026-10-10 (revision 9).
  - **`capture_log` without a foreign key.** Reason: an `existing` match may be an ingested
    group a nightly merge later deletes (#78).
  - **Phase 2's spike shrinks to what content scripts and form writes add; phase 2 adds
    `sidePanel`, ATS hosts and content scripts to 1e's manifest, and opens its panel from the
    popup.** Reason: 1e proves the channel; one action, one popup.
- **Revision 9, from the review of revision 8** (Opus, xhigh; 7 blocking and 16 minor findings,
  all taken):
  - **The extension reads, the console decides.** JSON-LD objects go to Python as parsed;
    mapping, URL choice, dedupe and add-or-preview are tested Python. Reason: one tested copy of
    the rules, and the extraction file no longer needs to read `.value`.
  - **`capture/extract.js` is one self-invoking expression with no top-level declarations.**
    Reason: the isolated world persists, so a second injection of a file with a top-level
    `const` throws and returns null.
  - **Score it returns 202 and the console scores in the background; a `status` route; the popup
    rebuilds from the console; every `/ext/v1/` route answers within 20 s.** Reason: Chrome stops
    a worker whose fetch waits over 30 s, and a synchronous scorer (Jev) can take up to 180 s.
  - **Popup state keyed by tab and normalized URL, single-flight, `capture_id` idempotence,
    dedupe and insert in one `BEGIN IMMEDIATE`, shortcut as `_execute_action`, no auto-capture
    beside a phase 2 ATS frame.** Reason: double triggers inserted duplicate manual groups that
    nothing merges; single-page boards change the job without a navigation. Supersedes revision
    8's "reopening shows the result stored under the tab id".
  - **Add at once only for a clear structured capture, or a menu selection with structured title
    and employer; everything else previewed in an editable box.** Reason: stale single-page
    JSON-LD, several postings, a stray icon selection and page furniture were committed in one
    click. Supersedes revision 8's "structured and selection captures add in one click".
  - **JSON-LD `url` used only when it identifies one posting on the page's or an ATS host; query
    parameters kept except tracking ones; ids reduced only for LinkedIn, Indeed and ATS rules;
    a generic URL match is `possible`, not `existing`.** Reason: many public-sector and career
    systems carry the posting id in the query, and stripping it collapsed different jobs into
    one match that could not be added.
  - **A later ingest joins a captured group by URL key or board id, without a second paid
    score.** Reason: capture makes manual groups the main path, and 1a keeps them out of nightly
    matching.
  - **Also:** menu selections use `info.selectionText` (covers embedded forms); a recursive,
    visibility-aware page-text walker and a mutation test prove read-only; the scorer privacy
    notice and the prefilter line in the popup; `capture/fetch` re-checks the ATS host
    server-side; the off list and the LinkedIn/Indeed notice are checked before injection;
    locations and `EmploymentType` set at insert; `og:site_name` ignored on board hosts; Add this
    description on empty manual groups (by `description_text`) with the mapped fields; the JSON-LD
    `identifier` is not a dedupe key; `capture_log` gains `fetch`, `possible`, `previewed` and
    `capture_id` and is counted by 1d's `apply stats`; the test manifest adds only top-level
    fixture hosts.
  - **LinkedIn and Indeed: read the Apply control, decode an off-site destination locally,
    record Easy Apply, link by board id and ATS URL; click-through linking and linking two groups
    only on your confirm.** Reason: your note that LinkedIn postings are Easy Apply or point to
    the employer's ATS; it joins LinkedIn alerts (`430a0e7`), the LinkedIn page and the ATS
    posting as one job. Reading that one control is the only board-specific rule, read-only, and
    nothing on linkedin.com or indeed.com is ever fetched or clicked.
  - **`job_board_ref` keyed by (board, board id), referencing a job, not a group.** Reason:
    `apply_link` holds one row per group, and merges move jobs between groups, so the link
    follows without a merge change.
  - **Your answers (2026-10-10):** capture on LinkedIn and Indeed is allowed after a one-time
    notice, with no site rows beyond the Apply control; 1e is built alongside 1b; the 001
    capture clause is approved.
- **Revision 10, from the check of revision 9** (Opus, xhigh; 4 new blocking and 18 minor
  findings, all taken):
  - **`action_id` is random per user action, reused only to resend; each follow-up is its own
    route with its own log outcome; non-write answers are re-evaluated, not replayed.** Reason: a
    hash of tab, URL and `documentId` replayed the first answer to every later action on the same
    page (Add, Add as new, Link them, Capture again). Supersedes revision 9's `capture_id`.
  - **"Scored only on request" is a group flag (`job_group.score_on_request`), checked by the
    prefilter, the screen, re-score plans and the console; canonical choice is for content only.**
    Reason: `_refresh_group` makes a full ingested copy canonical, so a source check on the
    canonical job would have let the nightly screen pay for a capture you chose not to score.
  - **`group_pending` matches ingested jobs to captured ones by URL key (one posting only), by
    board id through `job_board_ref` to any group, and by its near-duplicate test; a live claim
    or open batch counts as scored.** Reason: NLx copies carry aggregator URLs, and URL keys alone
    missed them.
  - **`link_same_job` refuses while either group is being scored; the kept-group table covers
    every case and keeps the scored group when only one is scored.** Reason: a merge during a
    background score would delete the group it writes to and lose the spend record.
  - **Also:** `score.py` split into a synchronous claim and a background run on its own
    connection; status order scored, submitted, in progress, stopped; the Apply-control match
    needs one posting and agreeing titles before an automatic link; board hosts on an
    `applylink` no-fetch list (no robots.txt request to linkedin.com); `unresolved` and `blocked`
    apply links may be replaced; tracking parameters served by `version`; fail closed without a
    console and before the notice (selection discarded); single stale JSON-LD previewed; in-flight
    timeout; no text in `storage.session`; 404 with `current_group` for merged groups.
- Carried: salary, EEO and self-ID never stored or filled; New packet standalone; Opus for
  generation; claim-strength and employer-claims checks with the cited line beside every line;
  `paste-manual` source; USAJOBS evidence lines only; Open application through `/apply/{id}`;
  snapshot before and after the attach, classify in code.

## Open questions

None for you right now. Revision 8's three questions were answered on 2026-10-10 (see the
revision 9 decisions).

One fact for you to act on, not a design question: Score it uses `[scoring] screen_scorer`,
which your `config.toml` does not set, so it is the default Anthropic Haiku batch, while every
screen score you have was made with Jev through `--scorer`. You have been asked to set
`screen_scorer = "jev:typesafe/jev-1.13"`; the spec covers both paths either way.

Still for the spikes to measure: in 1e, the headers Chrome sends, Local Network Access,
`setAccessLevel` on `storage.local`, how long a worker `fetch` may wait, `action.openPopup()`
after a menu click, and whether the shortcut can grant `activeTab` in a test; in 1b, `--safe-mode` with the subscription login and
`--json-schema` with `--tools ''`; in 2a, iframe re-injection under `activeTab`, Ashby's file
controls, and where the corner buttons sit beside Jobright's.

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

### Revision 5: our own extension, and your phase 1 decisions

On 2026-10-10 you asked: *"What if we had a chrome plugin that could talk locally to job
hunter?"* and approved revising the spec for it. Phase 2 is now a Manifest V3 extension loaded
unpacked from `extension/`: content scripts on the allowed ATS hosts (iframes included), a side
panel, and a service worker that is the only part talking to the console's new, paired
`/ext/v1/` API. It attaches the targeted resume, lets the site parse it, fills mapped gaps and
offers drafts on your click, and has no code path to Submit. Removed with Claude in Chrome: the
restricted Claude Code launch, allowlist, PreToolUse hook, `/apply-fill` skill, guard userscript
and confirm dialog, the required dedicated profile, and the 008 deviation for screenshots.
Phase 1 records your decisions: drafting by `claude -p` on your subscription with the SDK as
fallback under `[apply] daily_cap_usd = 1.00`; export PDF, text and Markdown; cover letters on
request; entailment on by default. Phase 1a's scope is unchanged. Estimate ≈ 9-12 d (was
8-10.5 d): +0.5 d for the CLI runner, +1 d for the extension and its API over the revision 4
helper.

### Revision 6: review of revision 5

An adversarial review (Opus, xhigh effort, 2026-10-10) found nine blocking and 36 minor issues,
all surviving a skeptic pass. All are taken, two in part; see the revision 6 entry under
[Decisions](#decisions-recorded-on-7fa29db). The largest changes: the CLI runner gets a scrubbed
environment and reads its own auth source and usage from `stream-json`, so it cannot bill an API
key silently; the API fallback needs your click; `/ext/v1/` is POST only; the stop before Submit
is one refusing dispatch helper with lint and e2e counters that would fail if it broke; settle
and attach use per-ATS signals; the never-store decision stays in Python, with a two-step
snapshot; the "Did you apply?" *Yes* path attaches the packet; the console's loopback-Host check
covers GETs. Names now match phase 1a on `bug/288631b-apply-1a` (`0029_application_packet`,
`apply/packets.py`, `apply/paste.py`, `apply/score.py`, `core/manual_sources.py`). Estimate
≈ 11.5-15 d (was 9-12 d): +1-1.5 d in phase 1 (runner hardening, spend sites, Host check, loop
fix), +1.5-2 d in phase 2 (two-part spike, dispatch helper, session binding, e2e counters).

### Revision 7: check of revision 6, and your answers

An Opus check at xhigh effort (2026-10-10) found eight of revision 6's nine blockers resolved and
the ninth resolved for its flags, plus one new blocker. With `--json-schema`, Claude Code adds a
`StructuredOutput` tool after `--tools ''` filtering, so revision 6's "tools must be empty" check
would have failed every call. Revision 7 requires exactly that one tool, kills the CLI on its
`init` line before the model call when anything is wrong, and takes the 20 minor findings (see
the revision 7 decisions). You answered the open questions. Your fill helper is Jobright, which
fills on click from its sidebar and adds a Customize button to upload widgets, so jobhunter
runs after it, treats its fills as filled, and adds its own corner buttons for the targeted
resume and the letter. Subscription data use is fine, so `runner = "cli"` is the default. The
remaining questions were settled on the defaults: fill from `/prefs` answers on click, option
suggestions on click, Workday before NEOGOV, a one-time pairing code, phase 1 before phase 2, a
gate of 8 packets in 4 weeks, and the deviations approved. The estimate is unchanged at
≈ 11.5-15 d. The multi-step design for Workday and NEOGOV is new work outside that figure.

### Revision 8: phase 1e, Capture

On 2026-10-10 you asked: *"for the chrome plugin it might nice if I am on a job site viewing a
job and it's not in my system I can click a button to get the job description sent to jobhunter
and score it."* Revision 8 adds [phase 1e](#phase-1e-capture-from-any-job-page): a **Send to
jobhunter** toolbar button, right-click item and shortcut that read the tab you clicked under
`activeTab` (structured `JobPosting` data first, then page text, or your selection), send it to
the console, and show whether the job is already in jobhunter or add it as a `paste-manual`
posting through 1a's `apply/paste.py`. **Score it** goes through 1a's `apply/score.py` estimate
and confirm; **Prepare packet** opens phase 1. Nothing is scored automatically. The extension's
manifest, pinned ID, pairing, `/ext/v1/` security, versions and test harness now land in 1e, so
phase 2's spike and console work shrink. `activeTab` and `contextMenus` join the allowed
permissions; the rest of the forbidden list stands. New open questions cover LinkedIn and Indeed
terms, build order, immediate scoring and the 001 wording. Estimate ≈ 12.5-16.5 d (was
11.5-15 d): +2-2.5 d for 1e, -1 d in phase 2 for what 1e already builds.

### Revision 9: review of revision 8

An adversarial review of revision 8 (Opus, xhigh effort, 2026-10-10) found 7 blocking and 16
minor issues that survived a skeptic pass; all are taken (see the revision 9 decisions). The
blockers: a repeat click on one page would fail because re-injected files collide in the
persistent isolated world; Score it with a synchronous scorer outlives the 30-second worker
fetch limit, so it now returns 202 and the console reports progress through `status`; opening
the popup had no rule against capturing again, now keyed by tab and URL with single-flight and
an idempotent `capture_id`; the JSON-LD `url` and stale single-page JSON-LD were trusted in a
one-click commit, now checked and previewed; stripping query parameters lost posting ids on many
career systems, now kept; and dedupe ran outside the write lock, now one `BEGIN IMMEDIATE`. The
console now decides add-or-preview from raw facts the extension sends. A later nightly copy of a
captured posting joins its group without a second paid score.

You then answered revision 8's questions (LinkedIn and Indeed allowed after a notice, 1e
alongside 1b, the 001 clause approved), and added: *"LinkedIn only allows to apply for jobs on its
site with Easy Apply, otherwise it provides an off-site link to Ashby / etc. so we might be able
to follow / check / dedupe entries (this linked in job is actually this other job)."* Capture now
reads the Apply control on LinkedIn and Indeed, decodes an off-site destination locally without
fetching the board, records Easy Apply, and links the board posting, its alert email
(`430a0e7`) and the ATS posting by board id and ATS URL; when the destination appears only on
click, you click Apply, capture the ATS page and confirm the link. The 2x immediate-score
question is withdrawn because your scoring runs on Jev, which is synchronous, and a note explains
that `screen_scorer` must name it. Estimate ≈ 13.5-17.5 d (was 12.5-16.5 d): +1 d in 1e.

### Revision 10: check of revision 9

An Opus check at xhigh effort (2026-10-10) found six of revision 9's seven blockers resolved and
the popup rules partly, plus four new blockers, all taken (see the revision 10 decisions). The
hashed `capture_id` would have replayed the first answer to every later action on a page, so
each action now has a random `action_id` and its own route. "Scored only on request" moves from
the canonical job's source to a group flag, because a full ingested copy becomes canonical and
the nightly screen would have paid for it. Later-ingest matching adds board ids through
`job_board_ref` and the near-duplicate test, which catches NLx. **Link them** refuses while a
score is running. The 18 minor findings are taken, including a no-fetch list that stops a
robots.txt request to linkedin.com that `resolve_group` would send today. You were asked to set
`screen_scorer` to Jev; both scoring paths stay specified. Estimate ≈ 14-18 d (was
13.5-17.5 d): +0.5 d in 1e.
