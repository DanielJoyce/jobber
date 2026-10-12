-- 0037_model_change: the history of model choices saved to models.toml (specs/020 Storage).
--
-- One row per field a Save, Reset, Revert, `jobhunter models set` or `models reset` changed.
-- old_value and new_value are what models.toml held for that field before and after (NULL: the
-- file did not set it, so config.toml or the default applied). Revert writes old_value back as
-- a new change; it never rewinds. Values are scorer specs, model ids, runner and effort names,
-- or "true"/"false": no personal data.

CREATE TABLE model_change (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    use TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL CHECK (source IN ('ui', 'cli', 'reset', 'revert'))
);
CREATE INDEX model_change_at ON model_change(at);
