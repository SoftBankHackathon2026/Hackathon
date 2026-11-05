CREATE TABLE IF NOT EXISTS demo_deployment_previews (
 id text PRIMARY KEY, team text, created_by text, snapshot jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now() + interval '15 minutes'
);
CREATE TABLE IF NOT EXISTS demo_deployment_runs (
 id text PRIMARY KEY, preview_id text NOT NULL UNIQUE REFERENCES demo_deployment_previews(id),
 idempotency_key text NOT NULL UNIQUE, team text, created_by text,
 snapshot jsonb NOT NULL,
 state text NOT NULL DEFAULT 'queued', build_id text, image text, error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS demo_deployment_run_targets (
 run_id text NOT NULL REFERENCES demo_deployment_runs(id), runtime text NOT NULL,
 environment_id text NOT NULL, application text NOT NULL,
 state text NOT NULL DEFAULT 'pending', attempt integer NOT NULL DEFAULT 1,
 release_id text REFERENCES releases(id), error_code text, service_url text,
 PRIMARY KEY(run_id,runtime)
);
CREATE TABLE IF NOT EXISTS demo_deployment_run_attempts (
 run_id text NOT NULL, runtime text NOT NULL, attempt integer NOT NULL,
 release_id text NOT NULL REFERENCES releases(id),
 PRIMARY KEY(run_id,runtime,attempt),
 FOREIGN KEY(run_id,runtime) REFERENCES demo_deployment_run_targets(run_id,runtime)
);
ALTER TABLE demo_deployment_run_targets ADD COLUMN IF NOT EXISTS owner_team text;
UPDATE demo_deployment_run_targets t SET owner_team=r.team FROM demo_deployment_runs r
 WHERE t.run_id=r.id AND t.owner_team IS NULL AND r.team IS NOT NULL;
CREATE TABLE IF NOT EXISTS demo_deployment_run_retries (
 idempotency_key text PRIMARY KEY, run_id text NOT NULL REFERENCES demo_deployment_runs(id),
 runtimes jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS demo_deployment_runs_team_created ON demo_deployment_runs(team,created_at DESC);
