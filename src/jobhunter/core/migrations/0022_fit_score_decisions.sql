-- 0022_fit_score_decisions: decisions-model scorers (Jev, specs/016 "Jev decisions scorer").
-- evidence_mode says how a row's rationale can be checked: 'quotes' (verbatim evidence quotes,
-- verified against the posting) or 'none' (a decisions model returns probabilities, not
-- quotes, and eval leaves these rows out of the evidence_unverified rate).
-- decisions holds the raw answers (probabilities and confidence per question) as JSON.
ALTER TABLE fit_score ADD COLUMN evidence_mode TEXT NOT NULL DEFAULT 'quotes'
  CHECK (evidence_mode IN ('quotes', 'none'));
ALTER TABLE fit_score ADD COLUMN decisions TEXT;
