ALTER TABLE agent_heartbeats ADD COLUMN IF NOT EXISTS config jsonb;
CREATE TABLE IF NOT EXISTS agent_commands (
 id bigserial PRIMARY KEY,
 agent_id text NOT NULL REFERENCES agent_heartbeats(agent_id),
 kind text NOT NULL,
 payload jsonb NOT NULL,
 requested_by text NOT NULL,
 requested_at timestamptz NOT NULL,
 consumed_at timestamptz,
 result text
);
CREATE INDEX IF NOT EXISTS agent_commands_open
 ON agent_commands(agent_id, requested_at) WHERE consumed_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS agent_commands_open_rerun
 ON agent_commands(agent_id) WHERE kind = 'rerun' AND consumed_at IS NULL;
