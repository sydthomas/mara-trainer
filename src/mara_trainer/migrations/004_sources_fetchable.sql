-- Link-only sources: cited and linked, but their domain is never fetched at answer time
-- (site terms forbid automated access, or the URL is a resolver like doi.org).
ALTER TABLE sources ADD COLUMN fetchable INTEGER NOT NULL DEFAULT 1 CHECK (fetchable IN (0, 1));
UPDATE sources SET fetchable = 0 WHERE domain = 'doi.org';
