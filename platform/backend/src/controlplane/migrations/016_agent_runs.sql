CREATE TABLE IF NOT EXISTS agent_heartbeats (
 agent_id text PRIMARY KEY,
 version text NOT NULL,
 auto_apply boolean NOT NULL,
 interval_seconds integer NOT NULL,
 observed_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
 run_id text PRIMARY KEY,
 agent_id text NOT NULL,
 job_id text NOT NULL,
 state text NOT NULL,
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 detail jsonb NOT NULL,
 events jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_runs_recent ON agent_runs(created_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS agent_runs_agent ON agent_runs(agent_id);
CREATE TABLE IF NOT EXISTS agent_approvals (
 id bigserial PRIMARY KEY,
 run_id text NOT NULL REFERENCES agent_runs(run_id),
 decision text NOT NULL,
 requested_by text NOT NULL,
 requested_at timestamptz NOT NULL,
 consumed_at timestamptz,
 result_state text
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_approvals_open
 ON agent_approvals(run_id) WHERE consumed_at IS NULL;
