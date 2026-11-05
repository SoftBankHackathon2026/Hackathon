CREATE TABLE IF NOT EXISTS fleet_worker_samples (
 job_id text NOT NULL,
 at timestamptz NOT NULL,
 sample jsonb NOT NULL,
 PRIMARY KEY (job_id, at)
);
CREATE TABLE IF NOT EXISTS fleet_cutover (
 job_id text PRIMARY KEY,
 stage text NOT NULL,
 checkpoint jsonb NOT NULL,
 updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_cutover_stages (
 job_id text NOT NULL,
 stage text NOT NULL,
 first_seen_at timestamptz NOT NULL,
 PRIMARY KEY (job_id, stage)
);
CREATE TABLE IF NOT EXISTS fleet_gate_decisions (
 decision_id text PRIMARY KEY,
 job_id text NOT NULL,
 stage text NOT NULL,
 outcome text NOT NULL,
 reason_codes jsonb NOT NULL DEFAULT '[]',
 policy_version text,
 evaluated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS fleet_gate_decisions_job ON fleet_gate_decisions(job_id, evaluated_at);
CREATE TABLE IF NOT EXISTS fleet_verify_reports (
 job_id text NOT NULL,
 mode text NOT NULL CHECK (mode IN ('pre', 'post')),
 report jsonb NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (job_id, mode)
);
CREATE TABLE IF NOT EXISTS fleet_connectors (
 job_id text PRIMARY KEY,
 connectors jsonb NOT NULL,
 observed_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_worker_status (
 job_id text PRIMARY KEY,
 status jsonb NOT NULL,
 observed_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_environment_facts (
 env_id text PRIMARY KEY,
 facts jsonb NOT NULL,
 observed_at timestamptz NOT NULL
);
