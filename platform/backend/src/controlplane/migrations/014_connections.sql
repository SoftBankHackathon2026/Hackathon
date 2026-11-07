-- Isolated from legacy bearer-token enrollments and deployment providers.
CREATE TABLE IF NOT EXISTS connection_requests (
    id text PRIMARY KEY,
    team text NOT NULL,
    owner text NOT NULL,
    operation_id text NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(team, owner, operation_id)
);
CREATE INDEX IF NOT EXISTS connection_requests_team_created_idx
    ON connection_requests(team, created_at DESC);
-- Registered metadata remains in its request aggregate, atomically coupled to
-- the accepted inventory and unique request. It grants no deployment capability.
CREATE UNIQUE INDEX IF NOT EXISTS connection_environment_identity_idx
    ON connection_requests ((document->>'environment_id'))
    WHERE document->>'environment_id' IS NOT NULL;
