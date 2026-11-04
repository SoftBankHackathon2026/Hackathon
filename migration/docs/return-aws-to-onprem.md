# AWS → 온프렘 귀환: 원본 점검과 귀환 대상 준비

작업보드 카드 "AWS→온프렘 원본 점검·귀환 대상 준비"의 절차 문서. 담당: 혜연.

> 아직 실제 RDS·온프렘 k3s에서 실행하지 않았다. 로컬 PostgreSQL 16에서 `return_precheck` 통합 테스트만 통과했다.
> 실제 실행 전까지 이 문서는 계획이다.

## 범위

정방향(온프렘 → AWS)이 끝난 뒤, **현재 AWS** 데이터를 **옛 온프렘 DB와 분리된 빈 온프렘 DB**로 되돌리기 위한 준비 단계다.
공통 도구(`init_db`, `cdc`, `cutover`, `verify`, `loadgen`)는 고치지 않고 귀환 설정 파일로만 방향을 바꿔 쓴다.

| 이 작업에서 추가한 것 | 용도 |
|---|---|
| `config.return.example.yaml` | 귀환 설정. source=aws, target=onprem_return, job_id=return_001 |
| `sql/return/aws_source_replication.sql` | RDS의 `migration_cdc`에 `rds_replication` 부여 (멱등) |
| `migration/return_precheck.py` | 읽기 전용 사전 점검. 완료 기준을 항목별로 PASS/FAIL 판정 |
| `tests/test_return_precheck.py` | 판정 단위 테스트, 실제 PostgreSQL 수집 통합 테스트 |

이름 규칙은 공통 코드가 만든다: slot `return_001`, publication `return_001_pub`, 커넥터 `return_001-source`와 `return_001-sink`, 토픽 `return_001.public.migration_demo`.
Debezium 커넥터 템플릿은 원본과 대상을 설정에서 읽으므로 정방향 템플릿을 그대로 쓴다.

## 완료 기준과 점검 항목

| 완료 기준 (카드) | `return_precheck` 항목 |
|---|---|
| 귀환 원본은 현재 AWS | `config.direction`, `aws.wal_level_logical`, `aws.cdc_replication`, `aws.replication_slot_capacity` |
| AWS 데이터를 1,000건으로 초기화하지 않는다 | `aws.seed_preserved`(seed 정확히 1,000건), `aws.not_reset`(seed 외 데이터 존재) |
| 귀환 대상은 빈 온프렘 DB | `onprem.database_exists`, `onprem.table_empty`, `onprem.separate_from_forward_source` |
| 대상 앱 쓰기는 닫고 Sink만 적재 | `onprem.app_write_closed`, `onprem.sink_write` |
| AWS와 DDL·타입·확장 차이 확인 | `schema.columns_match`, `schema.primary_key_match`, `schema.extensions`(경고), `schema.major_version`(경고) |
| 정방향 Source/Sink 종료, 자동 재개 안 됨 | `connect.forward_stopped`, `forward.slot_inactive`(경고) |

## 절차

비밀번호는 Secret 환경변수로만 준다. 이 문서와 설정 파일에는 적지 않는다.

```
# 0) 설정 준비 (호스트만 실제 값으로)
cp config.return.example.yaml config.return.yaml
#    targets.aws.host = oneaction-infra database 스택의 RDS 엔드포인트

# 1) 인계 확인 (가연님): 정방향 cutover COMPLETED, 커넥터 삭제, verify(post) 통과 보고서

# 2) AWS 원본 복제 권한 (RDS 파라미터 반영 후)
psql "host=<RDS> dbname=migration_demo user=oneaction_admin sslmode=require" \
  -f sql/return/aws_source_replication.sql

# 3) 온프렘 귀환 대상 생성 (빈 DB, 앱 INSERT 닫힘)
python -m migration.init_db --config config.return.yaml --target onprem_return
#    seed 는 실행하지 않는다

# 4) 사전 점검 (읽기 전용)
python -m migration.return_precheck --config config.return.yaml --forward-config config.yaml \
  --report reports/return-precheck.json
```

