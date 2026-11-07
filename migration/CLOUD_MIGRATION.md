# PostgreSQL 앱의 클라우드↔클라우드 전환

`python -m migration.cloud`는 `phase`, `request_id`, INSERT 데모 워커 없이
명시한 여러 PostgreSQL 테이블을 옮긴다. AWS→GCP, GCP→AWS, 다른 리전 등은
접속 설정으로 정한다. 기존 `migration.cutover` 데모 명령은 그대로 유지된다.
**앱 이미지·Kubernetes·네트워크·스키마를 자동 배포하는 도구는 아니다.**
앱 운영 플랫폼이 아래 제어 API를 구현하고 대상 앱/DB를 준비해야 한다.

## 실행

`config.cloud.example.yaml`을 복사하고 접속 정보와 앱 소유 테이블 전체를 명시한다.
비밀번호는 해당 환경변수로 주입한다. Connect에도 source CDC 및 target sink의
환경변수를 Secret으로 전달하고 `EnvVarConfigProvider`를 켠다.
TLS CA는 Python/libpq와 Connect/JDBC 양쪽에 설치한다. 제어 API와 Connect는
인증된 내부 네트워크/프록시로 보호한다. 이 CLI는 외부 공개 제어 API용이 아니다.

```sh
python -m migration.cloud --config cloud.yaml --state-dir /var/lib/migration/shop plan
python -m migration.cloud --config cloud.yaml --state-dir /var/lib/migration/shop run
# READY에서 종료 코드 3: 복사 완료, 원본 앱은 계속 서비스 중
python -m migration.cloud --config cloud.yaml --state-dir /var/lib/migration/shop run --approve CHANGE-123
python -m migration.cloud --config cloud.yaml --state-dir /var/lib/migration/shop status
```

설정과 상태 볼륨을 보존한다. 같은 job_id로 설정/테이블/방향을 바꾸면 거절한다.
로컬 파일 잠금과 DB 테이블별 advisory lock으로 중복 실행을 거절한다.
반대 방향은 새로운 job_id, 새로운 빈 대상 DB, 반대 source/target 설정으로 실행한다.
기존 원본을 덮어쓰는 즉석 귀환/롤백은 제공하지 않는다.

## 전환 과정과 완료 증거

1. PRECHECK: 동일 DB·스키마/PK 차이·지원하지 않는 스키마를 거절한다.
   대상이 비어 있음을 테이블 잠금 안에서 검사하고 대상 앱 쓰기를 차단한다.
2. COPYING: 다중 테이블 publication, Debezium source, 테이블별 JDBC sink를 구성한다.
   기본키 기반 upsert와 DELETE를 사용한다. Source offset의 snapshot 완료/streaming LSN을
   확인하며 빈 테이블도 지원한다. Connect가 RUNNING이라는 이유나 행 수만으로 통과하지 않는다.
3. READY: 명시적 승인 ID를 기다린다. 이 단계는 sink가 최종 동기화됐다는 선언이 아니다.
4. FENCING: 앱 pause/drain 확인 후 모든 지정 원본 테이블을 한 트랜잭션에서 잠그고
   영속 ALWAYS 트리거를 설치한다. 이전 쓰기 트랜잭션이 끝나야 설치되며 lock timeout이면 중단한다.
5. DRAINING: 양쪽 앱 쓰기가 차단된 상태에서 기본키별 **모든 열**을 비교한다.
   누락·추가·변경이 모두 0이 될 때까지 기다린다. 비교는 디스크 SQLite를 사용해 메모리 사용을 제한한다.
6. STOPPING: 모든 Connect task가 STOPPED/없음이 된 뒤 다시 전체 비교한다.
   serial/identity BY DEFAULT 소유 시퀀스의 다음 값도 맞추고 커넥터를 삭제한다.
7. SWITCHING: 앱이 PAUSED인 채 접속 대상을 바꾼 것을 확인하고 대상 차단을 해제한다.
   resume 후 RUNNING/대상/health를 확인한다. 원본 차단은 유지한다.

