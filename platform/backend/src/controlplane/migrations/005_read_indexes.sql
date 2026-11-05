-- Release detail and history read events by release; without this index every
-- read scanned the whole event table, which only grows.
CREATE INDEX IF NOT EXISTS release_events_by_release ON release_events(release_id, id);
INSERT INTO schema_migrations(version) VALUES(5) ON CONFLICT DO NOTHING;