3)에서 `init_db`는 귀환 대상을 `job.source`가 아닌 쪽으로 보고 `REVOKE INSERT`만 하고 publication은 만들지 않는다. 대상으로 맞는 동작이다.
**AWS에는 `init_db`를 다시 돌리지 않는다.** 이유는 아래 "가연님 확인 요청" 1번에 적었다.

제어 클러스터 접근 경로는 4)가 같은 파드에서 AWS와 온프렘 둘 다 접속에 성공하는지로 확인한다. Connect 워커에서도 RDS 5432에 닿아야 Source가 동작한다.

## 협의·요청 사항

R&R이 겹치지 않도록 다른 담당 영역은 고치지 않고 요청으로 남긴다.

### 유빈님 (AWS 환경, oneaction-infra)

1. **RDS 논리 복제 파라미터**: `infra/database/main.tf`의 `oneaction-postgres16` 파라미터 그룹에는 지금 `rds.force_ssl`만 있다. 귀환 원본이 되려면 `rds.logical_replication = 1`(apply_method `pending-reboot`)이 필요하고 **재부팅**해야 `wal_level=logical`이 된다. 정방향 실행 중 재부팅하면 안 되니 적용 시점을 미리 맞춰야 한다. 처음 RDS를 띄울 때 같이 넣어두면 재부팅 한 번을 아낄 수 있다.
2. **네트워크 경로**: RDS 보안그룹은 EKS 서브넷 CIDR에서만 5432를 허용한다. Kafka Connect와 이전 도구가 도는 제어 클러스터에서 RDS로 가는 경로가 필요하다. 정방향 Sink도 같은 경로를 쓰니 정방향 준비 때 함께 정해지면 귀환은 추가 작업이 없다.
3. **마스터 계정**: RDS 마스터는 `oneaction_admin`이고 비밀번호는 Secrets Manager가 관리한다(`manage_master_user_password`). 귀환 설정은 이 값을 기준으로 했다.

### 가연님 (공통 이전 도구)

공통 코드에서 귀환 방향(RDS가 원본)에 걸리는 부분이다. 귀환 쪽에서 우회한 것과 수정이 필요한 것을 나눴다.

1. `init_db.run`의 `managed_rds = target_name != config.job.source`: 귀환에서는 AWS가 source라 RDS를 일반 PostgreSQL로 보고 `ALTER ROLE migration_cdc WITH REPLICATION`을 실행해 거부된다. → **우회**: AWS에는 init_db를 다시 돌리지 않고 `sql/return/aws_source_replication.sql`로 `rds_replication`만 부여한다. 대상별 `managed: true` 같은 설정이 생기면 이 우회는 필요 없다.
2. `cutover` PRECHECK의 CDC 권한 확인이 `pg_roles.rolreplication`만 본다. RDS는 `rds_replication` 멤버십이라 이 값이 false여서 **귀환 PRECHECK가 실패한다.** → **수정 필요**: `rolreplication OR pg_has_role(cdc, 'rds_replication', 'MEMBER')`. `return_precheck`는 둘 다 인정한다.
3. `cdc.prepare_source`의 `ALTER PUBLICATION ... OWNER TO migration_cdc`: RDS 마스터(rds_superuser)가 이 소유권 변경을 할 수 있는지 PG16 RDS에서 실제 확인이 필요하다. 안 되면 원본이 RDS일 때 owner 변경을 건너뛰는 분기가 필요하다.
4. `config.example.yaml`의 `targets.aws.users.admin.user`가 `postgres`인데 실제 RDS 마스터는 `oneaction_admin`이다 (정방향 설정, 참고용).
5. 인계받을 것: 정방향 완료 시점 AWS 건수와 phase별 건수, 커넥터 삭제 확인(`cutover status` 체크포인트), `forward_001` 슬롯을 지울지 결정, 워커 `state_dir`(S 집합) 위치. 워커가 귀환 SWITCHING 때 `onprem_return`으로 갈 수 있게 워커 설정에 귀환 target이 들어가야 한다.

## 다음 카드로 넘기는 것

- "AWS→온프렘 CDC·Kubernetes 귀환 설정": `config.return.yaml`을 gitops Secret/ConfigMap으로 배포, 귀환 커넥터 등록.
- "AWS→온프렘 귀환 전환 구성·실행": `cutover --config config.return.yaml run` (위 가연님 2번 수정 후).
