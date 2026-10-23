-- How well a source's evidence covers women. Most sport-science studies have been done mostly on
-- men, so the coach should prefer women-tested evidence and say so when only male data exists.
ALTER TABLE sources ADD COLUMN female_evidence TEXT NOT NULL DEFAULT 'unknown' CHECK (
  female_evidence IN ('female_specific', 'sex_reported', 'mixed_unreported', 'mostly_male',
    'not_applicable', 'unknown')
);
ALTER TABLE sources ADD COLUMN population_note TEXT NOT NULL DEFAULT ''
  CHECK (length(population_note) <= 300);
