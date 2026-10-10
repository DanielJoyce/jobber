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
catches up after the laptop wakes. The units keep `WorkingDirectory=` at the checkout so
`./.env` is read, and `jobhunter schedule install` writes `Environment=XDG_CONFIG_HOME=`,
`XDG_DATA_HOME=` and `XDG_CACHE_HOME=` with the values the installing shell resolved, so the
timers use the same config, database and cache as `jobhunter paths` shows even when the systemd
user manager does not see `XDG_*` from a login shell. Reinstall after changing them.
`JOBHUNTER_*` path variables are not copied; set those in `config.toml` or `.env`.

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
└── backups/                       sqlite backups (migrate-paths brings the old data/backups)
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

**The old layout: detect and stop, never guess.** The previous defaults were `./data`,
`./profile` and `./resume`, relative to the working directory. Path resolution has no fallback
to them: an explicit setting, else the XDG default. Instead `src/jobhunter/legacy_data.py` looks
for an old database, narrowly: only `data/jobhunter.db` counts (a `resume/` or `profile/` alone
is not a jobhunter checkout), and only in two places, the main checkout of the repository the
package runs from (`git rev-parse --git-common-dir`, so every worktree maps to the same main
checkout) and the working directory. Every command except `paths`, `migrate-paths`, `init` and
`schedule` checks first (the Typer root callback):

| Old `data/jobhunter.db` | Configured database | Result |
|---|---|---|
| none | missing | runs; the database is created (a fresh install; `jobhunter init` does it explicitly) |
| exists, no `MOVED.txt` in `data/` | missing | stops: "your data is still in <path>; run jobhunter migrate-paths". Nothing is created. |
| exists, no `MOVED.txt` | exists, default location | stops: two databases, jobhunter will not pick one |
| exists, no `MOVED.txt` | exists, set explicitly | runs, with a note (a one-off run against a copy) |
| exists, `MOVED.txt` beside it | exists | runs, with a note that the old one is stale |
| is the configured one (`data_dir = "data"`) | | runs: the user chose it |

`jobhunter init` creates the data directory (0700) and the database (0600) and refuses while old
data exists.

**`migrate-paths`.** A dry run by default; `--from DIR` names the old folder, otherwise the one
found above (two different old databases need `--from`). `--apply`:

1. refuses while any other process has the old database open: a scan of `/proc/*/fd` names the
   process, and an exclusive SQLite lock (`locking_mode=EXCLUSIVE` plus `BEGIN EXCLUSIVE`, which
   in WAL mode fails while any other connection is open) is taken and held to the end, so
   nothing can open the database meanwhile. It says to stop `jobhunter console` and
   `systemctl --user stop` the timers;
2. copies the database with SQLite's online backup API into a temp file beside the destination,
   and `data/backups`, `data/cache`, `profile/` and `resume/` into staging names;
3. verifies: `integrity_check`, the same tables, schema and row counts for the database, the same
   sha256 for every file of every tree; then moves the copies into place;
4. makes the data directory 0700 and the database, backups, profile and resume 0600 (existing
   ones too);
5. renames the old folders in their parent to `data.migrated-YYYYMMDD`,
   `profile.migrated-YYYYMMDD`, `resume.migrated-YYYYMMDD` (with `-2` and so on if taken) and
   writes a `MOVED.txt` inside each. A rename is atomic and reversible, and no process can keep
   using the old path. There is no `--remove-old`: the user deletes the `.migrated` folders by
   hand; the command prints the `rm -rf` line. The archives still hold personal data, so
   `.gitignore` and the forbid-personal-paths hook cover `<name>.migrated-*/` as well.

A failure before step 5 removes this run's copies and leaves the old layout untouched. A
destination that exists and differs (a database that exists at all) is a conflict: nothing
happens until the user moves one aside. A rerun after success prints "nothing to migrate" and
the archive folders still present. The tests' isolation guard (`tests/conftest.py`) blocks the
real XDG locations as well as the repo-relative ones, and points the main-checkout detector away
from the developer's checkout.

## Configuration

Three layers, most specific wins:

1. `src/jobhunter/config.py` — defaults in code.
2. `$XDG_CONFIG_HOME/jobhunter/config.toml` (`~/.config/jobhunter/config.toml`) — machine settings: paths, rate limits, model choice,
   spend cap, `respect_robots`, contact email for the User-Agent.
3. `<data dir>/profile/preferences.yaml` — what you want in a job. Versioned by content hash so scores
   record which profile produced them.

Secrets: `ANTHROPIC_API_KEY` from the environment, or an `ant auth login` profile — the SDK
resolves either with a bare `Anthropic()` client. No keys in config files.
