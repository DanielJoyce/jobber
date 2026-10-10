-- 0026_rescore_plan: a re-score runs the plan the user confirmed, nothing more (specs/014
-- "Re-score now"). group_ids is the JSON list of job_group ids the confirmed plan covers; the run
-- sends each of them at most once and never any other group. max_usd is the spend the user
-- confirmed (the estimate plus headroom); the run stops before it would go past it. Both are
-- NULL for a request queued by a /prefs save until something plans it.
ALTER TABLE rescore_request ADD COLUMN group_ids TEXT;
ALTER TABLE rescore_request ADD COLUMN max_usd REAL;
