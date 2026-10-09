-- 0005_profile_snapshot: /prefs history support (specs/014 "History and undo").

-- The last profile state the system has seen, so hand edits to preferences.yaml can be
-- diffed and logged to profile_change with source = 'file'. Single row.
CREATE TABLE profile_snapshot (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  at              TEXT NOT NULL,
  data            TEXT NOT NULL,             -- JSON: editable profile fields + resume hash
  filter_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL
);

-- A paid profile change asked to re-score existing jobs. The web request only records the
-- choice; a later scoring run picks up pending rows (never the console itself).
CREATE TABLE rescore_request (
  id               INTEGER PRIMARY KEY,
  requested_at     TEXT NOT NULL,
  scope            TEXT NOT NULL CHECK (scope IN ('recent', 'all')),  -- recent: open A-E, 14 days
  scoring_version  TEXT NOT NULL,
  job_count        INTEGER NOT NULL,
  cost_per_job_usd REAL NOT NULL,
  estimated_usd    REAL NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'done', 'canceled')),
  done_at          TEXT
);
CREATE INDEX rescore_request_status ON rescore_request(status);
