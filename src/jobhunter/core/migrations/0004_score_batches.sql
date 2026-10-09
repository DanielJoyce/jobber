-- 0004_score_batches: Stage 2 batch tracking and screen columns (specs/006 "Stage 2").

-- One row per Message Batch submitted. collected_at stays NULL until results are read.
CREATE TABLE score_batch (
  id              TEXT PRIMARY KEY,          -- the provider's batch id
  tier            TEXT NOT NULL CHECK (tier IN ('screen', 'deep')),
  model           TEXT NOT NULL,             -- scorer name, e.g. anthropic:claude-haiku-4-5
  prompt_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL,
  request_count   INTEGER NOT NULL,
  submitted_at    TEXT NOT NULL,
  collected_at    TEXT,
  counts          TEXT                       -- JSON from collect: succeeded/errored/...
);

-- Which job group each custom_id in a batch scores. Groups in an uncollected batch are not
-- resubmitted; groups whose result errored or expired become eligible again on collect.
CREATE TABLE score_batch_item (
  batch_id     TEXT NOT NULL REFERENCES score_batch(id),
  custom_id    TEXT NOT NULL,
  job_group_id INTEGER NOT NULL REFERENCES job_group(id),
  result       TEXT,                         -- succeeded|errored|canceled|expired|invalid
  PRIMARY KEY (batch_id, custom_id)
);
CREATE INDEX score_batch_item_group ON score_batch_item(job_group_id);

-- Screen output with no column in 0001.
ALTER TABLE fit_score ADD COLUMN shape_flags TEXT;            -- JSON array
ALTER TABLE fit_score ADD COLUMN evidence_unverified INTEGER NOT NULL DEFAULT 0
  CHECK (evidence_unverified IN (0, 1));
