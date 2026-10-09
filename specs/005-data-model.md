# 005 — Data model

SQLite, WAL mode, FTS5 for search. One file: `data/jobhunter.db`. Migrations are numbered SQL
files applied in order, tracked in `schema_version`.

Design rules that the schema enforces:

- **Raw is never discarded.** Every normalized field keeps its `*_raw` source so a parser fix
  can be applied retroactively over cached bytes.
- **Nothing is overwritten by a re-run.** `first_seen_at` is immutable; only volatile fields
  update on conflict.
- **Location is many-to-many** (see [011](011-federal-and-geography.md)) — there is no `state`
  column on `job`.
- **Scoring is keyed by (job_group, scoring_version, prompt_version, model)** so scores are
  comparable and re-scoring is targeted, never blind.
- **History is append-only** where it matters: application status changes are events, not an
  UPDATE to a status column.

## Sources & runs

```sql
CREATE TABLE source (                      -- mirrors registry.yaml; registry is the source of truth
  key            TEXT PRIMARY KEY,
  state          TEXT,                     -- NULL for federal
  class          TEXT NOT NULL,            -- 'A' job bank | 'B' state employer | 'C' federal
  name           TEXT NOT NULL,
  family         TEXT NOT NULL,            -- vos | joblink | nlx | usajobs | htmlconfig | ...
  tier           TEXT NOT NULL,            -- api | feed | http | browser | manual
  entry          TEXT NOT NULL,
  policy         TEXT NOT NULL,            -- enabled | blocked | manual | disabled
  robots_status  TEXT, robots_checked  TEXT, robots_hash TEXT,
  verified_at    TEXT,
  status         TEXT NOT NULL DEFAULT 'ok',   -- ok|suspect|broken|blocked|manual|disabled
  status_note    TEXT
);

CREATE TABLE source_state (                -- watermarks, one row per source
  source_key         TEXT PRIMARY KEY REFERENCES source(key),
  last_run_at        TEXT,
  last_ok_at         TEXT,
  max_posted_at_seen TEXT,
  jobs_last_7d       INTEGER NOT NULL DEFAULT 0,
  resolve_backlog    INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE run (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL, ended_at TEXT,
  stages      TEXT NOT NULL,              -- JSON array of stages executed
  exit_status TEXT,
  counts      TEXT                        -- JSON: {listed, resolved, scored, errors}
);

CREATE TABLE run_source (                 -- per-source outcome within a run
  run_id      INTEGER NOT NULL REFERENCES run(id),
  source_key  TEXT NOT NULL REFERENCES source(key),
  queries_run INTEGER, stubs_found INTEGER, resolved INTEGER,
  status      TEXT NOT NULL, error TEXT,
  duration_ms INTEGER,
  PRIMARY KEY (run_id, source_key)
);
```

## Fetch cache

```sql
CREATE TABLE fetch_log (
  id            INTEGER PRIMARY KEY,
  source_key    TEXT REFERENCES source(key),
  url           TEXT NOT NULL,
  method        TEXT NOT NULL DEFAULT 'GET',
  request_hash  TEXT NOT NULL,            -- sha256(method|url|body) — the cache key
  content_hash  TEXT,                     -- sha256(body) — the cache FILENAME
  http_status   INTEGER,
  etag          TEXT, last_modified TEXT, -- enables If-None-Match / If-Modified-Since
  fetched_at    TEXT NOT NULL,
  bytes         INTEGER,
  from_cache    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX fetch_log_req ON fetch_log(request_hash, fetched_at DESC);
```

