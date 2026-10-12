-- 0042_batch_reserve: what the spend cap held per request when a batch was submitted
-- (specs/020 M5b, git-bug a322711). Uncollected batches stay held at this figure until they
-- are collected, so a hold sized from the profile at submit time is not re-priced lower later.
-- NULL for batches submitted before this column: they are held at the model's current figure.
ALTER TABLE score_batch ADD COLUMN reserve_per_request_usd REAL;
