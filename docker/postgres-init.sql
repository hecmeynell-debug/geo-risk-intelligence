-- Runs once on first initialisation of the data volume.
-- The migration also creates the extension; doing it here as well means a fresh volume
-- is usable even before migrations run.
CREATE EXTENSION IF NOT EXISTS vector;
