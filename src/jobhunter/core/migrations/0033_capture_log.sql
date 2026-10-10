-- 0033_capture_log: one row per extension action (specs/017 "Data", phase 1e).
-- action_id is a random UUID the extension mints per user action; a repeated write action
-- returns the stored response. No foreign keys: an 'existing' match may be an ingested group
-- that a nightly merge later deletes, and the log may dangle. No page text is stored.

CREATE TABLE capture_log (
  id            INTEGER PRIMARY KEY,
  action_id     TEXT NOT NULL UNIQUE,
  route         TEXT NOT NULL,
  job_group_id  INTEGER,
  job_id        INTEGER,
  captured_at   TEXT NOT NULL,
  host          TEXT NOT NULL,
  method        TEXT CHECK (method IN ('jsonld', 'microdata', 'site', 'page', 'selection',
                                     'fetch')),
  outcome       TEXT NOT NULL CHECK (outcome IN ('added', 'existing', 'possible', 'previewed',
                                                 'description_added', 'linked', 'same_job',
                                                 'fetched', 'linked_groups', 'not_same')),
  response      TEXT,
  ext_version   TEXT
);
CREATE INDEX capture_log_host ON capture_log(host);
