ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS hidden_by text;
CREATE INDEX IF NOT EXISTS agent_runs_visible
 ON agent_runs(created_at DESC, run_id DESC) WHERE hidden_at IS NULL;
