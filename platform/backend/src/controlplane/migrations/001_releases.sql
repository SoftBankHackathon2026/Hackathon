CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS releases (
 id text PRIMARY KEY, application text NOT NULL, environment_id text NOT NULL,
 idempotency_key text NOT NULL UNIQUE, fingerprint text NOT NULL,
 request jsonb NOT NULL, binding_fingerprint text NOT NULL,
 runtime text NOT NULL, next_attempt_at timestamptz NOT NULL DEFAULT now(),
 state text NOT NULL, checkpoint jsonb NOT NULL DEFAULT '{}',
 service_url text, error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 deadline timestamptz NOT NULL DEFAULT now() + interval '12 minutes'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_release ON releases(application, environment_id)
 WHERE state NOT IN ('succeeded', 'failed');
CREATE TABLE IF NOT EXISTS release_events (
 id bigserial PRIMARY KEY, release_id text NOT NULL REFERENCES releases(id),
 state text NOT NULL, message text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS due_release_by_runtime ON releases(runtime,next_attempt_at,created_at)
 WHERE state NOT IN ('succeeded','failed','uncertain');
