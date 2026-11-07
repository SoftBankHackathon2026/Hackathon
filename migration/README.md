# migration/ — 온프렘 PostgreSQL → AWS PostgreSQL 이전 데모 도구

`업무-아젠다/01-DB-이전-계획.md` 의 "구성과 INSERT 워커", "온프렘에서 AWS로", "전환과 복구", "데이터 검증" 절을 그대로 코드로 옮긴 것이다.
INSERT 워커, Debezium CDC 제어, 전환 상태 머신, B/T/S 집합 검증을 한 디렉터리에 모았다.

Keycloak 인증 기반의 탐색·점검·호스트 설치는 독립 명령
`python -m migration.keycloak_infra`로 제공한다.
[사용법과 플랫폼 입출력 규약](keycloak-infra/README.md)을 참고한다.
[주요 구현 디렉토리의 설치 흐름·구성도·수집 정보 안내](migration/keycloak_infra/README.md)에서
기존 환경 조사 방식과 신규 설치 후 반환 정보의 범위를 확인할 수 있다.
이 명령은 계정·IAM 정책 이관이나 기존 데이터베이스 전환을 실행하지 않는다.

> **주의: 이 코드는 아직 실제 클러스터(온프렘 k3s, 기존 Postgres, RDS, Kafka Connect)에서 실행·검증하지 않았다.**
> 로컬에서는 pytest 단위 테스트와 `docker run postgres:16-alpine` 통합 테스트만 통과했다. Debezium/Kafka Connect 와의 결합은 미검증이다.

## 구성

```
migration/
  README.md                 이 문서
  pyproject.toml            패키지 `migration`, 의존성(psycopg[binary], PyYAML), pytest 설정
  Dockerfile                ghcr.io/softbankhackathon2026/migration-tools (python:3.12-slim, 비루트)
  config.example.yaml       targets(onprem/aws)·job·kafka·worker·cutover 설정 예시. 비밀번호 없음
  sql/001_demo.sql          migration_demo 테이블 DDL, 역할 4개, 테이블 GRANT (멱등)
  connectors/forward-source.json.tmpl   Debezium PostgreSQL Source 템플릿
  connectors/forward-sink.json.tmpl     Debezium JDBC Sink 템플릿
  migration/config.py       YAML + 환경변수 로딩, 접속 사양 조립 (비밀번호는 repr/log 제외)
  migration/db.py           psycopg 3 연결 헬퍼 (sslmode 기본 require, 타임아웃, 서버 식별)
  migration/state.py        SQLite 영속 상태 (요청 기록, 전환 단계·체크포인트)
  migration/init_db.py      DB 생성 → SQL 적용 → 역할 LOGIN/비밀번호 → 복제 권한
  migration/seed.py         seed 1,000건 결정적 삽입 + 검증
  migration/loadgen.py      INSERT 워커 + HTTP 제어 API
  migration/cdc.py          Kafka Connect REST 클라이언트, publication/slot 준비, 템플릿 렌더링
  migration/cutover.py      전환 상태 머신
  migration/verify.py       B/T/S 집합 비교 보고서
  migration/worker_client.py 워커 HTTP API 클라이언트 (cutover 가 사용)
  tests/                    pytest (단위 + MIGRATION_TEST_DSN 있을 때 통합)
```

`migration/connect/` 는 이 디렉터리에 포함되지 않는다 (Kafka Connect 이미지·CI 는 별도 작업).

## 환경변수

비밀번호는 **환경변수로만** 전달한다. 설정 파일·로그·보고서·예외 메시지에 적히지 않는다 (`config.redact` 가 오류 문자열에서도 제거).

