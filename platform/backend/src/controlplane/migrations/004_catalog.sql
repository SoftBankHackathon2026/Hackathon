CREATE TABLE IF NOT EXISTS applications (
 id text PRIMARY KEY, name text NOT NULL, team text, created_by text,
 creation_key text UNIQUE, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS applications_owner_name ON applications(name, COALESCE(team,''));
CREATE TABLE IF NOT EXISTS deployment_targets (
 id text PRIMARY KEY, name text NOT NULL, runtime text NOT NULL,
 provider_binding_id text NOT NULL UNIQUE, creation_enabled boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS app_environments (
 id text PRIMARY KEY, application_id text NOT NULL REFERENCES applications(id),
 target_id text NOT NULL REFERENCES deployment_targets(id),
 runtime_application text NOT NULL, hostname text UNIQUE,
 binding jsonb, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(application_id,target_id)
);
INSERT INTO schema_migrations(version) VALUES(4) ON CONFLICT DO NOTHING;
