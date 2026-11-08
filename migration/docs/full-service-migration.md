# DB·데모앱·접수서비스 전체 이전

이 옵션은 기존 데모앱을 AWS EKS에 재배포하고, DB를 RDS로 전환하며, 접수서비스의 Redis 요청 원장을 AWS 전용 Redis로 인계한다. 실행 중인 Pod의 메모리나 임의의 Kubernetes 앱을 복제하는 기능은 아니다. GitOps가 앱·접수서비스·Redis를 먼저 배포하고 러너가 준비 상태를 검증한다.

사용자 도메인의 DNS/Caddy 경로 변경은 이번 단계에서 하지 않는다. 기존 경로는 원본 접수서비스의 영속 forwarding을 통해 AWS로 연결된다. `COMPLETED`는 세 구성요소의 이전 결과이며, `post_switch.dns_cutover_pending=true`가 남는다. 기존 주소를 사용하는 클라이언트의 전환과 원본 폐기는 후속 작업이다.

## 설정

`gitops/apps/live-db-cutover-full-aws/config.full-aws.yaml`이 환경별 설정의 원본이다. `config.yaml`의 DB-only 작업을 바꾸지 않고 별도 GitOps overlay로 선택한다. 새 `job_id`를 사용한다.

플랫폼에서 생성한 작업은 `platform_data_targets`의 명시 매핑으로 환경 ID(`onprem`)를
실제 DB 대상(`onprem_return`)으로 연결한다. 설정한 출발→도착 방향과 일치해야 하며,
`platform_apps`에 명시한 데모앱 이름/워크로드 ID 중 하나만 선택할 수 있다. 다른 앱·다른 DB·역방향 작업을 이 러너가 데모앱 이전으로 잘못 실행하지 않는다.
플랫폼 command claim 응답에 선택 앱 목록이 포함되어야 한다(이 PR의 플랫폼 API 배포 필요).

필수 `full_migration` 항목:

| 항목 | 의미 |
|---|---|
| `destination_control_url` | 새 접수서비스의 HTTPS `/control` 주소 |
| `destination_token_env` | 해당 제어 API 토큰 환경변수 이름 |
| `destination_receipt_url` | 새 접수서비스의 직접 HTTPS 주소 |
| `destination_api_token_env` | 요청 API 토큰 환경변수 이름 |
| `destination_target` | 새 큐가 전달할 앱 이름 (`aws_eks`) |
| `destination_app_version` | EKS에 배포한 이미지의 `APP_VERSION` (게시된 빌드 SHA) |
| `forward_target` | 원본 접수서비스에 미리 설정한 목적지 allowlist 이름 (`aws_receipt`) |
| `timeout_seconds` | 제어 호출 제한 시간 |

`ingress.targets`의 대상과 `destination_target`이 같아야 한다. 대상 `app_checks`에는 HTTPS 앱 주소·environment·실제 DB명이 필요하다. 원본 제어 토큰도 필수다. Secret 값은 YAML·체크포인트·정책 입력에 쓰지 않는다. `traffic`과 함께 설정하면 로딩 단계에서 거부한다(DNS/공개 경로 전환과 구분).

대상 앱은 `/api/info`에서 고정 버전·환경·DB를 증명해야 한다. 대상 INSERT가 닫혀 있는 사전 확인에는 쓰기 준비 상태를 요구하지 않고, GRANT 후 `/readyz`와 쓰기 제어 상태를 확인한다. 아직 게시되지 않은 feature SHA를 운영 이미지처럼 사용하지 않는다.

## 실행 순서