| 변수 | 용도 | 사용하는 명령 |
|---|---|---|
| `MIGRATION_ADMIN_PASSWORD` | 온프렘 관리자(superuser) 비밀번호 | init_db, cdc prepare/drop-slot, cutover |
| `MIGRATION_AWS_ADMIN_PASSWORD` | RDS 마스터 비밀번호 (config.example.yaml 의 aws.users.admin.password_env) | init_db --target aws, cutover |
| `MIGRATION_APP_PASSWORD` | `migration_app` (앱 writer: INSERT/SELECT) | loadgen, seed, cutover 시험 INSERT |
| `MIGRATION_CDC_PASSWORD` | `migration_cdc` (REPLICATION + SELECT) | init_db 가 설정. Connect 는 FileConfigProvider 로 읽음 |
| `MIGRATION_SINK_PASSWORD` | `migration_sink` (대상 upsert) | init_db 가 설정. Connect 가 읽음 |
| `MIGRATION_VERIFY_PASSWORD` | `migration_verify` (읽기 전용) | verify, seed 검증 |
| `MIGRATION_CONFIG` | 설정 파일 경로 (기본 `config.yaml`) | 모든 명령 (`--config` 로도 지정) |
| `MIGRATION_HOME` | `sql/`, `connectors/` 가 있는 루트 (이미지에서는 `/app`) | init_db, cdc, cutover |
| `WORKER_STATE_DIR` | SQLite 상태 디렉터리 (기본 `/var/lib/worker`) | loadgen, seed, verify, cutover |
| `WORKER_TARGET`, `WORKER_PHASE`, `WORKER_RUN_ID`, `WORKER_RATE`, `WORKER_PORT`, `WORKER_MAX_QUEUE`, `WORKER_MAX_QUEUE_AGE`, `WORKER_MAX_CONSECUTIVE_FAILURES` | 워커 CLI 기본값 | loadgen |
| `MIGRATION_TEST_DSN` | 통합 테스트용 관리자 DSN (없으면 통합 테스트 skip) | pytest |
| `LOG_LEVEL` | 로그 레벨 (기본 INFO) | 모든 명령 |

Kafka Connect 쪽 비밀번호는 템플릿에서 `${env:MIGRATION_CDC_PASSWORD}` 형식(EnvVarConfigProvider)으로 참조한다. Connect 워커에 `config.providers=env` 와 `MIGRATION_*_PASSWORD` 환경변수가 있어야 하며 gitops `apps/migration-cdc` 가 이를 설정한다.
Connect 워커에 `config.providers=file`, `config.providers.file.class=org.apache.kafka.common.config.provider.FileConfigProvider` 가 설정되어 있고, 해당 파일이 Secret 으로 마운트돼야 한다 (`migration/connect/` 담당).

## 설정 파일

`config.example.yaml` 을 복사해 `config.yaml` 로 쓴다. 핵심 항목:

- `targets.onprem` / `targets.aws`: host, port, dbname(`migration_demo`), sslmode(기본 `require`), admin_dbname, users(역할별 user + password_env).
- `job`: `job_id`(예 `forward_001`), `source`, `target`, `table`, 전환 전후 phase 이름.
  이름 규칙은 코드가 만든다: slot `forward_001`, publication `forward_001_pub`, topic.prefix `forward_001`,
  데이터 토픽 `forward_001.public.migration_demo`, 커넥터 `forward_001-source` / `forward_001-sink`.
- `kafka.connect_url`, `kafka.secrets_file`, `worker.url`, `cutover.*` (대기 조건·시간 제한).

## 명령

모든 명령은 `python -m migration.<모듈>` 이며, 컨테이너의 ENTRYPOINT 가 `python -m` 이므로 `docker run <image> migration.loadgen ...` 처럼 쓴다.
인자 없이 `python -m migration` 을 실행하면 명령 목록을 출력한다.

### init_db — DB·스키마·역할 초기화 (재실행 안전)

```
python -m migration.init_db --config config.yaml --target onprem
python -m migration.init_db --config config.yaml --target aws
```

