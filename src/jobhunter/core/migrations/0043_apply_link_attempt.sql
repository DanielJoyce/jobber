-- 0043_apply_link_attempt: how many times a group's apply link was resolved (bug c369bcc).
--
-- One row per group that was tried, written by the nightly apply_links pass and by the
-- console's on-demand walk, whether or not the try stored an apply_link row (a deferred try
-- stores none). A group whose link is not settled (no row, or an 'unresolved' row not set by
-- the user) is tried again until attempts reaches apply_links.max_attempts, spaced by
-- apply_attempts.BACKOFF_H after last_at. Reset on the job page clears the row.
-- last_outcome: live | expired | unresolved | blocked | deferred | user_link | no_url | error.
CREATE TABLE apply_link_attempt (
  job_group_id INTEGER PRIMARY KEY REFERENCES job_group(id),
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_at      TEXT NOT NULL,
  last_outcome TEXT
);
