-- 0001_initial: full schema from specs 005, 011, 012, 014, 015.

CREATE TABLE source (
  key            TEXT PRIMARY KEY,
  state          TEXT,
  class          TEXT NOT NULL CHECK (class IN ('A', 'B', 'C')),
  name           TEXT NOT NULL,
  family         TEXT NOT NULL,
  tier           TEXT NOT NULL CHECK (tier IN ('api', 'feed', 'http', 'browser', 'manual')),
  entry          TEXT NOT NULL,
  policy         TEXT NOT NULL CHECK (policy IN ('enabled', 'blocked', 'manual', 'disabled')),
  robots_status  TEXT,
  robots_checked TEXT,
  robots_hash    TEXT,
  verified_at    TEXT,
  status         TEXT NOT NULL DEFAULT 'ok'
                 CHECK (status IN ('ok', 'suspect', 'broken', 'blocked', 'manual', 'disabled')),
  status_note    TEXT
);

CREATE TABLE source_state (
  source_key           TEXT PRIMARY KEY REFERENCES source(key),
  last_run_at          TEXT,
  last_ok_at           TEXT,
  max_posted_at_seen   TEXT,
  jobs_last_7d         INTEGER NOT NULL DEFAULT 0,
  resolve_backlog      INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE run (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  stages      TEXT NOT NULL,
  exit_status TEXT,
  counts      TEXT
);

CREATE TABLE run_source (
  run_id      INTEGER NOT NULL REFERENCES run(id),
  source_key  TEXT NOT NULL REFERENCES source(key),
  queries_run INTEGER,
  stubs_found INTEGER,
  resolved    INTEGER,
  status      TEXT NOT NULL,
  error       TEXT,
  duration_ms INTEGER,
  PRIMARY KEY (run_id, source_key)
);

CREATE TABLE fetch_log (
  id            INTEGER PRIMARY KEY,
  source_key    TEXT REFERENCES source(key),
  url           TEXT NOT NULL,
  method        TEXT NOT NULL DEFAULT 'GET',
  request_hash  TEXT NOT NULL,
  content_hash  TEXT,
  http_status   INTEGER,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TEXT NOT NULL,
  bytes         INTEGER,
  from_cache    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX fetch_log_req ON fetch_log(request_hash, fetched_at DESC);

-- job_group.canonical_job_id and job.job_group_id reference each other (circular FKs);
-- SQLite resolves FK targets lazily, so job_group is created first.
CREATE TABLE job_group (
  id               INTEGER PRIMARY KEY,
  canonical_job_id INTEGER REFERENCES job(id),
  member_count     INTEGER NOT NULL DEFAULT 1,
  method           TEXT NOT NULL CHECK (method IN ('exact_hash', 'near_dupe', 'manual')),
  confidence       REAL,
  created_at       TEXT NOT NULL
);

CREATE TABLE job (
  id              INTEGER PRIMARY KEY,
  source_key      TEXT NOT NULL REFERENCES source(key),
  external_id     TEXT NOT NULL,
  job_group_id    INTEGER REFERENCES job_group(id),

  url             TEXT NOT NULL,
  title           TEXT NOT NULL,
  employer        TEXT,
  agency_raw      TEXT,

  description_raw  TEXT,
  description_text TEXT,
  description_completeness TEXT NOT NULL DEFAULT 'full'
                   CHECK (description_completeness IN ('full', 'partial', 'pasted')),
  content_hash     TEXT,

  posted_at           TEXT,
  posted_at_estimated INTEGER NOT NULL DEFAULT 0,
  closes_at           TEXT,

  salary_raw     TEXT,
  salary_min     REAL,
  salary_max     REAL,
  salary_period  TEXT CHECK (salary_period IN ('hour', 'day', 'week', 'month', 'year')),
  salary_stated  INTEGER NOT NULL DEFAULT 0,

  location_raw   TEXT,
  location_scope TEXT NOT NULL DEFAULT 'unknown'
                 CHECK (location_scope IN ('single', 'multi_state', 'nationwide', 'remote_us',
                                           'negotiable', 'overseas', 'unknown')),
  remote         TEXT NOT NULL DEFAULT 'unknown'
                 CHECK (remote IN ('onsite', 'hybrid', 'remote', 'unknown')),
  employment_type TEXT NOT NULL DEFAULT 'unknown',

  occupation_code TEXT,
  pay_plan        TEXT,
  grade_low       TEXT,
  grade_high      TEXT,

  stage          TEXT NOT NULL DEFAULT 'listed'
                 CHECK (stage IN ('listed', 'resolved', 'normalized', 'grouped', 'prefiltered',
                                  'scored', 'triaged')),
  needs_resolve  INTEGER NOT NULL DEFAULT 1,
  resolve_attempts INTEGER NOT NULL DEFAULT 0,
  parse_warnings TEXT,

  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  UNIQUE (source_key, external_id)
);
CREATE INDEX job_stage    ON job(stage, needs_resolve);
CREATE INDEX job_posted   ON job(posted_at DESC);
CREATE INDEX job_group_ix ON job(job_group_id);
CREATE INDEX job_chash    ON job(content_hash);

CREATE TABLE job_locations (
  job_id     INTEGER NOT NULL REFERENCES job(id),
  state      TEXT,
  city       TEXT,
  county     TEXT,
  lat        REAL,
  lon        REAL,
  is_primary INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, state, city)
);
CREATE INDEX job_locations_state ON job_locations(state);

CREATE VIRTUAL TABLE job_fts USING fts5(
  title, employer, description_text,
  content='job', content_rowid='id', tokenize='porter unicode61'
);

CREATE TRIGGER job_fts_ai AFTER INSERT ON job BEGIN
  INSERT INTO job_fts(rowid, title, employer, description_text)
  VALUES (new.id, new.title, new.employer, new.description_text);
END;

CREATE TRIGGER job_fts_ad AFTER DELETE ON job BEGIN
  INSERT INTO job_fts(job_fts, rowid, title, employer, description_text)
  VALUES ('delete', old.id, old.title, old.employer, old.description_text);
END;

CREATE TRIGGER job_fts_au AFTER UPDATE ON job BEGIN
  INSERT INTO job_fts(job_fts, rowid, title, employer, description_text)
  VALUES ('delete', old.id, old.title, old.employer, old.description_text);
  INSERT INTO job_fts(rowid, title, employer, description_text)
  VALUES (new.id, new.title, new.employer, new.description_text);
END;

CREATE TABLE prefilter_result (
  job_id         INTEGER PRIMARY KEY REFERENCES job(id),
  passed         INTEGER NOT NULL CHECK (passed IN (0, 1)),
  reasons        TEXT NOT NULL,
  filter_version TEXT NOT NULL,
  evaluated_at   TEXT NOT NULL
);

CREATE TABLE fit_score (
  id              INTEGER PRIMARY KEY,
  job_group_id    INTEGER NOT NULL REFERENCES job_group(id),
  tier            TEXT NOT NULL CHECK (tier IN ('screen', 'deep')),
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL,

  verdict         TEXT NOT NULL CHECK (verdict IN ('strong', 'possible', 'weak', 'mismatch')),
  overall         INTEGER NOT NULL CHECK (overall BETWEEN 0 AND 100),
  dimensions      TEXT NOT NULL,
  evidence        TEXT NOT NULL,
  blockers        TEXT,
  missing_info    TEXT,
  tailoring_hints TEXT,

  input_tokens      INTEGER,
  output_tokens     INTEGER,
  cache_read_tokens INTEGER,
  cost_usd          REAL,
  batch_id          TEXT,
  created_at        TEXT NOT NULL,
  UNIQUE (job_group_id, tier, prompt_version, scoring_version, model)
);

CREATE TABLE label (
  job_group_id INTEGER PRIMARY KEY REFERENCES job_group(id),
  label        TEXT NOT NULL CHECK (label IN ('interesting', 'not_interesting', 'applied')),
  note         TEXT,
  labeled_at   TEXT NOT NULL
);

CREATE TABLE llm_spend (
  day           TEXT NOT NULL,
  model         TEXT NOT NULL,
  tier          TEXT NOT NULL,
  calls         INTEGER NOT NULL,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL NOT NULL,
  PRIMARY KEY (day, model, tier)
);

CREATE TABLE application (
  id                INTEGER PRIMARY KEY,
  job_group_id      INTEGER NOT NULL UNIQUE REFERENCES job_group(id),
  status            TEXT NOT NULL
                    CHECK (status IN ('interested', 'preparing', 'applied', 'acknowledged',
                                      'screening', 'interview', 'offer', 'rejected',
                                      'withdrawn', 'no_response', 'closed')),
  applied_at        TEXT,
  resume_version    TEXT,
  cover_letter_path TEXT,
  external_ref      TEXT,
  next_action       TEXT,
  next_action_at    TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE application_event (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  at             TEXT NOT NULL,
  status         TEXT NOT NULL
                 CHECK (status IN ('interested', 'preparing', 'applied', 'acknowledged',
                                   'screening', 'interview', 'offer', 'rejected',
                                   'withdrawn', 'no_response', 'closed')),
  note           TEXT,
  source         TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'email', 'import'))
);

