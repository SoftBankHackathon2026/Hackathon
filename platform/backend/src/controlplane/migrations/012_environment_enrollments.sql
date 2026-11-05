-- 마이그레이션 준비 1단계(환경 연결). 사용자가 자기 셸에서 등록 스크립트를 실행하면 읽기 전용 접근 정보와
-- 환경 정보가 들어온다. access 는 수집기용 자격증명(kubeconfig, 역할 ARN)이라 API 로 내보내지 않는다.
CREATE TABLE IF NOT EXISTS environment_enrollments (
 id text PRIMARY KEY,
 kind text NOT NULL CHECK (kind IN ('kubernetes', 'aws')),
 status text NOT NULL CHECK (status IN ('pending', 'registered')),
 token_hash text NOT NULL,
 external_id text,
 team text,
 name text,
 inventory jsonb,
 access jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL,
 registered_at timestamptz
);
CREATE INDEX IF NOT EXISTS environment_enrollments_team ON environment_enrollments(team, created_at DESC);
