-- Login mode: users, browser sessions, and which team owns each registered environment.
CREATE TABLE IF NOT EXISTS platform_users (
 id text PRIMARY KEY, email text NOT NULL UNIQUE, password_hash text NOT NULL,
 team text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
-- Only the SHA-256 of the session token is stored.
CREATE TABLE IF NOT EXISTS platform_sessions (
 token_hash text PRIMARY KEY, user_id text NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
-- An environment runs one app, so the first team that registers or deploys it owns it.
CREATE TABLE IF NOT EXISTS environment_teams (
 environment_id text PRIMARY KEY, team text NOT NULL, claimed_by text NOT NULL,
 claimed_at timestamptz NOT NULL DEFAULT now()
);
-- NULL for releases made in operator/demo mode or before login mode.
ALTER TABLE releases ADD COLUMN IF NOT EXISTS team text;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS created_by text;
CREATE INDEX IF NOT EXISTS releases_by_team ON releases(team, created_at DESC);
INSERT INTO schema_migrations(version) VALUES(2) ON CONFLICT DO NOTHING;
