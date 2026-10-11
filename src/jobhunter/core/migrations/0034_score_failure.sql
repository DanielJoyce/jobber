-- 0034_score_failure: paid scoring attempts that wrote no fit_score row and should not be
-- retried automatically (bug 15f7012 review). A Haiku 5.5 screen refusal is billed in some
-- categories and would otherwise be re-submitted by every daily run; a deep pass cut off at
-- max_tokens would otherwise be paid for again by every `score --deep N`. Keyed like fit_score
-- so a new description revision, prompt, scoring_version or model tries again. Explicit
-- requests (a confirmed re-score, `score --deep-group`, Score it) still run. No foreign key:
-- a nightly merge may delete the group and the row may dangle, harmlessly.

CREATE TABLE score_failure (
  id              INTEGER PRIMARY KEY,
  job_group_id    INTEGER NOT NULL,
  tier            TEXT NOT NULL CHECK (tier IN ('screen', 'deep')),
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  scoring_version TEXT NOT NULL,
  input_rev       INTEGER NOT NULL DEFAULT 0,
  reason          TEXT NOT NULL,             -- refusal[:category] | max_tokens
  created_at      TEXT NOT NULL
);
CREATE INDEX score_failure_group ON score_failure(job_group_id, tier);