1. `PRECHECK`: 원본 DB는 쓰기 허용, RDS는 앱 쓰기 차단. 봇·원본 원장·미사용 AWS standby 원장·고정 EKS 앱 버전 확인.
2. `COPYING`/`CATCHING_UP`: 기존 CDC 초기 복사·변경분 반영 검증. 승인 대기까지는 기존 접수 경로로 처리.
3. `FENCING`: 봇 생성 정지/미확인 요청 소진 → 원본 접수 봉인 및 진행 전달 완료 → 원본 INSERT 회수·세션 종료·차단 확인. 봉인 이후 새 요청은 503 재시도 응답이며 성공 접수로 응답하지 않음.
4. `DRAINING`/`SYNC_CONFIRMED`: 마지막 커밋과 데이터 건수 확인, CDC 커넥터 종료.
5. `SWITCHING`: 전체 요청 내용·accepted/committed 상태를 페이지 단위 복사하고 digest/건수 검증 → RDS INSERT 허용 → EKS 앱 준비 확인 → AWS 접수 활성화·전달 재개 → 원본을 forwarding 전용으로 변경.
6. `VERIFYING`: 직접 AWS 접수 주소의 원장 식별자와 EKS 앱 버전/DB 확인 → 대상 봇 배치 커밋 및 DB 건수 확인 → 봇 생성 정지.
7. `COMPLETED`: 구성요소 이전 결과와 DNS 후속 조건 기록. 운영자가 원본을 자동 삭제하지 않음.

앱 데이터의 정본은 RDS로 인계된다. Redis는 서버의 메모리 전체나 소비자 lease를 복제하지 않고, 접수 API가 소유한 불변 요청·중복 방지 기록·대기 순서를 인계한다. 목적지는 자신의 소비자 lease를 갖고 새 요청을 처리한다. 프로토콜의 상세 계약은 [sample-app durable ingress 문서](https://github.com/SoftBankHackathon2026/sample-app/blob/main/docs/receipt-handoff.md)를 따른다.

## 실패와 재개

- 작업 ID·양쪽 원장 ID·설정 digest가 달라지면 자동 재개하지 않는다. 기존 DB-only 작업을 full 모드로 바꾸지 않는다.
- 페이지 전송 응답이 유실돼도 같은 내용 재전송은 멱등 처리된다. 레코드 본문은 Python 체크포인트/로그에 저장하지 않는다.
- 봉인 시도부터 원본 접수를 자동으로 다시 열지 않는다. 실패 상태와 실제 양쪽 원장을 확인해 같은 작업을 재개한다.
- RDS GRANT/접수 활성화 호출은 **호출 전에** 시도 의도를 영속 저장한다. 응답 유실을 실패 확정으로 해석해 원본 쓰기를 열지 않는다.
- `NEEDS_RECOVERY`는 authoritative DB와 접수 원장을 확인한 뒤 기존 CLI의 `--force-retry`로 재개한다. 자동 rollback 대신 멈춰 데이터 손상을 피한다.
- AWS 접수 프로세스 재시작 시 admission/원장은 보존되며 전달은 paused로 시작한다. 러너는 같은 handoff를 확인하고 `/resume`한다.
- 원본 forwarding은 구주소 호환용이다. DNS/클라이언트 전환 전 원본 전원을 꺼도 된다는 뜻이 아니다.

## 검증

```sh
cd migration
python -m pytest tests/test_cutover*.py tests/test_return_cutover.py tests/test_config.py tests/test_ingress_client.py tests/test_full_migration.py
SAMPLE_APP_SOURCE=/path/to/sample-app python -m pytest tests/test_full_migration_contract.py
```

두 번째 테스트는 `npm ci`된 새 sample-app checkout과 로컬 `node`, `redis-server`를 사용한다. 임시 Redis 두 개·실제 Node 접수 API·Python 인계 클라이언트를 연결하고, 원본 중단 뒤 대상 신규 쓰기·기존 조회·ID 충돌을 검증한다. 앱의 DB 어댑터는 메모리 fixture다. 실제 RDS/EKS/ALB/Tailscale/CDC의 통합 시험이나 운영 전환을 대체하지 않는다.

배포는 sample-app/Hackathon 코드 머지 및 이미지 게시 → GitOps 고정 SHA/앱 버전 갱신 → 필수 Secret·AWS 접수 전용 주소/TLS 준비 → GitOps overlay 적용 → 읽기 사전 확인 → 가연의 실제 통합 시험 순서다. 사용자 도메인의 DNS는 이 순서 이후 별도로 전환한다.
