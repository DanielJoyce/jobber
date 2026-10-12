-- 0035_achievements: storage and write points for the achievements board (specs/021 "Data",
-- ticket A1). All additive. The rules that read these come later (A2).
--
-- achievement_unlock: one row per earned tier. achievement_setting: enabled, notify,
-- hidden_categories, backfilled_at, mail_setup_at. followup_log: planned / done / no-response
-- records, since Done clears next_action and kept no trace. The three new columns stay NULL on
-- rows written before this migration ("predates the migration").
--
-- The existing next actions get one `planned` row each, marked backfill = 1, so a later Done can
-- find the date it closes; a backfill row is never an active day.

CREATE TABLE achievement_unlock (
  id              INTEGER PRIMARY KEY,
  achievement_id  TEXT NOT NULL,              -- 'apply.momentum'
  tier            INTEGER NOT NULL DEFAULT 1,  -- 1-based into the family's thresholds
  unlocked_at     TEXT NOT NULL,              -- the crossing date (display)
  detected_at     TEXT NOT NULL,              -- when evaluate() wrote it (revocation clock)
  evidence        TEXT NOT NULL,              -- JSON {"application": [12, 15]}: display only
  catalog_version INTEGER NOT NULL,
  seen_at         TEXT,                       -- NULL: notice not yet shown or dismissed
  UNIQUE (achievement_id, tier)
);
CREATE INDEX achievement_unlock_unseen ON achievement_unlock(seen_at) WHERE seen_at IS NULL;

CREATE TABLE followup_log (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  at             TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('planned', 'done', 'no_response')),
  due_at         TEXT,                         -- the next_action_at in force (date or time)
  backfill       INTEGER NOT NULL DEFAULT 0 CHECK (backfill IN (0, 1))
);
CREATE INDEX followup_log_app ON followup_log(application_id);

CREATE TABLE achievement_setting (            -- enabled, notify, hidden_categories, backfilled_at
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE contact ADD COLUMN created_at TEXT;            -- NULL: predates this migration
ALTER TABLE application_event ADD COLUMN recorded_at TEXT; -- NULL: predates this migration
ALTER TABLE rejection ADD COLUMN confirmed_at TEXT;        -- set when you confirm a pending row

INSERT INTO followup_log (application_id, at, kind, due_at, backfill)
  SELECT id, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), 'planned', next_action_at, 1
  FROM application WHERE next_action_at IS NOT NULL AND next_action_at != '';
