-- migration_demo 데이터베이스 초기 스키마와 역할.
-- 이 파일은 migration_demo 데이터베이스에 접속한 뒤 실행한다 (CREATE DATABASE는 init_db.py가 수행).
-- 모든 문장은 재실행해도 안전하다. 비밀번호는 init_db.py가 환경변수로 받아 따로 설정한다.

-- 1. 데모 테이블 (01-DB-이전-계획.md "데이터 예시"의 DDL 그대로)
CREATE TABLE IF NOT EXISTS public.migration_demo (
    request_id uuid PRIMARY KEY,
    run_id text NOT NULL,
    phase text NOT NULL,
    request_no bigint NOT NULL,
    payload text NOT NULL,
    created_at timestamptz NOT NULL
);

-- 2. 역할 4개. 비밀번호는 여기서 설정하지 않는다 (NOLOGIN 상태로 만든 뒤 init_db.py가 LOGIN + PASSWORD 설정).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_app') THEN
        CREATE ROLE migration_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_cdc') THEN
        CREATE ROLE migration_cdc NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_sink') THEN
        CREATE ROLE migration_sink NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_verify') THEN
        CREATE ROLE migration_verify NOLOGIN;
    END IF;
END
$$;

-- 3. 최소 권한. 앱 writer는 객체 소유자가 아니며 INSERT/SELECT만 가진다.
--    테이블 소유자는 이 스크립트를 실행한 관리자다.
REVOKE ALL ON public.migration_demo FROM PUBLIC;

-- GRANT CONNECT ON DATABASE 는 DB 이름이 설정에 따라 달라지므로 init_db.py 가 수행한다.
GRANT USAGE ON SCHEMA public TO migration_app, migration_cdc, migration_sink, migration_verify;

-- 앱 writer: INSERT/SELECT만. 전환 시 cutover.py가 INSERT를 REVOKE/GRANT 한다.
GRANT SELECT, INSERT ON public.migration_demo TO migration_app;

-- CDC reader: 스냅샷용 SELECT. REPLICATION 속성은 init_db.py가 대상별로 부여한다.
GRANT SELECT ON public.migration_demo TO migration_cdc;

-- sink writer: upsert 수행. DELETE는 데모 범위 밖이므로 주지 않는다.
GRANT SELECT, INSERT, UPDATE ON public.migration_demo TO migration_sink;

-- 검증 계정: 읽기 전용.
GRANT SELECT ON public.migration_demo TO migration_verify;
