-- 0023_rejection: rejections FROM EMPLOYERS (specs/007 "Gmail matching", specs/006
-- "Employer rejections"). Not the same thing as the /rejected page or prefilter "rejected",
-- which are jobs WE filtered out.
-- One row per rejection email (gmail_message_id UNIQUE makes rescans idempotent), or per manual
-- entry (source 'manual', gmail_message_id NULL). Stored as a fact even when the email matches
-- no known job: most applications happen outside jobhunter. employer_norm is the employer name
-- normalized for matching (core/rejections.employer_norm). evidence holds sender, subject, a
-- snippet of at most match.SNIPPET_MAX characters and the matched phrase; never message bodies.
CREATE TABLE rejection (
  id                INTEGER PRIMARY KEY,
  gmail_message_id  TEXT UNIQUE,
  thread_id         TEXT,
  received_at       TEXT NOT NULL,
  employer          TEXT,
  employer_norm     TEXT,
  title             TEXT,
  job_group_id      INTEGER REFERENCES job_group(id),
  application_id    INTEGER REFERENCES application(id),
  source            TEXT NOT NULL CHECK (source IN ('email', 'manual')),
  evidence          TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL
);

CREATE INDEX idx_rejection_employer_norm ON rejection(employer_norm);
CREATE INDEX idx_rejection_job_group ON rejection(job_group_id);
