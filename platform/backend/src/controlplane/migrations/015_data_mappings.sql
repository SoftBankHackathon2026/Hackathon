CREATE TABLE IF NOT EXISTS fleet_data_mappings (
 job_id text PRIMARY KEY,
 inventory_hash text NOT NULL,
 bindings jsonb NOT NULL,
 confirmed_by text,
 confirmed_at timestamptz NOT NULL
);