1. 관리자로 `admin_dbname`(postgres) 에 접속해 `migration_demo` 가 없으면 `CREATE DATABASE`.
   보호 목록(`postgres`, `template*`, `oneaction_review`)은 거부한다. 다른 DB 는 건드리지 않는다.
2. `sql/001_demo.sql` 적용: 테이블, 역할 4개(NOLOGIN 으로 생성), 테이블 GRANT.
3. `GRANT CONNECT ON DATABASE` (DB 이름이 설정에 따라 달라 Python 에서 수행).
4. 역할 4개에 `LOGIN` + 환경변수 비밀번호 설정.
5. 복제 권한: `--target` 이 `job.source` 면 `ALTER ROLE migration_cdc REPLICATION`, 아니면(RDS) `rds_replication` 역할이 있을 때만 GRANT.
   aws 에서는 publication 을 만들지 않는다.
6. 앱 계정 INSERT: `--app-write auto`(기본)는 `job.source` 에서만 허용하고 대상에서는 `REVOKE INSERT` 한다 (대상은 sink 만 쓰기, 전환 시 cutover 가 GRANT).
   `allow` / `deny` 로 강제할 수 있다.
7. `wal_level`, `ssl` 등 서버 설정과 역할별 테이블 권한을 로그로 남긴다.

### seed — 초기 1,000건

```
python -m migration.seed --config config.yaml --target onprem --state-dir /var/lib/worker
```

`request_id = uuid5(고정 namespace, "seed-{n}")`, `request_no 1..1000`, `phase=seed`, `created_at = 2026-01-01T00:00:00Z + n초`.
`ON CONFLICT DO NOTHING` 으로 넣고 phase=seed 행이 정확히 1,000건이며 모든 필드가 기대와 같은지 확인한다.
`--state-dir` 를 주면 요청 기록에도 committed 로 남겨 verify 의 S 집합에 포함된다.

### loadgen — INSERT 워커

```
python -m migration.loadgen --config config.yaml --target onprem --rate 1 --state-dir /var/lib/worker --port 8080
```

동작(01 문서 "INSERT 워커 동작" 5단계):

1. 요청 생성 → SQLite `requests.sqlite3` 에 `pending` 으로 먼저 기록 (request_id=uuid4, payload, created_at 고정).
2. 활성 DB 에 `INSERT ... ON CONFLICT (request_id) DO NOTHING RETURNING request_id`.
3. 충돌이면 기존 행의 run_id/phase/request_no/payload/created_at 을 비교 → 같으면 `committed`, 다르면 `mismatch` 기록.
4. 커밋 성공 또는 동일 요청의 기존 커밋 확인 시에만 완료. 커밋 확인 시각·지연(생성→커밋)을 기록.
5. 연결 오류·응답 유실이면 같은 request_id 로 먼저 조회한 뒤 재시도. 재시도 횟수·마지막 오류를 기록. 지수 backoff(최대 30초).

상태: `RUNNING` / `PAUSED`(새 요청은 대기열+SQLite 에만, DB 전송 중지) / `SWITCHING` / `ERROR`(연속 실패 상한 초과, 반대 DB 에 쓰지 않고 중단).
재시작 시 SQLite 의 pending 요청을 같은 ID 로 이어서 처리하고 request_no 도 이어진다.
대기열 상한(건수 `--max-queue`, 최장 대기 `--max-queue-age`)을 넘으면 요청을 `queue_rejected` 로 기록한다. **대기열 저장은 DB 쓰기 성공이 아니다.**
`state_dir/worker.json` 에 현재 target/phase 를 저장하며, 재시작 시 **저장된 target 이 CLI `--target` 보다 우선**한다 (전환 뒤 재시작이 옛 DB 로 돌아가는 사고 방지).

HTTP 제어 API (포트 8080):

