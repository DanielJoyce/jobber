-- 0030_packet_runner: assisted apply phase 1b (specs/017 "Data model").
-- runner: which runner generated a version ('cli' = the Claude Code CLI on the subscription,
-- 'api' = the anthropic SDK). NULL for 'base' and 'edited' versions.
-- api_equiv_usd: the CLI's reported total_cost_usd (an API-equivalent figure) for a
-- subscription call, including its entailment pass; shown on /costs for information only and
-- never charged to any cap. cost_usd stays the charged amount (0 on the subscription).
ALTER TABLE packet_document ADD COLUMN runner TEXT CHECK (runner IN ('cli', 'api'));
ALTER TABLE packet_document ADD COLUMN api_equiv_usd REAL;
