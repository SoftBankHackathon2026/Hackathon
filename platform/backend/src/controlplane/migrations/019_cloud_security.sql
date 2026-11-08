-- 클라우드 API 로 읽은 보안 실측(환경마다 마지막 결과 하나). 수집기가 주기마다 덮어쓴다.
CREATE TABLE IF NOT EXISTS fleet_cloud_security (
 env_id text PRIMARY KEY,
 facts jsonb NOT NULL,
 observed_at timestamptz NOT NULL
);
