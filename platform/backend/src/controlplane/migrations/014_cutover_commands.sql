CREATE TABLE IF NOT EXISTS fleet_cutover_commands (
 id text PRIMARY KEY,
 job_id text NOT NULL,
 action text NOT NULL,
 approval_id text,
 note text,
 requested_by text,
 requested_at timestamptz NOT NULL,
 status text NOT NULL,
 started_at timestamptz,
 finished_at timestamptz,
 exit_code integer,
 stage text,
 log_tail text
);
CREATE INDEX IF NOT EXISTS fleet_cutover_commands_job
 ON fleet_cutover_commands(job_id, requested_at DESC);
-- 작업당 대기·실행 중 명령은 하나만: 동시 요청의 두 번째 방어선(첫 번째는 서비스의 선점 검사).
CREATE UNIQUE INDEX IF NOT EXISTS fleet_cutover_commands_active
 ON fleet_cutover_commands(job_id) WHERE status IN ('pending', 'running');
