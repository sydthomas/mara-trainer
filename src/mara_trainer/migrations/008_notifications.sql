-- Proactive messages the coach has sent (the long-run debrief check-in), one row per kind and
-- athlete-local date, so a restarted bot never sends the same one twice.

CREATE TABLE notifications (
  kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
  date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (kind, date)
);
