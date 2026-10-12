-- 0041_page_match: matching an application form to a job (specs/022 "Matching the page to a job").
--
-- ats_posting_ref: your pick (or Add this job) for an ATS posting key, so the next visit to the
-- same posting decides at once (matching rule 2). It points at a job, not a group, so the
-- nightly merges carry it: jobs are only ever re-pointed to the surviving group.
--
-- fill_session: a Fill session bound to an application (specs/022 "Data"). Created here because
-- matching rule 1 reads it; the fill routes that write it come with ticket A5. start_url and
-- tab_id bind a Fill-this-page session (no posting key) to its own tab and starting page. The
-- report never holds field values. No personal data: ids, URLs and fixed words only.

CREATE TABLE ats_posting_ref (
    posting_key TEXT PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES job(id),
    set_by      TEXT NOT NULL CHECK (set_by IN ('pick', 'add')),
    seen_at     TEXT NOT NULL
);
CREATE INDEX ats_posting_ref_job ON ats_posting_ref(job_id);

CREATE TABLE fill_session (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id),
    packet_id      INTEGER REFERENCES application_packet(id),  -- NULL: no packet
    ats            TEXT NOT NULL,                              -- 'generic' on Fill this page
    posting_key    TEXT,                                       -- NULL on Fill this page
    start_url      TEXT,                                       -- page_match.start_url_for (fragment kept)
    tab_id         INTEGER,                                    -- the Chrome tab, from the worker
    host           TEXT NOT NULL,
    ext_version    TEXT,
    attach         TEXT CHECK (attach IN ('datatransfer', 'drop', 'manual', 'none')),
    attach_failed  INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    finished_at    TEXT,
    outcome        TEXT CHECK (outcome IN ('stopped_before_submit', 'submit_seen', 'applied',
                                           'challenge', 'aborted', 'error')),
    report         TEXT   -- JSON: field keys, labels, actions, attached document ids. Never values
);
CREATE INDEX fill_session_application ON fill_session(application_id);
CREATE INDEX fill_session_tab ON fill_session(tab_id);
