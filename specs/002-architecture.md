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
data/cache/ab/cd/abcdef…sha256.gz
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
├── profile/                      ← YOUR data. gitignored.
│   ├── resume.md
│   └── preferences.yaml
├── data/                         ← gitignored
│   ├── jobhunter.db
│   └── cache/
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

## Configuration

Three layers, most specific wins:

1. `src/jobhunter/config.py` — defaults in code.
2. `~/.config/jobhunter/config.toml` — machine settings: paths, rate limits, model choice,
   spend cap, `respect_robots`, contact email for the User-Agent.
3. `profile/preferences.yaml` — what you want in a job. Versioned by content hash so scores
   record which profile produced them.

Secrets: `ANTHROPIC_API_KEY` from the environment, or an `ant auth login` profile — the SDK
resolves either with a bare `Anthropic()` client. No keys in config files.
