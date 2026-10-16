-- 0044_apply_link_from_hint: the link won from a fallback start, a link in the posting's text
-- that names no id or title of this job (git-bug 3bab68d). Such a link is never used to
-- merge job groups (dedupe_url, dedupe_xstate) or to match mail, pastes, captures or pages to
-- a job: one employer's text can name the same page in
-- every posting. 0 for every link resolved before this column.
ALTER TABLE apply_link ADD COLUMN from_hint INTEGER NOT NULL DEFAULT 0;