| 메서드/경로 | 동작 | 응답 |
|---|---|---|
| `GET /healthz` | 생존 확인 | `{"ok": true, "state": ...}` |
| `GET /status` | 상태·target·phase·카운터·대기열 길이·진행 중 요청·지연 p50/p95/max·target 별 커밋 수·마지막 전환 정보 | JSON |
| `GET /metrics` | Prometheus 텍스트 (`migration_worker_state`, `migration_worker_queue_length`, `migration_worker_requests_total{kind}`, `migration_worker_latency_seconds{quantile}` 등) | text |
| `POST /pause` | PAUSED 로 전환. 진행 중 요청은 끝까지 판정 | status JSON |
| `POST /resume` | RUNNING 으로 전환 (ERROR 에서도 가능) | status JSON |
| `POST /drain` body `{"timeout": 60}` | 진행 중 요청이 확정될 때까지 대기. 대기열은 비우지 않음 | `{"settled", "in_flight", "queue_length", "last_committed"}` (미확정이면 409) |
| `POST /target` body `{"target": "aws", "phase": "aws_live"}` | 기존 연결을 닫고 새 target 에 연결·식별 확인. **PAUSED 이고 진행 중 요청이 없을 때만** 허용, 실패 시 target 유지 | status JSON (거부 시 409) |

### cdc — Kafka Connect 제어

```
python -m migration.cdc --config config.yaml prepare          # 원본에 publication forward_001_pub + slot forward_001 (관리자, 멱등)
python -m migration.cdc --config config.yaml render           # 템플릿 렌더링 결과 확인 (비밀번호 가림)
python -m migration.cdc --config config.yaml apply            # Source/Sink 등록 또는 갱신 (PUT /connectors/<name>/config)
python -m migration.cdc --config config.yaml status           # 상태 + /offsets
python -m migration.cdc --config config.yaml pause|stop|delete [--which source|sink|both]
python -m migration.cdc --config config.yaml drop-slot        # 커넥터 삭제 후 비활성 슬롯 제거 (WAL 축적 방지)
```

템플릿 자리표시자는 `{{job_id}}`, `{{source_host}}`, `{{target_sink_user}}` 등이며 `${env:...}` 는 그대로 Connect 에 전달된다.
Source: `pgoutput`, `snapshot.mode=initial`, `publication.autocreate.mode=disabled`, `slot.drop.on.stop=false`, JSON converter(schema 포함), `errors.tolerance=none`.
Sink: `insert.mode=upsert`, `primary.key.mode=record_key`, `primary.key.fields=request_id`, `schema.evolution=none`, `delete.enabled=false`, `tasks.max=1`, `ExtractNewRecordState` 변환.

### cutover — 전환 상태 머신

```
python -m migration.cutover --config config.yaml --state-dir /var/lib/worker run --dry-run
python -m migration.cutover --config config.yaml --state-dir /var/lib/worker run              # READY 에서 멈춤 (종료 코드 3)
python -m migration.cutover --config config.yaml --state-dir /var/lib/worker run --approve <승인ID>
python -m migration.cutover --config config.yaml --state-dir /var/lib/worker status
python -m migration.cutover --config config.yaml --state-dir /var/lib/worker run --retry       # ABORTED 를 실패 단계부터 재개
python -m migration.cutover --config config.yaml --state-dir /var/lib/worker abort             # ABORTED(대상 쓰기 전) 원복
```

상태 전이 `PRECHECK → COPYING → CATCHING_UP → READY → FENCING → DRAINING → SYNC_CONFIRMED → SWITCHING → VERIFYING → COMPLETED`.
단계·체크포인트는 `cutover.sqlite3` 에 저장되고, 외부 작업(커넥터 등록·삭제, REVOKE/GRANT, 워커 pause/drain/target/resume)은 `cutover_steps` 에 기록되어 같은 job_id 재실행 시 반복하지 않는다.

