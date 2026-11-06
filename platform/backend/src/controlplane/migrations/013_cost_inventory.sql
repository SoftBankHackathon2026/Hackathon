CREATE TABLE IF NOT EXISTS cost_inventory (
 env_id text PRIMARY KEY,
 inventory jsonb NOT NULL,
 observed_at timestamptz NOT NULL
);
