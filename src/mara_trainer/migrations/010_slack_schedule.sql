-- Fixed-time Slack messages queued on Slack's servers (chat.scheduleMessage), so they arrive
-- even when the laptop running the bot is asleep. One row per kind and athlete-local date.
-- status: scheduled (queued), sent (post time passed), cancelled (plan changed),
-- cancelled_live (the bot was awake and sends the live version instead).

CREATE TABLE scheduled_messages (
  kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
  date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  post_at TEXT NOT NULL,
  channel TEXT NOT NULL,
  slack_id TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('scheduled', 'sent', 'cancelled', 'cancelled_live')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (kind, date)
);

-- Small key/value state for the bot, e.g. the newest DM it has handled (for catch-up).
CREATE TABLE slack_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
