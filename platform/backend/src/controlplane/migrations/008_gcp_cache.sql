CREATE TABLE IF NOT EXISTS gcp_cache (
 service_name text PRIMARY KEY,
 instance_name text NOT NULL UNIQUE,
 policy_fingerprint text NOT NULL,
 checkpoint jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(8) ON CONFLICT DO NOTHING;
