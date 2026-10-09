-- 0021_alert_signup: per-board email alert subscriptions and the "+ rejected" list (specs/012
-- "Subscribing"). User-entered state only; alert arrivals come from mail_message.

-- One row per registry source the user has (un)subscribed to by hand.
CREATE TABLE alert_signup (
  source_key      TEXT PRIMARY KEY,
  subscribed_at   TEXT,            -- set when marked subscribed; NULL when unsubscribed
  unsubscribed_at TEXT,
  updated_at      TEXT NOT NULL
);

-- Sender domains whose board refused the +jobs address. The user adds them to
-- mail.fallback_sender_domains in config.toml, which is what filters actually use.
CREATE TABLE alert_plus_rejected (
  domain      TEXT PRIMARY KEY,    -- bare lowercase domain, no leading @ or www.
  source_key  TEXT,                -- the registry row the rejection was recorded from
  rejected_at TEXT NOT NULL
);
