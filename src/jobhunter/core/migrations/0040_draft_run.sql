-- 0040_draft_run: one row per drafting run (specs/022 "Drafting in the background").
--
-- generator.start writes the row once it holds the document's claim; run_claimed sets its
-- outcome. A row with no outcome whose claim is no longer held (the console restarted mid-run)
-- reads as "stopped". error holds the refusal or failure text shown to you, never model output.
-- No personal data: ids, kinds, the question key, runner names and times.

CREATE TABLE draft_run (
    id            INTEGER PRIMARY KEY,
    packet_id     INTEGER NOT NULL REFERENCES application_packet(id),
    kind          TEXT NOT NULL,
    question_key  TEXT NOT NULL DEFAULT '',
    runner        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT CHECK (outcome IN ('done', 'failed')),  -- NULL + no claim = stopped
    doc_id        INTEGER,
    error         TEXT
);
CREATE INDEX draft_run_doc ON draft_run(packet_id, kind, question_key, id);
