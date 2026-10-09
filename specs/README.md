# jobhunter — design specs

A single-user system that sweeps all 50 state job banks plus federal USAJOBS, resolves full job
descriptions, uses an LLM to judge fit against your *current* experience, sorts results into
compatibility buckets, and tracks jobs and applications in a local console.

Status: **design, awaiting your approval.** No implementation code written yet.

## Read in this order

| # | Spec | What it settles |
|---|------|-----------------|
| [001](001-goals-and-scope.md) | Goals & scope | What this is, what it explicitly is not |
| [002](002-architecture.md) | Architecture & stack | Python/SQLite/FastAPI, process model, the raw cache |
| [003](003-sources-and-adapters.md) | Sources & adapters | Platform families, query-driven ingestion, adapter contract |
| [004](004-ingest-pipeline.md) | Ingest pipeline | 8 stages, watermarks, the CSV-resolve problem, dedupe |
| [005](005-data-model.md) | Data model | SQLite schema |
| [006](006-fit-scoring.md) | **Fit scoring** | Prefilter → Haiku screen → Opus deep pass, **buckets**, **recency**, calibration, cost |
| [007](007-console-and-tracking.md) | Console & tracking | Bucket-grouped inbox, keyboard triage, application pipeline |
| [008](008-compliance.md) | Compliance | robots.txt findings, rate limits, what we will not build |
| [009](009-roadmap.md) | Roadmap | 9 milestones, effort estimates, ready for `git bug` |
| [010](010-source-inventory.md) | **Source inventory** | All 54 verified URLs + platform family per source |
| [011](011-federal-and-geography.md) | Federal & geography | USAJOBS API, multi-location blending into states |
| [012](012-email-ingest.md) | Email ingest | `+jobs` address, Gmail filters, partial-description handling |
| [013](013-dashboard.md) | **Today dashboard** | KPI tiles, US map with AK/HI, per-state stats and coverage, charts |
| [014](014-preferences-console.md) | **Preferences console** | Edit salary, states, weights, queries; live preview; free vs paid changes |
| [015](015-apply-links.md) | **Apply links** | Button straight to the employer's application, redirects resolved |

## Headline decisions

