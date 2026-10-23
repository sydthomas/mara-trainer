-- Source registry for the coach: vetted references the agent may cite.
-- Stores links, metadata and our own short notes only, never copied text from the source.
-- Only 'approved' rows feed the agent's allowed web domains; approval needs a named person.

CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE CHECK (slug GLOB '[a-z0-9]*' AND length(slug) BETWEEN 2 AND 80),
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
  author TEXT NOT NULL CHECK (length(author) BETWEEN 1 AND 300),
  kind TEXT NOT NULL CHECK (kind IN
    ('position_stand', 'review', 'research', 'guideline', 'organization', 'coach', 'book')),
  url TEXT CHECK (url IS NULL OR url GLOB 'https://*'),
  domain TEXT,
  topics TEXT NOT NULL CHECK (json_valid(topics) AND json_array_length(topics) >= 1),
  license TEXT NOT NULL CHECK (license IN
    ('open_access', 'cc_by', 'public_domain', 'copyrighted', 'unknown')),
  year INTEGER CHECK (year BETWEEN 1900 AND 2100),
  trust_reason TEXT NOT NULL CHECK (length(trust_reason) BETWEEN 1 AND 500),
  notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'retired')),
  approved_by TEXT,
  approved_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  CHECK ((url IS NULL) = (domain IS NULL)),
  CHECK (status != 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);
CREATE INDEX sources_status ON sources (status);
