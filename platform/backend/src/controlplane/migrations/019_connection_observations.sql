-- Keep observations out of the connection document rewritten by every agent poll.
ALTER TABLE connection_requests ADD COLUMN IF NOT EXISTS observation jsonb;
