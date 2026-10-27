-- One row per coach answer (Claude API calls summed over the tool loop): token counts and cost
-- only. No question or answer text, by design.

CREATE TABLE api_usage (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  channel TEXT NOT NULL CHECK (length(channel) BETWEEN 1 AND 32),
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cache_read_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_input_tokens >= 0),
  cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_creation_input_tokens >= 0),
  steps INTEGER NOT NULL CHECK (steps >= 1),
  cost_usd REAL  -- null when the model has no price in mara_coach/usage.py
);

CREATE INDEX api_usage_at ON api_usage(at);