| 단계 | 하는 일 (01 문서 11단계 대응) | 실패 시 |
|---|---|---|
| PRECHECK | 양쪽 테이블·권한(원본 앱 INSERT 가능, 대상 앱 INSERT 불가)·wal_level·CDC 계정 REPLICATION·워커 target·Connect 접근 (1) | ABORTED |
| COPYING | publication/slot 준비, Source/Sink 등록, 커넥터 RUNNING + 대상 seed 건수 ≥ 원본 seed 건수 (3) | ABORTED |
| CATCHING_UP | 원본에 snapshot 이후 `min_live_rows_after_snapshot`(100)건 추가, 최신 10건이 대상에 반영 (4) | ABORTED |
| READY | `--approve` 없으면 종료 코드 3 으로 멈춤 | - |
| FENCING | 워커 `/pause` → `/drain` → 원본 `REVOKE INSERT` → 앱 계정 세션만 `pg_terminate_backend` → 앱 계정으로 시험 INSERT 가 거절되는지 실제 확인 (5, 6) | ABORTED |
| DRAINING | 마지막 커밋 요청 ID 가 대상에 있고 원본·대상 건수가 같고 sink `/offsets` 가 `offset_stable_seconds` 동안 변하지 않음 (7) | ABORTED |
| SYNC_CONFIRMED | verify(pre) 통과 → 커넥터 설정·상태·offset 증거 저장 → Source, Sink 삭제 → 대상 앱 쓰기가 아직 닫혀 있는지 확인 → verify(pre) 재실행 (7, 8) | ABORTED |
| SWITCHING | 대상 `GRANT INSERT` → 워커 `/target {"target":"aws","phase":"aws_live"}` → 접속 DB 이름 확인 → `/resume` (9) | **NEEDS_RECOVERY** |
| VERIFYING | 대상에 `min_rows_after_switch`(100)건 신규 커밋, mismatch 0, 원본 앱 쓰기 여전히 차단, verify(post) 통과 (10) | NEEDS_RECOVERY |
| COMPLETED | 체크포인트에 승인 ID·마지막 요청·offset·검증 결과 보관 (11) | - |

- `ABORTED`: 대상에 앱 쓰기를 열기 전 실패. `run --retry` 로 실패 단계부터 재개하거나 `abort` 로 원복(커넥터 삭제, 원본 `GRANT INSERT`, 워커 `/resume`; 워커 target 이 원본일 때만).
- `NEEDS_RECOVERY`: 대상에 앱 쓰기를 연 뒤 실패. 자동 원복하지 않는다. 사람이 authoritative DB 를 확정한 뒤 `run --force-retry`.
- Kafka offset `H+1` 비교: Connect `/connectors/<sink>/offsets` 로 sink 소비 위치를 읽어 정체 여부와 값을 기록한다.
  데이터 토픽의 end offset 은 Connect REST 로 알 수 없어 **H 자체는 계산하지 않고** DB 커밋 결과(마지막 요청 ID 존재·전체 비교)로 판정한다.
  `/offsets` 미지원(Connect < 3.6)이면 체크포인트 `offset_note` 와 로그에 fallback 을 명시한다.
- 종료 코드: 0 완료, 1 실패, 2 설정 오류, 3 승인 대기.

### verify — B/T/S 비교

```
python -m migration.verify --config config.yaml --state-dir /var/lib/worker --mode pre  --report reports/pre.json
python -m migration.verify --config config.yaml --state-dir /var/lib/worker --mode post --report reports/final.json
```

원본 B, 대상 T 의 **전체 행**을 `migration_verify` 로 읽어 request_id 집합과 모든 필드(timestamptz 는 UTC 로 정규화)를 비교하고,
SQLite 요청 기록의 committed 집합 S 와 대조한다. `B-T`, `T-B`, 필드 불일치, `S-T` 각 건수와 샘플 ID(최대 20)를 JSON 으로 남기며 모두 0 일 때만 종료 코드 0.
`--mode post` 에서는 대상에서 커밋 확인된 요청(S 중 target=aws)만 `T-B` 에서 제외한다. 건수·checksum 만으로 통과시키지 않는다.

