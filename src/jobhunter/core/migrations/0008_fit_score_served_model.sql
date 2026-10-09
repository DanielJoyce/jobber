-- 0008_fit_score_served_model: the model that actually answered (specs/016 rule 2).
-- fit_score.model stays the configured scorer name; routers (typesafe/jev-router) serve
-- different models per request, so the reply's model is recorded alongside it. NULL on old rows.
ALTER TABLE fit_score ADD COLUMN served_model TEXT;
