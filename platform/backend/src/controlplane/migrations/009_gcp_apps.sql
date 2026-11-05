-- No credentials: only app setup operations and pinned Cloud Run identity.
CREATE TABLE IF NOT EXISTS gcp_apps (
 service_name text PRIMARY KEY,
 owner text NOT NULL,
 checkpoint jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(9) ON CONFLICT DO NOTHING;
