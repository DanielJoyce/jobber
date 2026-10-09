-- 0009_mail_alerts: email alert ingest state and re-scoring pasted descriptions (specs/012).

-- Gmail historyId watermark per label. One row per label name.
CREATE TABLE mail_sync_state (
  label       TEXT PRIMARY KEY,
  label_id    TEXT,
  history_id  TEXT,
  updated_at  TEXT NOT NULL
);

-- Every alert email processed, so a message is parsed once even if history replays it.
CREATE TABLE mail_message (
  message_id        TEXT PRIMARY KEY,
  history_id        TEXT,
  family            TEXT NOT NULL,
  origin_source_key TEXT,          -- registry row the alert came from, when identifiable
  sender_domain     TEXT,
  subject           TEXT,
  received_at       TEXT,
  entries           INTEGER NOT NULL DEFAULT 0,
  processed_at      TEXT NOT NULL
);

-- One row per job entry in an alert: either a new partial job or a sighting of an existing
-- full record (dedupe-first, specs/012 "The partial-description problem").
CREATE TABLE mail_entry (
  id                INTEGER PRIMARY KEY,
  message_id        TEXT NOT NULL REFERENCES mail_message(message_id),
  idx               INTEGER NOT NULL,
  outcome           TEXT NOT NULL CHECK (outcome IN ('new', 'sighting')),
  external_id       TEXT NOT NULL,  -- job.external_id under us-mailalerts for 'new'
  matched_job_id    INTEGER REFERENCES job(id),  -- the full record a sighting attached to
  match_method      TEXT,           -- apply_url | title_employer_state
  title             TEXT,
  url               TEXT,
  seen_at           TEXT NOT NULL,
  UNIQUE (message_id, idx)
);
CREATE INDEX mail_entry_matched ON mail_entry(matched_job_id);

-- Pasted descriptions bump the group's revision; a screen score is per revision, so the group
-- becomes eligible again without deleting the earlier (partial) score.
ALTER TABLE job_group ADD COLUMN description_rev INTEGER NOT NULL DEFAULT 0;
ALTER TABLE score_batch_item ADD COLUMN input_rev INTEGER NOT NULL DEFAULT 0;

-- fit_score's UNIQUE key gains input_rev. SQLite cannot alter a table constraint, so rebuild.
-- Nothing references fit_score(id), so foreign keys can stay on.
CREATE TABLE fit_score_new (
  id              INTEGER PRIMARY KEY,
  job_group_id    INTEGER NOT NULL REFERENCES job_group(id),
  tier            TEXT NOT NULL CHECK (tier IN ('screen', 'deep')),
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL,
  input_rev       INTEGER NOT NULL DEFAULT 0,

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
  shape_flags       TEXT,
  evidence_unverified INTEGER NOT NULL DEFAULT 0 CHECK (evidence_unverified IN (0, 1)),
  deep_report       TEXT,
  served_model      TEXT,
  UNIQUE (job_group_id, tier, prompt_version, scoring_version, model, input_rev)
);

INSERT INTO fit_score_new (
  id, job_group_id, tier, model, prompt_version, scoring_version, input_rev, verdict, overall,
  dimensions, evidence, blockers, missing_info, tailoring_hints, input_tokens, output_tokens,
  cache_read_tokens, cost_usd, batch_id, created_at, shape_flags, evidence_unverified,
  deep_report, served_model
)
SELECT
  id, job_group_id, tier, model, prompt_version, scoring_version, 0, verdict, overall,
  dimensions, evidence, blockers, missing_info, tailoring_hints, input_tokens, output_tokens,
  cache_read_tokens, cost_usd, batch_id, created_at, shape_flags, evidence_unverified,
  deep_report, served_model
FROM fit_score;

DROP TABLE fit_score;
ALTER TABLE fit_score_new RENAME TO fit_score;
