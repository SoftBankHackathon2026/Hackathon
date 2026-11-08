ALTER TABLE fleet_migration_tasks ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
ALTER TABLE fleet_migration_tasks ADD COLUMN IF NOT EXISTS hidden_by text;
