-- Team roles: owner (manages members, deploys), member (deploys), viewer (read only).
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS role text;
-- Users from before roles: the earliest user of each team becomes its owner, the rest
-- keep deploying as members. Only rows without a role are touched, so reruns change nothing.
UPDATE platform_users u SET role = CASE
  WHEN u.id = (SELECT f.id FROM platform_users f WHERE f.team = u.team ORDER BY f.created_at, f.id LIMIT 1)
   AND NOT EXISTS (SELECT 1 FROM platform_users o WHERE o.team = u.team AND o.role = 'owner')
  THEN 'owner' ELSE 'member' END
 WHERE u.role IS NULL;
INSERT INTO schema_migrations(version) VALUES(6) ON CONFLICT DO NOTHING;
