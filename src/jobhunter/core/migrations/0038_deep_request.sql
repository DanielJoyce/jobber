-- 0038_deep_request: a deep pass confirmed in the console (specs/020 "Deep pass from the console").
--
-- A row exists only once you confirm: there is no pending state. It carries the confirmed plan
-- (mode, model, scoring_version, the exact group ids, the estimate and the spend ceiling), so
-- the run can never grow past what was confirmed, and its progress, so a reload or the
-- activity band can read it. error holds fixed text or an exception type, never a message
-- from the API. No personal data: group ids, model specs and dollar figures only.

CREATE TABLE deep_request (
    id INTEGER PRIMARY KEY,
    requested_at TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('next', 'rerun', 'group')),
    scorer TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    group_ids TEXT NOT NULL,            -- JSON list of ints, in run order
    estimated_usd REAL NOT NULL,
    max_usd REAL NOT NULL,              -- the ceiling: no call starts unless its worst case fits
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done', 'failed', 'canceled')),
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,    -- groups taken, each at most once
    scored INTEGER NOT NULL DEFAULT 0,  -- new deep reports written
    spent_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    note TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    cancel_requested_at TEXT,
    done_at TEXT
);
CREATE INDEX deep_request_status ON deep_request(status);
