-- Fueling log: what the runner ate before and took during a run, and how their stomach and energy
-- held up. It exists to practice race-day fueling, so by design there are no calorie, weight or
-- daily-intake columns. Every measure is nullable: missing stays missing, never zero.

CREATE TABLE fuel_log (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL,
  distance_meters INTEGER CHECK (distance_meters BETWEEN 1 AND 500000),
  duration_seconds INTEGER CHECK (duration_seconds BETWEEN 1 AND 86400),
  pre_run TEXT CHECK (pre_run IS NULL OR length(pre_run) <= 500),
  pre_run_minutes INTEGER CHECK (pre_run_minutes BETWEEN 0 AND 720),
  during TEXT CHECK (during IS NULL OR length(during) <= 500),
  carbs_grams REAL CHECK (carbs_grams BETWEEN 0 AND 1000),
  fluid_ml REAL CHECK (fluid_ml BETWEEN 0 AND 10000),
  stomach INTEGER CHECK (stomach BETWEEN 1 AND 5),
  energy INTEGER CHECK (energy BETWEEN 1 AND 5),
  effort INTEGER CHECK (effort BETWEEN 1 AND 10),
  notes TEXT CHECK (notes IS NULL OR length(notes) <= 2000),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX fuel_log_date ON fuel_log (date DESC);
