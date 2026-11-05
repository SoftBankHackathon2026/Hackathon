from enum import IntEnum

from psycopg import Connection


class AwsLock(IntEnum):
    SCHEMA = 21
    PRIORITY = 22


def aws_xact_lock(conn: Connection, lock: AwsLock) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), %s)", (int(lock),))


SCHEMA = """
CREATE TABLE IF NOT EXISTS oneport_apps (
  app_id text NOT NULL,
  environment_id text NOT NULL,
  port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
  health_path text NOT NULL,
  domain text NOT NULL,
  rule_priority integer NOT NULL CHECK (rule_priority BETWEEN 1 AND 50000),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_id, environment_id),
  UNIQUE (environment_id, rule_priority)
);
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS database text NOT NULL DEFAULT 'none';
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS database_mode text NOT NULL DEFAULT 'dedicated';
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS db_state text NOT NULL DEFAULT 'none';
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS db_error text;
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS db_updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS redis boolean NOT NULL DEFAULT false;
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS cache_state text NOT NULL DEFAULT 'none';
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS cache_error text;
ALTER TABLE oneport_apps ADD COLUMN IF NOT EXISTS cache_updated_at timestamptz NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS oneport_restores (
  release_id text PRIMARY KEY,   -- the new release in the team `releases` journal
  restore_of text NOT NULL,      -- the earlier succeeded release whose revision it re-deploys
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oneport_app_secrets (
  app_id text NOT NULL,
  environment_id text NOT NULL,
  binding text NOT NULL,
  ref text NOT NULL,  -- Secrets Manager ARN pinned to a VersionId; never a value
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_id, environment_id, binding)
);
"""
