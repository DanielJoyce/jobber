-- Gmail application matching (specs/007 "Optional: Gmail matching"): proposals only.
-- One row per Gmail message; the unique message id makes rescans idempotent. Only a short
-- snippet, sender, subject and ids are stored (inside evidence JSON), never message bodies.
CREATE TABLE mail_proposal (
  id                INTEGER PRIMARY KEY,
  gmail_message_id  TEXT NOT NULL UNIQUE,
  thread_id         TEXT,
  received_at       TEXT NOT NULL,
  kind              TEXT NOT NULL
                    CHECK (kind IN ('confirmation', 'acknowledgement', 'rejection',
                                    'interview', 'offer')),
  proposed_action   TEXT NOT NULL CHECK (proposed_action IN ('create_application', 'add_event')),
  application_id    INTEGER REFERENCES application(id),
  job_group_id      INTEGER REFERENCES job_group(id),
  proposed_status   TEXT NOT NULL,
  confidence        REAL NOT NULL,
  evidence          TEXT NOT NULL,
  state             TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'accepted', 'dismissed')),
  created_at        TEXT NOT NULL,
  decided_at        TEXT
);

CREATE INDEX idx_mail_proposal_state ON mail_proposal(state, received_at);
