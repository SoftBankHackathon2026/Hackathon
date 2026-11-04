-- AWS(RDS) 를 귀환 CDC 원본으로 쓰기 위한 복제 권한. RDS 마스터(oneaction_admin)로 migration_demo 에 접속해 실행한다.
-- 재실행해도 안전하다. 비밀번호·파라미터 변경은 여기서 하지 않는다.
--
-- 왜 init_db 대신 이 파일인가:
--   init_db 는 "job.source 이면 ALTER ROLE ... REPLICATION" 을 실행한다. 귀환에서는 AWS 가 job.source 라
--   RDS 에서 이 문장이 거부된다 (RDS 마스터는 REPLICATION 속성을 줄 수 없다). 그래서 귀환에서는 AWS 에
--   init_db 를 다시 돌리지 않고, 정방향에서 이미 만든 스키마·역할 위에 rds_replication 만 더한다.
--
-- 전제: RDS 파라미터 그룹에 rds.logical_replication=1 이 적용되고 재부팅까지 끝나 wal_level=logical 이어야 한다.
--       이 변경은 oneaction-infra database 스택 담당과 협의해 반영한다.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication') THEN
        RAISE EXCEPTION 'rds_replication 역할이 없다. RDS PostgreSQL 이 맞는지 확인한다';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_cdc') THEN
        RAISE EXCEPTION 'migration_cdc 역할이 없다. 정방향 init_db --target aws 가 끝났는지 확인한다';
    END IF;
    IF NOT pg_has_role('migration_cdc', 'rds_replication', 'MEMBER') THEN
        GRANT rds_replication TO migration_cdc;
        RAISE NOTICE 'migration_cdc 에 rds_replication 부여';
    ELSE
        RAISE NOTICE 'migration_cdc 는 이미 rds_replication 멤버';
    END IF;
END
$$;

-- 스냅샷용 SELECT (정방향 001_demo.sql 에서 이미 부여됐지만 확인 차원에서 재부여, 멱등)
GRANT SELECT ON public.migration_demo TO migration_cdc;

-- 확인용 출력
SELECT name, setting FROM pg_settings
 WHERE name IN ('wal_level', 'rds.logical_replication', 'max_replication_slots', 'max_wal_senders');
SELECT pg_has_role('migration_cdc', 'rds_replication', 'MEMBER') AS cdc_has_rds_replication;
