-- 0031_group_score_on_request: "scored only on request" is a property of the group
-- (specs/017 "Scored on request: a group flag", phase 1e).
-- Phase 1a keyed it on the canonical job's source_key, which breaks once a captured or pasted
-- group gains an ingested member: dedupe._refresh_group may make the fuller ingested copy
-- canonical, and the nightly screen would then pay for a posting the user chose not to score.
-- The nightly prefilter, screen and re-score plans check this flag instead; canonical choice is
-- about content only. Backfilled for every group whose canonical job is a manual source.

ALTER TABLE job_group ADD COLUMN score_on_request INTEGER NOT NULL DEFAULT 0
  CHECK (score_on_request IN (0, 1));

UPDATE job_group SET score_on_request = 1
WHERE canonical_job_id IN (
  SELECT id FROM job WHERE source_key IN ('paste-manual', 'email-manual')
);
