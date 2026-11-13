-- Body check / niggle log: the runner's own green / yellow / red self-report, with optional
-- detail (where, swelling, pain 0-10). Several entries per day are allowed (before and after a
-- run). Storage only: nothing here classifies an entry or decides whether to run. Every detail is
-- nullable: missing stays missing, never zero (swelling 0 and pain 0 are real answers).

CREATE TABLE niggle_log (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  status TEXT NOT NULL CHECK (status IN ('green', 'yellow', 'red')),
  context TEXT CHECK (context IN ('before_run', 'after_run', 'other')),
  location TEXT CHECK (location IS NULL OR length(location) <= 200),
  swelling INTEGER CHECK (swelling IN (0, 1)),
  pain INTEGER CHECK (pain BETWEEN 0 AND 10),
  notes TEXT CHECK (notes IS NULL OR length(notes) <= 2000),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX niggle_log_date ON niggle_log (date DESC);
