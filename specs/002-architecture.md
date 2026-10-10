# 002 — Architecture & stack

## Stack decision

**Python 3.13**, managed by `uv`.

Why Python over TypeScript or Go: the work is 70% HTML/JSON wrangling, 20% LLM calls, 10% UI.
Python wins the first category decisively (`lxml`, `selectolax`, `rapidfuzz`, `dateparser`),
ties the second, and loses the third only if we wanted a rich SPA — which we do not.
One toolchain, one language, no build step.

**Pin 3.13, not 3.14.** `python3` on this machine is 3.14.7. Scraping and parsing dependencies
lean heavily on compiled wheels (`lxml`, `selectolax`, `playwright`) and new CPython minors
routinely lag on those. `uv python pin 3.13` removes a whole class of install friction; revisit
later.

### Dependencies

| Need | Choice | Notes |
|---|---|---|
| Packaging / venv / Python | `uv` | Already installed (0.12.5) |
| HTTP | `httpx` | HTTP/2, timeouts, connection pooling, sync + async |
| HTML parsing | `selectolax` (fast path), `lxml` (XPath where needed) | `selectolax` is dramatically faster on large pages; GA's board is a 728 KB document |
| Browser | `playwright` | `tier: browser` sources only. Needs `playwright install chromium` |
| Schema / validation | `pydantic` v2 | Normalized job model, LLM structured-output schemas, config files |
| DB | `sqlite3` (stdlib) + `sqlite-utils` for migrations | No ORM. The queries here are simple and hand-written SQL is clearer |
| Full-text search | SQLite **FTS5** | Built in; searches descriptions |
| Fuzzy matching | `rapidfuzz` | Near-duplicate job detection |
| Date parsing | `dateparser` | Boards write dates 20 different ways |
| LLM | `anthropic` | Official SDK |
| CLI | `typer` | Every stage is a subcommand |
| Web console | `fastapi` + `jinja2` + `htmx` (vendored JS) | Server-rendered, no build step |
| Tests | `pytest` + `pytest-recording` or cached fixtures | Adapter tests run against **frozen HTML fixtures**, never the network |

Deliberately **not** used: Scrapy (its architecture fights per-source rate policies and a
resolve stage), Airflow/Prefect/Celery (one user, one machine), SQLAlchemy (no need),
React/Next (no need), Selenium (Playwright is strictly better).

## Process model

Three processes, all optional, all independent:

```
  jobhunter run        one-shot CLI pipeline     ← systemd timer, nightly
  jobhunter console    FastAPI on 127.0.0.1:8808 ← you, interactively
  jobhunter score      LLM batch submit/collect  ← called by run, or standalone
```

No daemon, no queue, no broker. The CLI is the only writer during a run; the console writes
only triage and application state. SQLite in WAL mode handles that concurrency fine.

Scheduling is a `systemd` **user** timer (`~/.config/systemd/user/jobhunter.timer`), not cron:
it gives logs via `journalctl`, `OnFailure=` hooks, and `Persistent=true` so a missed run
catches up after the laptop wakes.

## The pipeline is stages over a database, not a dataflow graph

Every stage reads rows in one state and writes rows in the next. Each is **idempotent** and
**independently resumable**. A crash in `resolve` costs you nothing already fetched, and
re-running `normalize` after a parser fix does not touch the network.

```
discover → list → resolve → normalize → dedupe → prefilter → score → triage
```

Details in [004](004-ingest-pipeline.md).

## The raw cache is load-bearing

Every HTTP response — listing page, detail page, JSON payload, CSV export — is stored
content-addressed and gzipped:

```
~/.cache/jobhunter/ab/cd/abcdef…sha256.gz
```

with a `fetch_log` row recording URL, source, timestamp, status, content hash, and the
`etag`/`last-modified` headers. Parsers read from the cache, never from the network.

This buys four things, and they are the difference between this project being tractable and
being miserable:

1. **Adapter development is offline.** Fetch a board's pages once, then iterate on the parser
   against local bytes with zero latency and zero load on a government web server.
2. **Parser bugs are retroactively fixable.** Fix the salary regex, re-run `normalize` over
   three months of cached HTML, and every historical row is corrected.
3. **Breakage is diagnosable.** When a source starts returning nothing, diff today's cached
   HTML against last week's and the layout change is right there.
4. **Conditional requests are free.** Stored `etag`/`last-modified` let the fetcher send
   `If-None-Match` and take a `304` — politeness and speed at once.

Cache is never auto-evicted. Text compresses ~8x; a year of 50 states is single-digit GB.

## Repository layout