## 실행 절차 (온프렘 → AWS)

```
# 0) 비밀번호 환경변수 주입 (Secret), config.yaml 준비
# 1) DB 준비
python -m migration.init_db --target onprem
python -m migration.init_db --target aws
python -m migration.seed --target onprem --state-dir /var/lib/worker
# 2) 워커 시작 (k3s Deployment, 상태 디렉터리는 PVC)
python -m migration.loadgen --target onprem --rate 1
# 3~4) 복사·지속 복제, 5~8) 쓰기 차단·최종 반영·CDC 종료, 9~10) 전환·재개 — 전부 cutover 가 수행
python -m migration.cutover run                   # READY 까지 진행 후 멈춤
python -m migration.cutover run --approve <ID>    # FENCING 부터 COMPLETED 까지
# 11) 증거
python -m migration.cutover status
python -m migration.verify --mode post --report reports/final.json
python -m migration.cdc drop-slot                 # 귀환 실험 전까지 슬롯을 남길지 팀 결정
```

## 로컬 테스트

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest                                  # 단위 테스트 (네트워크 불필요)
docker run -d --name mig-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16-alpine \
  -c wal_level=logical -c max_replication_slots=4 -c max_wal_senders=4
MIGRATION_TEST_DSN="postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable" python -m pytest
python -m compileall migration
```

통합 테스트는 DSN 의 인스턴스 안에 임시 DB 두 개(`mig_src_*`, `mig_tgt_*`)를 만들고 끝나면 삭제한다. Kafka Connect 는 로컬 가짜 HTTP 서버로만 검증했다.

## 이미지

```
docker build -t ghcr.io/softbankhackathon2026/migration-tools:dev migration/
docker run --rm ghcr.io/softbankhackathon2026/migration-tools:dev            # 명령 목록
docker run --rm -e MIGRATION_APP_PASSWORD=... -v $PWD/config.yaml:/app/config.yaml \
  ghcr.io/softbankhackathon2026/migration-tools:dev migration.loadgen --config /app/config.yaml --target onprem