전환 후 앱은 UPDATE/DELETE할 수 있어 이전 원본과의 전체 일치를 요구하지 않는다.
COMPLETED의 데이터 근거는 **쓰기 개방 직전 전체 비교**, 운영 근거는 앱 health 응답이다.
사용자 요청의 end-to-end 성공·무중단·지연 보장은 별도 앱 인수시험으로 확인해야 한다.
보고서/상태에는 고객 행 값이나 기본키 값을 남기지 않는다. 임시 비교 데이터는 실행 중
0700 임시 디렉터리에만 존재하며 종료 시 지운다. 실행 호스트의 디스크 보호가 필요하다.

## DB 쓰기 차단

각 job 전용 `_mg_<job_id>` 스키마의 트리거 함수를 사용한다.
지정한 모든 테이블에 STATEMENT 및 ROW 트리거를 설치하므로 INSERT/UPDATE/DELETE,
빈 결과의 쓰기, TRUNCATE, 다른 계정의 쓰기와 FK cascade를 차단한다.
대상에서는 전용 sink 로그인만 허용한다. 원본에서는 모든 일반 쓰기를 거절한다.
앱 계정별 REVOKE/GRANT를 하지 않으므로 원래 ACL을 잃거나 권한을 확대하지 않는다.
프로세스가 죽어도 차단은 DB에 남는다. 권한 보유자가 트리거를 제거하는 DDL이나
관리자에 의한 우회는 지원 계약 밖이다. **마이그레이션 중 DDL·관리자 쓰기·시퀀스 직접 변경은 금지한다.**

## 앱 제어 API 계약

기존 데모 워커를 자동으로 일반 앱 제어기로 간주하지 않는다. 플랫폼은 아래 API를
앱별로 연결해야 한다. 모든 호출은 재실행해도 안전해야 하고 여러 앱 replica·배치·cron·
다른 쓰기 계정을 포함한 **전체 writer**를 제어해야 한다.

| 요청 | 필수 응답/동작 |
|---|---|
| `GET /status` | `state: RUNNING 또는 PAUSED`, `target: 설정 대상 이름`, `known_targets: [원본, 대상]`, `in_flight: 정수` |
| `POST /pause` | 신규 쓰기 중지 요청. 자동 resume 금지 |
| `POST /drain {"timeout":120}` | 진행 중 요청 정리 후 `settled:true`; `/status`도 `PAUSED,in_flight:0` |
| `POST /target {"target":"gcp"}` | PAUSED 유지하며 실제 DB 연결/앱 라우팅 변경. 대상 쓰기·시험 행 삽입 금지 |
| `POST /resume` | 새 대상에서 앱 서비스 재개 |
| `GET /healthz` | 대상 앱/DB 점검 후 `healthy:true,target:"gcp"` |

HTTP 접속 실패, 모호한 응답, DB 접근 실패는 검증 성공으로 보지 않는다.

## 지원 범위와 사전 준비

- PostgreSQL→PostgreSQL, Debezium PostgreSQL/JDBC 3.x, Connect STOPPED API(3.5+)와
  offset 조회 API(3.6+)가 필요하다. 다른 DB 엔진은 지원하지 않는다.
- 사전 생성한 동일 열·타입·PK의 일반 테이블. 소문자 `schema.table` 이름과 복합 PK 지원.
- 주요 scalar: integer, numeric, float, bool, text/varchar, date/time/timestamp/timestamptz,
  UUID, JSON/JSONB, bytea. timestamp 정밀도와 numeric을 보존하는 커넥터 설정을 사용한다.
- 파티션/상속, RLS, 배열·사용자 타입, generated/identity ALWAYS는 사전 거절한다.
- 테이블별 sink의 순서가 달라 **대상 FK와 사용자 트리거는 사전 거절**한다.
  필요한 앱은 별도 승인된 스키마 준비/제약조건 검증·복구 단계를 먼저 설계해야 한다.
