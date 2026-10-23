-- Single-athlete schema adapted from the legacy Postgres migrations 001-006.
-- Dropped: users, sessions (auth), agent_runs, athlete_id foreign keys.
-- Units: meters, seconds, UTC ISO-8601 instants; local dates are athlete-local YYYY-MM-DD.

CREATE TABLE athlete (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL DEFAULT 'Athlete' CHECK (length(name) BETWEEN 1 AND 100),
  timezone TEXT NOT NULL,
  units TEXT NOT NULL DEFAULT 'mi' CHECK (units IN ('km', 'mi')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 'strava' is deliberately NOT an allowed source: Strava API data may not be stored (see CLAUDE.md).
CREATE TABLE activities (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('manual', 'fit', 'gpx', 'apple_health')),
  source_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
  started_at TEXT NOT NULL,
  distance_meters INTEGER NOT NULL CHECK (distance_meters > 0 AND distance_meters <= 500000),
  duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0 AND duration_seconds <= 604800),
  rpe INTEGER CHECK (rpe BETWEEN 1 AND 10),
  notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (source, source_id)
);
CREATE INDEX activities_started ON activities (started_at DESC);
CREATE INDEX activities_fingerprint ON activities (fingerprint);

-- Nullable everywhere: missing stays missing.
CREATE TABLE daily_check_ins (
  date TEXT PRIMARY KEY,
  sleep_hours REAL CHECK (sleep_hours BETWEEN 0 AND 24),
  hrv_ms REAL CHECK (hrv_ms > 0),
  resting_hr REAL CHECK (resting_hr BETWEEN 20 AND 250),
  fatigue INTEGER CHECK (fatigue BETWEEN 1 AND 5),
  soreness INTEGER CHECK (soreness BETWEEN 1 AND 5),
  notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000),
  source TEXT NOT NULL DEFAULT 'manual',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- File-level replay detection (same bytes imported twice).
CREATE TABLE import_batches (
  id INTEGER PRIMARY KEY,
  file_hash TEXT NOT NULL UNIQUE CHECK (length(file_hash) = 64),
  filename TEXT NOT NULL,
  source TEXT NOT NULL,
  result TEXT NOT NULL CHECK (json_valid(result)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE race_setup (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  setup TEXT NOT NULL CHECK (json_valid(setup)),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE training_plans (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('draft', 'accepted', 'archived')),
  plan TEXT NOT NULL CHECK (json_valid(plan)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
