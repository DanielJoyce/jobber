-- 0026_rejection_state: a rejection read from a loose phrase needs the user's confirmation
-- (specs/007 "Gmail matching", bug d28c8de). 'confirmed' rows are facts that hide the posting
-- and flag other roles; 'pending' rows do nothing until confirmed on /rejections. Specific
-- phrases ("decided to move forward with other candidates") are confirmed when recorded;
-- a loose word alone ("unfortunately", "not selected") is pending, since confirmations and
-- interview mail use those words too.
ALTER TABLE rejection ADD COLUMN state TEXT NOT NULL DEFAULT 'confirmed'
  CHECK (state IN ('pending', 'confirmed'));

-- Existing email rows recorded on a loose word, whose stored snippet shows no specific
-- rejection phrase either, become pending. Manual rows and specific phrases stay confirmed.
UPDATE rejection SET state = 'pending'
WHERE source = 'email'
  AND json_extract(evidence, '$.phrase') IN ('unfortunately', 'not selected')
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%not move forward%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%not to move forward%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%not moving forward%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%not be moving forward%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%forward with other%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%not be proceeding%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%pursue other candidates%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%no longer under consideration%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%has been filled%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%regret to inform%'
  AND lower(COALESCE(json_extract(evidence, '$.snippet'), '')) NOT LIKE '%unable to offer you%';

CREATE INDEX idx_rejection_state ON rejection(state);
