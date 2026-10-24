-- Runner profile for the coach: preferences and history the runner writes in their own words.
-- One row (single athlete). Every field is nullable: missing stays missing, never a default.
-- List fields hold JSON arrays of short strings; an empty array means "none", null means unknown.
-- Read and written by `mara_coach.profile`; the core only owns the schema.

CREATE TABLE athlete_profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  diet TEXT CHECK (diet IS NULL OR length(diet) <= 2000),
  allergies TEXT CHECK (allergies IS NULL OR
    CASE WHEN json_valid(allergies) THEN json_type(allergies) = 'array' ELSE 0 END),
  shoe_likes TEXT CHECK (shoe_likes IS NULL OR
    CASE WHEN json_valid(shoe_likes) THEN json_type(shoe_likes) = 'array' ELSE 0 END),
  shoe_dislikes TEXT CHECK (shoe_dislikes IS NULL OR
    CASE WHEN json_valid(shoe_dislikes) THEN json_type(shoe_dislikes) = 'array' ELSE 0 END),
  current_shoes TEXT CHECK (current_shoes IS NULL OR
    CASE WHEN json_valid(current_shoes) THEN json_type(current_shoes) = 'array' ELSE 0 END),
  injury_history TEXT CHECK (injury_history IS NULL OR length(injury_history) <= 2000),
  notes TEXT CHECK (notes IS NULL OR length(notes) <= 2000),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
