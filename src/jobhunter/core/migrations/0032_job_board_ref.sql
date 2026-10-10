-- 0032_job_board_ref: LinkedIn and Indeed job ids, and where their Apply button goes
-- (specs/017 "LinkedIn and Indeed: the Apply control", "Data", phase 1e).
-- Keyed by (board, board id) and pointing at a job, not a group: merges move jobs between
-- groups, so the link follows. apply_url is decoded locally from the page's Apply control and
-- is never fetched from the board.

CREATE TABLE job_board_ref (
  board       TEXT NOT NULL CHECK (board IN ('linkedin', 'indeed')),
  board_id    TEXT NOT NULL,
  job_id      INTEGER NOT NULL REFERENCES job(id),
  apply_mode  TEXT NOT NULL DEFAULT 'unknown'
              CHECK (apply_mode IN ('easy_apply', 'offsite', 'unknown')),
  apply_url   TEXT,
  seen_at     TEXT NOT NULL,
  PRIMARY KEY (board, board_id)
);
CREATE INDEX job_board_ref_job ON job_board_ref(job_id);

-- The page a capture was made on, when it differs from the URL chosen for job.url (a JSON-LD
-- url or the canonical link). Matching a later ingest tries both (specs/017 "A later ingest of
-- a captured posting"). NULL for every other job.
ALTER TABLE job ADD COLUMN page_url TEXT;
