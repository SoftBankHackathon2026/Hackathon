CREATE TABLE IF NOT EXISTS fleet_migration_tasks (
 job_id text PRIMARY KEY,
 config jsonb NOT NULL,
 created_by text,
 created_at timestamptz NOT NULL DEFAULT now()
);
