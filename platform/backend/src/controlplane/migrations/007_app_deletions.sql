-- App deletion: one durable request per environment, run by that provider's worker.
-- `archived` hides releases made before a completed deletion from history and from
-- workers looking for a previous release: 'app' (app resources removed, data kept)
-- or 'all' (data removed too).
ALTER TABLE releases ADD COLUMN IF NOT EXISTS archived text;
CREATE TABLE IF NOT EXISTS app_deletions (
 id text PRIMARY KEY, application text NOT NULL, environment_id text NOT NULL,
 runtime text NOT NULL, idempotency_key text NOT NULL UNIQUE,
 delete_data boolean NOT NULL DEFAULT false,
 state text NOT NULL, checkpoint jsonb NOT NULL DEFAULT '{}', error_code text,
 team text, created_by text,
 next_attempt_at timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_deletion ON app_deletions(environment_id)
 WHERE state NOT IN ('deleted', 'failed');
CREATE TABLE IF NOT EXISTS app_deletion_events (
 id bigserial PRIMARY KEY, deletion_id text NOT NULL REFERENCES app_deletions(id),
 state text NOT NULL, message text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(7) ON CONFLICT DO NOTHING;