```

ENTRYPOINT `["python", "-m"]`, 기본 CMD `["migration"]`, 사용자 `migration`(uid 10001), 상태 디렉터리 `/var/lib/worker`.

## 설계 문서와 다르게 결정한 점

- `GRANT CONNECT ON DATABASE` 는 SQL 파일이 아니라 init_db.py 가 수행한다 (DB 이름이 설정값이라 하드코딩 불가).
- 역할은 SQL 에서 NOLOGIN 으로 만들고 init_db.py 가 LOGIN 과 비밀번호를 붙인다. 비밀번호가 SQL 파일에 들어가지 않게 하기 위해서다.
- 슬롯은 `cdc prepare` 가 미리 만든다 (`pg_create_logical_replication_slot(..., 'pgoutput')`). Debezium 은 기존 슬롯을 그대로 쓴다. `--no-slot` 으로 Debezium 에 맡길 수도 있다.
- Kafka 토픽의 `H` offset 은 Connect REST 만으로 구할 수 없어 계산하지 않는다. sink `/offsets` 값·정체 여부와 DB 커밋 결과로 판정하며 이 사실을 체크포인트에 남긴다.
- READY 단계에서 `--approve` 를 요구해 되돌리기 어려운 쓰기 차단 이전에 사람이 개입하는 지점을 두었다. 설계의 lease_owner/fencing_epoch 는 워커 1개·전환 실행자 1명 전제라 구현하지 않았다.
- `phase` 는 요청 생성 시점의 값이다. PAUSED 중 생성된 요청은 `onprem_live` 로 표시되지만 실제 커밋 DB 는 요청 기록의 `target` 에 남는다 (설계 "실제 커밋 DB 는 별도 요청 기록" 그대로).
- 워커는 재시작 시 `worker.json` 의 target 을 CLI 값보다 우선한다.

## 귀환(AWS→온프렘) 대비 보강 (2026-11-04)

- `targets.<name>.kind: rds | self_managed` — `init_db` 가 REPLICATION 부여 방식을 고를 때 쓴다. 없으면 종전처럼 `job.source` 가 아닌 쪽을 RDS 로 추정한다. 귀환처럼 AWS 가 source 면 반드시 `kind: rds` 를 적는다.
- cutover PRECHECK 가 워커 `/status.known_targets` 로 **원본·대상 target 이 워커 설정에 있는지** 확인한다. 없으면 SWITCHING(자동 원복 없음)에서 `/target` 이 실패해 NEEDS_RECOVERY 가 되므로 미리 막는다. 워커 ConfigMap(gitops `apps/db-insert-worker`)에 귀환 대상 `onprem_return` 이 들어 있어야 한다.
- COMPLETED 직전에 원본의 비활성 slot 과 publication 을 지운다(`cutover.drop_source_slot_on_complete`, 기본 true). 지우지 않으면 공용 Postgres 에 WAL 이 계속 쌓인다. 실패해도 전환 결과는 바꾸지 않고 경고만 남기니 그때는 `python -m migration.cdc drop-slot` 을 수동 실행한다.
- 앱 세션 종료(`FENCING`)는 `datname = current_database()` 로 한정한다. 귀환 대상 `migration_demo_return` 이 정방향 원본과 같은 인스턴스에 있어도 서로의 세션을 끊지 않는다.


## Harbor 온프레미스 목적지 준비

별도 Ansible 모듈 `python -m migration.harbor_infra`로 기존 Harbor 조사·신규 설치·접근권한·노드 인증서 신뢰 설정을 준비합니다. 실제 업무 이미지 복사는 수행하지 않습니다. [설치와 플랫폼 호출 안내](docs/harbor-infra/README.md)를 참조하십시오.

# OpenBao 온프레미스 준비

`python -m migration.openbao_infra`로 기존 OpenBao 탐색·점검, 선택한 Ubuntu 노드 한 대의 운영체제 서비스 설치, 인증·접근 권한과 고정 키 인계를 수행합니다. 실제 비밀값 이관과 Keycloak 서버 설치는 포함하지 않습니다. [사용법과 플랫폼 연동](docs/openbao-infra/README.md), [검증 범위](docs/openbao-infra/VALIDATION.md), [예시 설정](config.openbao-infra.example.yaml)을 참고하십시오.

# Wazuh 온프레미스 감사로그 기반 준비

`python -m migration.wazuh_infra`로 기존 환경 조사·점검, 선택한 Ubuntu 노드 한 대의 Wazuh 설치·전체 이벤트 검색 설정·원본 보관공간·용량 정보를 준비합니다. 실제 원본 파일 이관과 AWS 설정 변경은 수행하지 않습니다. [사용법과 플랫폼 연결](docs/wazuh-infra/README.md), [입력 예시](config.wazuh-infra.example.yaml), [검증 범위](docs/wazuh-infra/VALIDATION.md)를 참고하십시오.

## AWS 비밀값의 OpenBao 이관

`python -m migration.secrets`로 준비된 OpenBao에 AWS Secrets Manager의 현재·직전 값을 이관하고 검증합니다. 계정 이관 PR #51의 신원·경로·정책 결과를 연결하며, 별도 명령으로 PostgreSQL 일반 접속 계정의 단일 비밀번호 또는 명시한 JSON 필드를 교체합니다. 앱 변경과 원본 삭제는 수행하지 않습니다. [구현 흐름](migration/secrets/README.md), [운영 명령](docs/secrets/operations.md), [결정과 후속 작업](docs/secrets/decisions-and-followups.md), [검증 범위](docs/secrets/validation.md)를 참고하십시오.
