# 009 — Roadmap

Two passes, decided 2026-10-09:

- **Pass 1 — the happy path.** USAJOBS plus the state boards whose `robots.txt` lets us in,
  preferring ones with an API. Scoring, console and tracking get built on that data.
- **Pass 2 — email.** Job-alert emails, delivered to a dedicated `+jobs` address, cover the
  boards that disallow crawling ([008](008-compliance.md#the-decision-you-have-to-make)).

The `robots.txt` decision is **respect robots, plus email alerts** (options 1 + 2). Nothing in this
roadmap scrapes a disallowed path. `git-bug` v0.10.1 is installed; each bullet is sized to be
one bug.

## Pass 1 — happy path

### M0 — Scaffold

- `uv init`, pin Python 3.13, dependency manifest
- `core/db.py`: SQLite WAL, migration runner, schema from [005](005-data-model.md)
- `core/models.py`: pydantic models
- `core/fetch.py`: `FetchContext` with the content-addressed cache, `fetch_log`, per-source rate
  limiter, **robots enforcement**, conditional requests, retries, cookie sessions
- `cli.py`: Typer skeleton, every stage a no-op subcommand
- Test harness, fixture loader, and the check that no adapter imports `httpx` or `playwright`

**Done when** `jobhunter run --dry-run` prints an empty plan and the schema applies cleanly.

### M1 — Inventory and the open set

- Generate `registry.yaml` from [010](010-source-inventory.md)
- `jobhunter sources verify`: probe every entry URL; record status, family signals, and a dated
  `robots.txt` hash
- Check `robots.txt` on all 54 banks (24 done so far); set `policy` for each
- Classify the 18 unclassified banks
- **Output: the open set.** Every source where the job search path *and* the job detail path are
  allowed. Surveyed 2026-10-09 ([010](010-source-inventory.md)): **national NLx (`usnlx.com`)**,
  KY, MT, NY (NLx), WY (Next.js `/api/`), MI, UT, WI, LA (the one VOS host without robots),
  OK and WA (Salesforce communities, JavaScript-rendered), WV (`Crawl-delay: 10`), MP
- Capture fixtures for each open family

**Done when** every row has a family, a policy and a dated robots record, and the open set is
written down.

### M2 — USAJOBS

- Free API key; `usajobs` adapter ([011](011-federal-and-geography.md))
- `job_locations` many-to-many and `location_scope`
- `list` → `normalize` → `dedupe`, run on real data
- Store USAJOBS `ApplyURI` as the job's apply link ([015](015-apply-links.md))
- Multi-location blending (`Denver, CO +13`)

**Done when** federal jobs for your states are in the DB with full descriptions and correct
blending.

### M3 — Open state boards

API tier first, then HTML.

- **NLx adapter:** national `usnlx.com` plus the MT, NY and KY state sites. National NLx carries
  postings syndicated from many states' job banks, so it may be the biggest single win in the
  project. **Measure its real coverage per state before relying on it.** How much of a blocked
  state's postings shows up in NLx is unknown, and finding out is part of this milestone.
- **Wyoming:** find the `/api/` route behind the Next.js app; `api` tier
- **MI, UT, WI, LA, WV, MP** and the Salesforce boards (OK, WA): `htmlconfig` rows, or a family
  adapter when one exists
- Volume floors and shape assertions per source
- **Apply-link resolver**: redirector unwrapping, hop following with per-hop robots checks, the
  ATS rules table, expiry detection ([015](015-apply-links.md))
- **Yield check:** after a week of nightly runs, count Staff/Principal backend and Rust matches
  per source. Drop sources that never produce a relevant job.

**Done when** the open set runs nightly with health reporting, and the yield numbers are in.

### M4 — Fit scoring

- `profile.py`: resume, `preferences.yaml`, `current_focus`, `profile_version`
- `prefilter.py`: hard constraints, audit reasons, multi-location handling
- `rubric.py`: versioned prompt, anchors, `Screen` schema, raw vs recency-weighted skills
- **Free/paid split:** `comp` and `location` computed in Python; `filter_version` and
  `scoring_version` ([014](014-preferences-console.md#the-design-change-free-settings-vs-paid-settings)).
  This has to land before the first scores exist.
- `screen.py`: Haiku 4.5 through the Batch API, cached prefix, cache-hit assertion
- Verbatim check on evidence quotes
- Buckets A–G
- `FitScorer` interface: Anthropic and OpenAI-compatible implementations
- `deep.py`: Opus 5 shortlist pass
- Spend tracking and a daily cap

### M5 — Console

See [007](007-console-and-tracking.md) and [013](013-dashboard.md). Today dashboard with KPI
tiles and the US map first, then the charts. `/prefs` with live preview and change history
([014](014-preferences-console.md)). Apply button, chain display, click endpoint and the "Did
you apply?" prompt ([015](015-apply-links.md)). Inbox grouped by bucket, keyboard triage, job detail
with highlighted evidence, and the sources, rejected, search and costs pages.

### M6 — Calibration

- `jobhunter eval` and `eval --compare` across prompt versions and scorers
- Label at least 150 jobs through ordinary triage, then tune the rubric

### M7 — Application tracking

Kanban over the append-only `application_event` log, follow-up nudges, contacts, attachments,
and which resume version you sent.

## Pass 2 — email

### M8 — Email alert ingest

See [012](012-email-ingest.md).

- Gmail API OAuth in the app (`gmail.readonly` + `gmail.settings.basic`). The connector
  available in Claude can't create filters, so the app talks to Gmail directly.
- **Every job-alert subscription uses `<you>+jobs@gmail.com`.** One filter on that
  address applies the `jobhunter/alerts` label and skips the inbox, so alerts never clutter your
  main view. A sender-domain filter is the fallback for sign-up forms that reject `+`.
- `jobhunter mail setup` creates the label and filter through the API, and is idempotent
- One alert parser per family (VOS, JobLink, NEOGOV, other), tested against saved emails
- Alert-to-stub mapping, deduped against jobs already found on open sources
- **Partial-description handling for blocked hosts** (see 012): no fetching of disallowed detail
  pages
- Subscribe to alerts on every blocked board. This step is manual and is a checklist in the
  console.

**Done when** alerts from the blocked boards land under `jobhunter/alerts`, never in your inbox,
and appear in the jobhunter inbox as scored jobs.

### M9 — Remainder and ops

- Gmail application matching (proposals only, [007](007-console-and-tracking.md#optional-gmail-matching))
- Class B state-employer sites, under the same robots policy
- `systemd` user timer with an `OnFailure=` notification
- Backfill run

## After Pass 2

### Setup and readiness

See [019](019-setup-and-readiness.md). A `/setup` page, a banner on Today and the inbox until
the essentials are present, and `jobhunter doctor`, all from one set of local, free checks.
S0 (every command resolves the resume the same way), S1 (the checks module), S9 and S10 first;
then `doctor` (S2), the page (S3a), Gmail token age (S5) and the run's "no scorer credential"
line (S7); then the banner and first-run redirect (S3b); last the nightly-run snapshot (S4) and
the free live checks (S6). Docs (S8) once the page and `doctor` exist. Two addenda: in-app help
by topic, packaged with the console so it works in containers and shared with the README (S11a
to S11e), and a config watch that offers Apply or Restart when `config.toml` or a `.env` file
changes (S12a, S12b, on the merged activity indicator; after S1, S3a and S11a). About 11.9 h of continuous run
(range 8.3 to 19 h, 19 tickets by the calibrated per-ticket rule in 019), about 17M subagent
tokens.

**Done when** a fresh install with default config, no Anthropic credential and no jobs
redirects `/` to `/setup` naming the screen scorer, `/inbox` shows the banner, `jobhunter
doctor` exits 1 saying the same, the nightly run's summary says why it did not score, and none
of it made a network call.

### Choosing models

See [020](020-model-selection.md). A `/models` page with one row per model use, each with a
dropdown of usable models, its price, whether its key or Claude subscription is there (local,
free checks; live checks only on click), and Save or Save + re-run (the existing re-score
confirm, never an automatic spend). Choices are saved to `<data dir>/models.toml`, layered over
`config.toml`; an invalid screening or deep choice blocks that use rather than falling back to
another provider. First the catalog (M1) and credential
detection (M3, after 019 S1); then storage (M2), the page (M4) and Save + re-run (M5); then
help (M6), banners on other pages (M7), the config-watch hook (M8, after 019 S12a), the
readiness rows (M10, after 019 S2), and the deep-pass button (M11: pick N, see the groups and the cost, confirm, background run). M9 (`score --submit` only scores new jobs) is independent.
M5 also waits on the per-model spend reservation fixes (git-bug `026b50c`, PR 121, and
`df51dff`). About 8.4 h of
continuous run (the dependency chain binds; range 8.4 to 13.1 h, 11 tickets by the calibrated rule),
about 15M subagent tokens.

**Done when** a fresh install with no Anthropic credential shows the message on `/models` with
a working help link, a model saved there is the one the next nightly run uses, a broken
`models.toml` skips scoring instead of spending on another provider, and no page render, save
or banner spent a credit or made a network call.

### Achievements

See [021](021-achievements.md). An `/achievements` board in the nav, a notice in the activity
band and a one-line strip on Today, computed on read from tables jobhunter already keeps, plus
a `followup_log` so follow-ups leave a record. A1 (migration and write points), then A2 (rules,
`evaluate`, the route and nav entry), then the page (A3), the notice (A4) and the strip (A5)
together; A6 after 019 S1. About 6.3 h of continuous run (the A1, A2, A4 chain binds; 6
tickets by the calibrated rule in 019), about 7.7M subagent tokens.

**Done when** a copy of your database shows the 7 history unlocks on `/achievements` without a
notice, marking a dated follow-up done shows "Achievement: Follow-through" in the activity band
at once, a pipeline run holding the write lock leaves `/` and `/achievements` at 200, turning
achievements off removes the nav entry, strip and notices, and no test or page load constructs
a model client.

## Dependency order

```
M0 ──┬── M1 ── M3 (open boards) ──┐
     └── M2 (USAJOBS) ────────────┴── M4 ── M5 ──┬── M6
                                                  └── M7 ── M8 (email) ── M9
```

M4 needs real data from M2 at minimum, not every source. M8 can start any time after M4 if alert
volume turns out to matter more than expected.

## Breaking this into git-bug issues

Titles are prefixed with the milestone, and each body links its spec section. Labels:
`milestone:Mn`, `pass:1|2`, `area:sources|pipeline|scoring|console|mail|ops`. `risk:high` goes on
the NLx coverage measurement and the M3 yield check, since those two decide how much Pass 2
matters.

## Effort estimate — agent-driven

Estimates assume development runs the way your resume describes: a governed Claude Code agent
fleet, spec-driven, with review and test gates and you as reviewer. "Agent-days" are wall-clock
days with agents working and you reviewing, not person-days.

### What the evidence says

The published numbers disagree with each other, by a lot:

| Source | Finding | Applies here? |
|---|---|---|
| METR 2025 RCT ([summary](https://arxiv.org/pdf/2604.26275)) | Experienced devs were **19% slower** with AI on large, familiar, existing codebases | Weakly. Early-2025 tools, brownfield, no agent fleet. A caution for live-debugging work |
| GitHub Copilot RCT (2023) | 56% faster on a bounded task | Weakly. Autocomplete era |
| Microsoft, Jul 2026 ([reported](https://letsdatascience.com/news/agentic-ai-transforms-software-engineering-productivity-3b662e3d)) | ~24% more merged PRs among Claude Code / Copilot CLI adopters | Measures throughput across mixed work, not greenfield speed |
| 2026 industry write-ups ([e.g.](https://haithembuilds.substack.com/p/the-agentic-shift-a-comprehensive)) | 40–55% speedup on greenfield; 10–30% on routine work; near zero on unfamiliar codebases | Secondary sourcing; directionally useful |
| METR time horizons, Time Horizon 1.1 ([tracker](https://ai2027-tracker.com/predictions/metr-doubling/), [discussion](https://www.lesswrong.com/posts/EYb2K9acKfyG2bome/metr-time-horizons-now-10x-year)) | Frontier agents finish **~14–20 hour** expert tasks at 50% reliability, **~3–4 hours at 80%**; doubling every ~4–7 months | Yes. Every milestone below decomposes into tasks well inside the 80% horizon |
| **Your own record** (resume) | Kumi: 91K LOC + 110K LOC tests in 57 days vs COCOMO II 204 person-months → **~10x**. Ziosec: −90% vs estimates. Terasense: −80% on docs/tests | **Most relevant.** Same method, same operator, measured |

I could not reach METR's or Microsoft's primary reports directly. The figures above come from
secondary summaries and should be treated as approximate.

How to read the spread: the controlled studies measure individuals using assistants on
unfamiliar, existing code. Your numbers measure a spec-driven fleet building greenfield code.
This project is greenfield, specified in detail (these documents), in Python, with small and
well-bounded modules. That is close to ideal for agents, so your ~10x is the right prior **for
the code itself.**

### Why the whole project doesn't compress 10x

Kumi was mostly code volume. A good part of this project's critical path is the outside world,
and agents don't speed that up:

- **Live government servers at 1 request per 5 seconds.** Working out an undocumented search
  API or an HTML layout is bound by the round trip to the server, not by typing speed. METR's
  slowdown result is a fair warning for this kind of work.
- **Batch API turnaround.** Up to about an hour per scoring iteration while tuning the rubric.
- **Real data has to accumulate.** Measuring yield and calibrating against ≥150 labels needs one
  to two weeks of nightly ingest. No agent shortens a calendar.
- **External dependencies.** A USAJOBS API key, Gmail OAuth consent, signing up for alerts on
  each blocked board, and your decisions on profile and policy.
- **Review bandwidth.** Your review becomes the bottleneck. Your Ziosec result came with review
  gates, and the estimates keep them.

So each milestone gets one of three compression factors:

| Work type | Factor | Basis |
|---|---|---|
| Specified greenfield code (schema, CLI, models, console, tracking) | **~8–10x** | Your Kumi/Ziosec record |
| Live integration (scrapers against real sites, API discovery, prompt tuning against batches) | **~2–3x** | Feedback loop bound by external latency |
| Wall-clock / human (data accumulation, labeling, keys, decisions) | **1x** | Not compressible |

### Per milestone

| Milestone | Human est. | Dominant work | Agent est. |
|---|---|---|---|
| M0 Scaffold | 1–2 d | code | **2–4 h** |
| M1 Inventory + open set | 3–5 d | scripted probing, rate-limited | **0.5–1 d** |
| M2 USAJOBS | 1–2 d | code against a clean API | **2–4 h** + key issuance |
| M3 Open state boards + apply resolver | 4–8 d | live integration, NLx coverage, ATS rules | **1.5–2.5 d** + 1 week calendar for the yield check |
| M4 Scoring | 3–5 d | code is quick; rubric tuning waits on batches | **1–1.5 d** |
| M5 Console + prefs + apply button | 4–7 d | code | **0.75–1.5 d** + your UX review |
| M6 Calibration | 2–3 d + labeling | code is quick; needs data | **2–4 h** + **1–2 weeks calendar** |
| M7 Tracking | 2–3 d | code | **3–6 h** |
| M8 Email ingest | 3–5 d | OAuth, filters, per-family alert parsers | **1–2 d** + your subscription time |
| M9 Remainder | 2–4 d | mixed | **0.5–1.5 d** |
| **Total** | **25–45 d** | | **~5.75–11 agent-days** |

**Pass 1 (M0–M7) is about 4.25–8 agent-days.** It is usable after M0 → M2 → M4 → M5, about
2–3.5 days, with the usual caveat: scores need 1–2 weeks of your triage labels before they deserve
trust.
