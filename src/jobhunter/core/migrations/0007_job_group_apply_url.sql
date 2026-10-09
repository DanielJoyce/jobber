-- Allow job_group.method = 'apply_url' (specs/004 dedupe by resolved application URL).
-- SQLite cannot alter a CHECK, so rebuild the table (the 12-step procedure).
-- migrate: foreign_keys=off
-- ^ db.migrate() sees this marker, runs PRAGMA foreign_keys=OFF before BEGIN (it is a no-op
-- inside a transaction), runs PRAGMA foreign_key_check before COMMIT, then turns it back on.
-- (defer_foreign_keys does not work here: DROP TABLE's implicit-delete violations are not
-- cleared by re-creating the parent, so COMMIT fails.)

CREATE TABLE job_group_new (
  id               INTEGER PRIMARY KEY,
  canonical_job_id INTEGER REFERENCES job(id),
  member_count     INTEGER NOT NULL DEFAULT 1,
  method           TEXT NOT NULL
                   CHECK (method IN ('exact_hash', 'near_dupe', 'manual', 'apply_url')),
  confidence       REAL,
  created_at       TEXT NOT NULL
);

INSERT INTO job_group_new (id, canonical_job_id, member_count, method, confidence, created_at)
  SELECT id, canonical_job_id, member_count, method, confidence, created_at FROM job_group;

DROP TABLE job_group;
ALTER TABLE job_group_new RENAME TO job_group;
