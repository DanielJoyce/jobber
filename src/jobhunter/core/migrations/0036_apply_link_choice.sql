-- 0036_apply_link_choice: who chose a group's apply link and why (bug 334d9eb).
--
-- chosen_by: NULL when the resolver ranked it (specs/015), 'capture' when an extension capture
-- of the employer page (or a board's off-site Apply) supplied it, 'user' when it was set by hand
-- on the job page. A 'user' link is never replaced by automatic resolution; Reset deletes it.
-- why: one line shown under the Apply button, e.g. "via www.aplitrak.com → jobs.ashbyhq.com".

ALTER TABLE apply_link ADD COLUMN chosen_by TEXT CHECK (chosen_by IN ('capture', 'user'));
ALTER TABLE apply_link ADD COLUMN why TEXT;