- 소유 시퀀스는 increment=1·noncycling만 지원. 시퀀스 standalone 사용, 공유 시퀀스,
  large object, 스키마 DDL·확장·함수의 자동 이전은 범위 밖이다.
- COPY 중 원본 TRUNCATE는 지원하지 않는다. 수행하면 최종 데이터 검증에서 차이가
  검출될 수 있으므로 앱 차단 외에 운영자가 DDL/TRUNCATE를 동결해야 한다.
- 앱별 테이블 목록의 완전성은 운영자가 보장한다. 목록 밖 DB/객체·외부 저장소·파일은 옮기지 않는다.
- 관리 역할은 대상 테이블 소유자 또는 해당 DDL 권한을 가져야 한다. 원본 CDC는
  logical replication·SELECT·publication 소유/CREATE 권한, 대상 sink는 지정 테이블 DML,
  verify는 양쪽 모든 행의 SELECT, 앱은 DDL을 못 하는 전용 로그인으로 준비한다.
  RDS/Cloud SQL의 복제 허용 플래그·관리 권한과 네트워크는 별도 준비 대상이다.

## 실패와 복구

- FAILED: 원인과 현재 fence/커넥터/앱 상태를 확인한 뒤 동일 설정 `run --retry`.
  자동으로 원본을 열거나 앱을 재개하지 않는다.
- STOPPING 이후 불일치: 일부 task가 이미 멈췄을 수 있다. 단순 재시도로 성공을
  강제하지 않고 운영자가 복제/상태를 조사한다.
- SWITCHING 시작 이후 오류: NEEDS_RECOVERY. 대상에 쓰기가 있었는지 불명확하므로
  자동 재시도/롤백을 금지한다. 유일 writer를 사람이 결정하고 복구한다.
- 원본 slot/publication은 복구 창 동안 유지한다. WAL 누적을 모니터링하고 복구 창이
  끝나면 해당 job 자원만 정리한다. 원본 fence는 authoritative DB 결정 전 해제하지 않는다.

## 검증

```sh
cd migration
python -m pytest tests/test_cloud.py tests/test_config.py tests/test_cutover.py tests/test_cdc.py tests/test_verify.py
# 격리 trust-auth PostgreSQL 컨테이너에만 지정 (운영 DB 금지)
CLOUD_TEST_PORT=<localhost-port> python -m pytest tests/test_cloud_pg.py
```

공식 동작 근거:
[Debezium JDBC native envelope / delete](https://debezium.io/documentation/reference/stable/connectors/jdbc.html),
[PostgreSQL snapshot](https://debezium.io/documentation/reference/stable/connectors/postgresql.html),
[PostgreSQL triggers](https://www.postgresql.org/docs/current/sql-createtrigger.html).

실제 CDC 시험 재현 (Docker 필요, 두 DB 및 역할을 임시 생성/삭제):

```sh
cd migration
docker compose -p ghost-cloud-test -f tests/cloud-compose.yaml up -d --build --wait
export CLOUD_TEST_PORT=$(docker compose -p ghost-cloud-test -f tests/cloud-compose.yaml port cloud-pg 5432 | cut -d: -f2)
export CLOUD_CONNECT_URL=http://$(docker compose -p ghost-cloud-test -f tests/cloud-compose.yaml port connect 8083)
python -m pytest tests/test_cloud_pg.py tests/test_cloud_cdc.py
# 시험 전용 프로젝트의 리소스만 정리
docker compose -p ghost-cloud-test -f tests/cloud-compose.yaml down -v
```

2026-11-07 로컬 실행: core/회귀 118 PASS, PostgreSQL 17 실DB 4 PASS,
Kafka 3.9.1 + Debezium PostgreSQL/JDBC 3.7.0.Final(Connect 4.3.1) 실제 CDC 1 PASS.
CDC 시험은 시험용 앱 어댑터를 주입했고 실제 대상 DB에서 새 쓰기를 실행했다.
운영 HTTP 어댑터·AWS/GCP TLS/권한/네트워크·사용자 직접 인수는 아직 검증하지 않았다.