```
jobhunter/
├── pyproject.toml
├── specs/                        ← these documents
├── src/jobhunter/
│   ├── cli.py                    Typer entrypoint
│   ├── config.py                 settings, paths, policy flags
│   ├── core/
│   │   ├── models.py             pydantic: JobStub, JobDetail, Job, FitScore
│   │   ├── db.py                 connection, migrations, FTS triggers
│   │   ├── fetch.py              FetchContext: cache + rate limit + robots + retry
│   │   └── textnorm.py           whitespace, html→text, salary & location parsing
│   ├── sources/
│   │   ├── registry.yaml         ← one row per state. The project's spine.
│   │   ├── registry.py           load + validate registry
│   │   └── adapters/
│   │       ├── base.py           SourceAdapter protocol
│   │       ├── neogov.py         NEOGOV / governmentjobs.com family
│   │       ├── workday.py        Workday CxS JSON API family
│   │       ├── taleo.py          Oracle Taleo family
│   │       ├── jobaps.py         JobAps family
│   │       ├── htmlconfig.py     config-driven CSS/XPath adapter (no code per source)
│   │       ├── browser.py        Playwright base for SPA sources
│   │       ├── csvdrop.py        manual CSV export import + resolve
│   │       └── mailalerts.py     Gmail job-alert ingest (optional)
│   ├── pipeline/                 one module per stage
│   ├── scoring/
│   │   ├── profile.py            load/validate profile, version hash
│   │   ├── prefilter.py          deterministic hard constraints
│   │   ├── rubric.py             versioned prompt + output schema
│   │   ├── screen.py             Haiku 4.5 via Batch API
│   │   ├── deep.py               Opus 5 shortlist pass
│   │   └── calibration.py        eval against hand labels
│   └── console/
│       ├── app.py
│       └── templates/
└── tests/
    └── fixtures/<source>/        frozen HTML/JSON per adapter
```

### Where files live

Your files are **not** in the checkout. Defaults follow the XDG base directory spec
(`src/jobhunter/xdg.py`); `jobhunter paths` prints the resolved value, its source and whether
it exists.

```
$XDG_CONFIG_HOME/jobhunter/        (~/.config/jobhunter)
├── config.toml                    machine settings
├── google_client_secret.json      Gmail OAuth client (optional)
└── gmail_token.json               fallback token store when no keyring
$XDG_DATA_HOME/jobhunter/          (~/.local/share/jobhunter)   user data, mode 0700
├── jobhunter.db
├── profile/preferences.yaml       edited by the console; dated backups beside it
├── resume/                        one .md; older versions in .previous/
└── backups/                       timestamped sqlite backups (migrate-paths writes the first)
$XDG_CACHE_HOME/jobhunter/         (~/.cache/jobhunter)
├── ab/cd/<sha256>.gz              the raw cache (still never auto-evicted by jobhunter)
└── openrouter_models.json
```

Decisions: `profile/preferences.yaml` stays with the resume under the data directory rather than
in the config directory. It is rewritten by the console (with backups and content-hash
snapshots) and its relative `resume_path` resolves against the profile directory's parent, so
keeping `profile/` and `resume/` side by side preserves both. The raw cache moved to the cache
directory as asked; it is still permanent as far as jobhunter is concerned, but a cache-cleaning
tool may remove `~/.cache`, which only costs re-fetching.

Precedence for each `[paths]` key: caller overrides, then `JOBHUNTER_DATA_DIR`,
`JOBHUNTER_CACHE_DIR`, `JOBHUNTER_DB_PATH`, `JOBHUNTER_PROFILE_DIR`, `JOBHUNTER_RESUME_PATH`,
then `config.toml`, then the XDG default. `db_path`, `profile_dir` and `resume_path` default to
names inside `data_dir`, so setting `data_dir` moves all three. Relative explicit paths still
resolve against the working directory.

**Compatibility with the old layout.** The previous defaults were `./data`, `./profile` and
`./resume`, relative to the working directory. For each of the database, profile, resume and
cache that has no explicit path: if the old location exists and the new one does not, the old
one is used and a single warning says to run `jobhunter migrate-paths`. The old location is
looked for in the working directory, then in the source checkout the package runs from, so a
command run from another directory still finds the real database instead of starting an empty
one. (An installed copy with no checkout and no old data in the cwd has nothing to fall back to.) `migrate-paths` is a dry run by default; `--apply` takes a timestamped
sqlite backup of the database, copies (never moves) the database, profile, resume and cache,
and leaves a `MOVED.txt` in each old directory. It refuses when a destination exists and
differs, unless the original is unchanged since it was copied (each `MOVED.txt` records a
digest), which means the destination is simply in use. `--apply --remove-old` deletes only
originals whose copy is identical or, by that digest, in use since the migration. Old
`backups/` are copied before the database backup is written into the new `backups/`. The tests'
isolation guard (`tests/conftest.py`) blocks the real XDG locations as well as the repo-relative ones.

## Configuration

Three layers, most specific wins:

1. `src/jobhunter/config.py` — defaults in code.
2. `$XDG_CONFIG_HOME/jobhunter/config.toml` (`~/.config/jobhunter/config.toml`) — machine settings: paths, rate limits, model choice,
   spend cap, `respect_robots`, contact email for the User-Agent.
3. `<data dir>/profile/preferences.yaml` — what you want in a job. Versioned by content hash so scores
   record which profile produced them.

Secrets: `ANTHROPIC_API_KEY` from the environment, or an `ant auth login` profile — the SDK
resolves either with a bare `Anthropic()` client. No keys in config files.
