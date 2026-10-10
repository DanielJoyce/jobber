-- migrate: foreign_keys=off
-- 0024_rescore_run: a rescore_request can now be run (specs/014 "Re-score now").
-- SQLite cannot alter a CHECK, so the table is rebuilt with the wider status set and the
-- columns a run reports: scorer, progress, cost, error and a heartbeat for single-flight.
CREATE TABLE rescore_request_new (
  id               INTEGER PRIMARY KEY,
  requested_at     TEXT NOT NULL,              -- UTC ISO-8601; when it was asked for, not a schedule
  scope            TEXT NOT NULL CHECK (scope IN ('recent', 'all')),
  scoring_version  TEXT NOT NULL,
  job_count        INTEGER NOT NULL,
  cost_per_job_usd REAL NOT NULL,
  estimated_usd    REAL NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'done', 'failed', 'canceled')),
  done_at          TEXT,
  scorer           TEXT,                       -- spec used by the run (NULL until it starts)
  started_at       TEXT,
  heartbeat_at     TEXT,                       -- bumped on every progress write
  total            INTEGER NOT NULL DEFAULT 0, -- groups the run set out to score
  scored           INTEGER NOT NULL DEFAULT 0,
  errored          INTEGER NOT NULL DEFAULT 0,
  cost_usd         REAL NOT NULL DEFAULT 0,
  error            TEXT,
  note             TEXT
);
INSERT INTO rescore_request_new
  (id, requested_at, scope, scoring_version, job_count, cost_per_job_usd, estimated_usd,
   status, done_at)
  SELECT id, requested_at, scope, scoring_version, job_count, cost_per_job_usd, estimated_usd,
         status, done_at FROM rescore_request;
DROP TABLE rescore_request;
ALTER TABLE rescore_request_new RENAME TO rescore_request;
CREATE INDEX rescore_request_status ON rescore_request(status);