Bodies live at `data/cache/<ch[0:2]>/<ch[2:4]>/<content_hash>.gz`. Two requests returning
identical bytes share one file. Never auto-evicted — see
[002](002-architecture.md#the-raw-cache-is-load-bearing).

## Jobs

```sql
CREATE TABLE job (
  id              INTEGER PRIMARY KEY,
  source_key      TEXT NOT NULL REFERENCES source(key),
  external_id     TEXT NOT NULL,
  job_group_id    INTEGER REFERENCES job_group(id),

  url             TEXT NOT NULL,
  title           TEXT NOT NULL,
  employer        TEXT,                    -- the hiring org (Class A boards carry private employers)
  agency_raw      TEXT,

  description_raw  TEXT,                   -- HTML as fetched
  description_text TEXT,                   -- normalized plaintext
  description_completeness TEXT NOT NULL DEFAULT 'full',  -- full | partial | pasted (see 012)
  content_hash     TEXT,                   -- sha256 over normalized title+employer+locations+description

  posted_at           TEXT,
  posted_at_estimated INTEGER NOT NULL DEFAULT 0,   -- true = first-seen used as a proxy
  closes_at           TEXT,

  salary_raw     TEXT,
  salary_min     REAL, salary_max REAL,
  salary_period  TEXT,                     -- hour|day|week|month|year
  salary_stated  INTEGER NOT NULL DEFAULT 0,

  location_raw   TEXT,
  location_scope TEXT NOT NULL DEFAULT 'unknown',  -- see 011
  remote         TEXT NOT NULL DEFAULT 'unknown',  -- onsite|hybrid|remote|unknown
  employment_type TEXT NOT NULL DEFAULT 'unknown',

  -- federal-only structured signals (NULL elsewhere)
  occupation_code TEXT,                    -- occupational series, e.g. '2210'
  pay_plan        TEXT,                    -- 'GS', 'ES', ...
  grade_low       TEXT, grade_high TEXT,

  stage          TEXT NOT NULL DEFAULT 'listed',
                 -- listed|resolved|normalized|grouped|prefiltered|scored|triaged
  needs_resolve  INTEGER NOT NULL DEFAULT 1,
  resolve_attempts INTEGER NOT NULL DEFAULT 0,
  parse_warnings TEXT,                     -- JSON array; never silently swallow a parse failure

  first_seen_at  TEXT NOT NULL,            -- immutable
  last_seen_at   TEXT NOT NULL,
  UNIQUE (source_key, external_id)
);
CREATE INDEX job_stage    ON job(stage, needs_resolve);
CREATE INDEX job_posted   ON job(posted_at DESC);
CREATE INDEX job_group_ix ON job(job_group_id);
CREATE INDEX job_chash    ON job(content_hash);
```

`job_locations` is defined in [011](011-federal-and-geography.md#the-fix-jobs-and-locations-are-many-to-many).

```sql
CREATE TABLE job_group (                   -- the dedupe unit; scoring operates on this
  id            INTEGER PRIMARY KEY,
  canonical_job_id INTEGER REFERENCES job(id),
  member_count  INTEGER NOT NULL DEFAULT 1,
  method        TEXT NOT NULL,             -- exact_hash | near_dupe | manual
  confidence    REAL,
  created_at    TEXT NOT NULL
);
```

Grouping is advisory and non-destructive: every original `job` row survives, and a group can be
split from the console when the heuristic is wrong.

### Full-text search

```sql
CREATE VIRTUAL TABLE job_fts USING fts5(
  title, employer, description_text,
  content='job', content_rowid='id', tokenize='porter unicode61'
);
```
Kept current by `AFTER INSERT/UPDATE/DELETE` triggers on `job`.

## Scoring

```sql
CREATE TABLE prefilter_result (
  job_id      INTEGER PRIMARY KEY REFERENCES job(id),
  passed      INTEGER NOT NULL,
  reasons     TEXT NOT NULL,              -- JSON array of rule keys that rejected it
  filter_version TEXT NOT NULL,           -- recomputed for free when filter settings change
  evaluated_at TEXT NOT NULL
);

CREATE TABLE fit_score (
  id              INTEGER PRIMARY KEY,
  job_group_id    INTEGER NOT NULL REFERENCES job_group(id),
  tier            TEXT NOT NULL,          -- 'screen' (Haiku) | 'deep' (Opus)
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL,          -- model-input half of the profile (see 014)

  verdict         TEXT NOT NULL,          -- strong|possible|weak|mismatch
  overall         INTEGER NOT NULL,       -- 0-100
  dimensions      TEXT NOT NULL,          -- JSON: skills/seniority/domain (model only).
                                          -- comp/location/overall/bucket are computed on read
  evidence        TEXT NOT NULL,          -- JSON [{claim, quote}] — quotes must appear in the posting
  blockers        TEXT,                   -- JSON array
  missing_info    TEXT,                   -- JSON array
  tailoring_hints TEXT,                   -- JSON array

  input_tokens    INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cost_usd REAL,
  batch_id        TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE (job_group_id, tier, prompt_version, scoring_version, model)
);
```

That `UNIQUE` constraint is the cost-control mechanism: a job is never re-scored under the same
conditions, and changing your profile or the rubric re-scores deliberately rather than by
accident.

```sql
CREATE TABLE label (                       -- your triage decisions = the calibration set
  job_group_id INTEGER PRIMARY KEY REFERENCES job_group(id),
  label        TEXT NOT NULL,              -- interesting | not_interesting | applied
  note         TEXT,
  labeled_at   TEXT NOT NULL
);

CREATE TABLE llm_spend (                   -- daily rollup; drives the console cost page and cap
  day   TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL,
  calls INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL NOT NULL,
  PRIMARY KEY (day, model, tier)
);
```

## Applications

```sql
CREATE TABLE application (
  id             INTEGER PRIMARY KEY,
  job_group_id   INTEGER NOT NULL UNIQUE REFERENCES job_group(id),
  status         TEXT NOT NULL,            -- derived cache of latest event; events are the truth
  applied_at     TEXT,
  resume_version TEXT,                     -- which resume you actually sent
  cover_letter_path TEXT,
  external_ref   TEXT,                     -- the board's confirmation/application number
  next_action    TEXT,
  next_action_at TEXT,
  created_at     TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE application_event (           -- append-only history
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  at             TEXT NOT NULL,
  status         TEXT NOT NULL,
                 -- interested|preparing|applied|acknowledged|screening
                 -- |interview|offer|rejected|withdrawn|no_response|closed
  note           TEXT,
  source         TEXT NOT NULL DEFAULT 'manual'   -- manual | email | import
);

CREATE TABLE contact (
  id INTEGER PRIMARY KEY,
  application_id INTEGER REFERENCES application(id),
  name TEXT, role TEXT, email TEXT, phone TEXT, note TEXT
);

CREATE TABLE attachment (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  kind TEXT NOT NULL,                      -- resume|cover_letter|transcript|assessment|other
  path TEXT NOT NULL, added_at TEXT NOT NULL
);
```

`status` on `application` is a denormalized cache of the latest `application_event`, rebuildable
at any time. The event log is what you actually look at six weeks later when you cannot remember
whether anyone ever replied.

## Defined elsewhere

- `job_locations`: [011](011-federal-and-geography.md#the-fix-jobs-and-locations-are-many-to-many)
- `profile_change`: [014](014-preferences-console.md#history-and-undo)
- `apply_link`, `apply_click`: [015](015-apply-links.md#data-model)
- `job.description_completeness`: [012](012-email-ingest.md#the-partial-description-problem)
