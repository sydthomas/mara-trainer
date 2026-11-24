-- Source gaps: topics the coach was asked about that no approved source covers. A private
-- research backlog (never published, never sent anywhere): a short topic label and a one-line
-- paraphrase in our own words, no health detail beyond the topic. One open or researching row
-- per topic (partial unique index); a repeat ask increments `asks` and moves `last_asked`.
-- Closed rows are kept as history, so a topic can be opened again later as a new row.

CREATE TABLE source_gaps (
  id INTEGER PRIMARY KEY,
  topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 60),
  summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 200),
  category TEXT NOT NULL
    CHECK (category IN ('training', 'fueling', 'recovery', 'gear', 'health', 'other')),
  safety INTEGER NOT NULL DEFAULT 0 CHECK (safety IN (0, 1)),
  asks INTEGER NOT NULL DEFAULT 1 CHECK (asks >= 1),
  first_asked TEXT NOT NULL,
  last_asked TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'researching', 'closed')),
  closed_reason TEXT CHECK (closed_reason IS NULL OR length(closed_reason) <= 500),
  closed_at TEXT,
  notes TEXT CHECK (notes IS NULL OR length(notes) <= 2000),
  CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);
CREATE UNIQUE INDEX source_gaps_topic_active ON source_gaps (topic) WHERE status != 'closed';
