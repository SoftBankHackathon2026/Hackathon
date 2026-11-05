CREATE TABLE IF NOT EXISTS release_restores (
 release_id text PRIMARY KEY REFERENCES releases(id),
 restore_of text NOT NULL REFERENCES releases(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