| Decision | Choice | Why |
|---|---|---|
| Language | **Python 3.13** (pinned via `uv`) | Best scraping + LLM + data ecosystem in one toolchain |
| Storage | **SQLite** (WAL, FTS5) | Single user, zero ops, full-text search built in |
| Console | **FastAPI + Jinja2 + HTMX** | Server-rendered, keyboard-driven, no build step |
| Ingestion | **Query-driven, not full-crawl** | 15x cheaper, more polite, more relevant — *confirmed with you* |
| Screening model | **Haiku 4.5 via Batch API**, Opus 5 on the shortlist | ~$2 per 1,000 jobs; scorer is **pluggable** |
| Output shape | **Compatibility buckets A–G**, computed in Python | A single score is not actionable |
| Browser automation | **Yes — Playwright, minority of sources** | [See below](#do-we-need-browser-controls) |
| Raw-response cache | Content-addressed, gzipped, permanent | Re-parse without re-fetching. The most important decision here |
| Orchestration | `systemd` user timer + Typer CLI | Airflow is wildly oversized for one user |
| robots.txt | **Respected.** Email alerts cover blocked boards | Decided 2026-10-09 |
| Build order | **Pass 1:** USAJOBS + open state boards. **Pass 2:** email | Decided 2026-10-09 |
| Alert address | **`<you>+jobs@gmail.com`**, filtered out of the inbox | One filter catches every board |

**Estimated running cost: ~$28/month** in LLM spend ([006](006-fit-scoring.md#cost)).

## What the research established

Your CareerOneStop link turned the riskiest part of this project from guesswork into a table —
and the follow-up `robots.txt` survey then took a large bite out of it.
Everything below was verified by live probe on **2026-10-09**:

- **54 source URLs**, all reachable — 50 states + DC, Guam, Northern Mariana Islands, US Virgin
  Islands ([010](010-source-inventory.md)).
- **Three platform families cover 36 of 54 sources**, so ~4 code adapters serve two-thirds of
  the system:
  - **25 × Geographic Solutions "Virtual OneStop"** (`/vosnet/`, ASP.NET WebForms)
  - **7 × a single shared "JobLink" codebase** — proven by fetching the same JS bundle from all
    seven hosts and getting the **byte-identical sha256**. Not a similar vendor: the same
    application, seven deployments.
  - **3 × National Labor Exchange** (`usnlx.com`)
- **The JobLink family has a real JSON API.** `GET /ada/r/search/jobs` returns **401 with a
  26-byte body** — auth required, not a missing route. Promoting those 7 sources to `api` tier
  is the highest-leverage task in the project.
- **USAJOBS is the best source in the system** — documented API, free key, full descriptions in
  the search response (so it needs *zero* detail fetches), plus occupational series and GS grade
  as structured seniority signals ([011](011-federal-and-geography.md)).
- **Most of the job banks disallow crawlers in `robots.txt`** — *correcting an earlier version
  of this README, which said only NEOGOV did.* 22 of 25 VOS hosts serve `Disallow: /`;
  all 8 JobLink hosts disallow `/search/jobs` specifically. Respecting robots, **12 of 54
  banks plus national NLx and federal** are reachable by direct collection. Email job alerts become the
  primary channel for the rest. Full survey and options in
  [008](008-compliance.md#verified-robotstxt-survey-2026-10-09).

## How the noise problem gets solved

> *"keyword searching is usually crappy and jobs don't fit based on my most recent experience"*

That is the core problem the design targets, in [006](006-fit-scoring.md). Keywords narrow, the
model judges, you decide:

1. **Stage 0** — board-side keyword/occupation search. A deliberately *broad* net, not a filter.
2. **Stage 1** — deterministic prefilter on hard facts only. Never rejects for missing data.
3. **Stage 2** — Haiku 4.5 reads the full description against your resume, your `current_focus`,
   and free-prose `want` / `avoid` / `done_with` blocks. It scores skills **twice**:
   `raw_skills` (what a keyword index sees) and `recency_weighted_skills` (last 3 years at full
   weight, 3–7 at half, beyond 7 at a quarter).
4. **The gap between those two numbers is the whole trick.** `raw_skills 88` +
   `recency_weighted_skills 41` means *this job matches who you were, not who you are* →
   **bucket F**, collapsed in the inbox and summarized by which stale skill triggered it.
5. **Buckets A–G** with a stated action each — Bullseye, Strong, Stretch up, Lateral, Downlevel,
   Stale match, Mismatch. Computed in Python from the dimensions, so they are consistent and
   tunable without re-scoring.

Guardrails that keep it honest: every claim must carry a **verbatim quote from the posting**,
verified by substring match (a quote that is not there gets flagged, not believed);
`missing_info` is required output so "pays too little" and "didn't say what it pays" stay
distinct; and `jobhunter eval` measures recall against your own triage decisions, so prompt
tuning is a measurement rather than a feeling.

The scorer is behind an interface — Haiku, Opus, or a third-party/local model via an
OpenAI-compatible endpoint — and `eval --compare` ranks them on *your* labeled jobs.

## Do we need browser controls?

**Yes, but as a last resort, not the default.** Three distinct things:

1. **Playwright (headless Chromium), in-process** — the production path for `tier: browser`
   sources only. `playwright install chromium` is the one heavyweight dependency.
2. **Playwright with request interception, during adapter authoring** — open a stubborn board
   once, watch the XHR traffic, find the JSON endpoint behind it, then write an `http`/`api`-tier
   adapter and *stop using the browser for that source*. This is the intended workflow, and it is
   exactly what the JobLink `/ada/r/` lead calls for.
3. **Claude-in-Chrome (interactive)** — for me to explore a board with you in your real browser
   session when an adapter misbehaves. Investigation, not the nightly run.

Manual forever: anything behind a login, MFA, or CAPTCHA ([008](008-compliance.md)).

## Open questions for you

1. **Your profile.** Fill in the `NEED FROM YOU` fields in `profile/preferences.yaml`: salary
   floor, remote vs commute, `done_with`, and `want`/`avoid`. This blocks M4.
2. **Scope of Class A.** Keep private-sector postings from the job banks, or public sector only?
3. **Puerto Rico.** It's missing from the CareerOneStop list. Add it manually?
4. **Target states.** You're applying out of state. Any states to rank first or rule out?

## Next step

Approve or amend the design, then I'll break [009](009-roadmap.md) into `git bug` issues
(`git-bug` v0.10.1 is installed) and start on M0. The roadmap is now two passes: open sources
first, email second.

Nothing is committed yet — the repo has no commits at all. Say the word and I'll make the
initial commit with these specs.