CREATE TABLE contact (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER REFERENCES application(id),
  name           TEXT,
  role           TEXT,
  email          TEXT,
  phone          TEXT,
  note           TEXT
);

CREATE TABLE attachment (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  kind           TEXT NOT NULL
                 CHECK (kind IN ('resume', 'cover_letter', 'transcript', 'assessment', 'other')),
  path           TEXT NOT NULL,
  added_at       TEXT NOT NULL
);

CREATE TABLE profile_change (
  id              INTEGER PRIMARY KEY,
  at              TEXT NOT NULL,
  field_path      TEXT NOT NULL,
  old_value       TEXT,
  new_value       TEXT,
  filter_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL,
  source          TEXT NOT NULL CHECK (source IN ('ui', 'file', 'revert'))
);

CREATE TABLE apply_link (
  job_group_id  INTEGER PRIMARY KEY REFERENCES job_group(id),
  start_url     TEXT NOT NULL,
  final_url     TEXT,
  chain         TEXT NOT NULL,
  ats           TEXT,
  employer_host TEXT,
  status        TEXT NOT NULL CHECK (status IN ('live', 'expired', 'unresolved', 'blocked')),
  resolved_at   TEXT NOT NULL,
  verified_at   TEXT,
  error         TEXT
);

CREATE TABLE apply_click (
  id           INTEGER PRIMARY KEY,
  job_group_id INTEGER NOT NULL REFERENCES job_group(id),
  at           TEXT NOT NULL,
  final_url    TEXT NOT NULL,
  outcome      TEXT CHECK (outcome IN ('redirected', 'expired'))
);
