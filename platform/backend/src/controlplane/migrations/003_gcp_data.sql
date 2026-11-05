CREATE TABLE IF NOT EXISTS gcp_data (
 service_name text PRIMARY KEY,
 policy_fingerprint text NOT NULL,
 checkpoint jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(3) ON CONFLICT DO NOTHING;
