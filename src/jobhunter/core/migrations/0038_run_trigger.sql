-- 0038_run_trigger: what started each run (specs/019 "What the nightly run sees", ticket S4).
--
-- 'timer' when the run unit set JOBHUNTER_TRIGGER=timer, 'manual' otherwise. Rows from before
-- this migration stay NULL (unknown), not 'manual', so the upgrade itself raises no alarm about
-- timers that are not running.

ALTER TABLE run ADD COLUMN "trigger" TEXT CHECK ("trigger" IN ('timer', 'manual'));
