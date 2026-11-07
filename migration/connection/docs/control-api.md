# 연결 제어 서버와 플랫폼 연결

기존 bearer-token 환경등록, 로그인, 설치기, 데이터 이관기는 유지합니다. 새 API는 `/api/v1/connections`에 추가합니다. 서버에는 선택 패키지 `migration-connection`을 설치하고 기능을 명시적으로 설정해야 합니다. 패키지가 없거나 설정되지 않으면 capabilities는 `available:false`를 반환합니다. 기본 배포를 새 연결 방식으로 자동 전환하지 않습니다.

## 실행과 설정

저장소 루트에서 전용 Python 3.12 환경에 `pip install -e migration/connection -e platform/backend`로 개발 설치합니다. 기존 플랫폼 이미지에는 패키지가 없으므로, 추가 기능용 `platform_bridge/Dockerfile`을 저장소 루트 문맥에서 빌드합니다. `PLATFORM_IMAGE`에는 이번 브랜치의 backend를 빌드한 검토된 이미지 참조를 전달합니다. 이미지에는 서버 개인키나 고객 자격증명을 포함하지 않습니다.

| 설정 | 의미 |
| --- | --- |
| `ONEPORT_CONNECTION_ENABLED=true` | 새 기능 명시 활성화 |
| `ONEPORT_CONNECTION_CLOUD_TENANTS` | AWS·GCP 탐색을 허용할 조직 식별자 목록(쉼표 구분, 기본은 비어 있음). 지원하는 단말 배포본 게시와 실환경 검증 후 지정 |
| `ONEPORT_CONNECTION_REDIS_URL` | 전용 Redis 연결. 장애 시 신규 인증 허용 금지 |
| `ONEPORT_CONNECTION_OPENSSL` | 검증된 OpenSSL 3.5 실행 파일의 절대 경로 |
| `ONEPORT_CONNECTION_JOB_SIGNING_KEY` | 작업 서명용 ML-DSA-65 개인키 보호 파일. 그룹·다른 사용자 읽기 금지 |
| `ONEPORT_CONNECTION_RUNTIME_TOKEN_FILE` | 브로커 전용 서비스 인증값 파일. 최소 32자, 그룹·다른 사용자 읽기 금지 |
| `ONEPORT_CONNECTION_BOOTSTRAP_URL` | 검증된 배포 진입점의 HTTPS 주소 |
| `ONEPORT_CONNECTION_BOOTSTRAP_SHA384` | 해당 진입 스크립트의 SHA-384 96자리 소문자 값 |
| `ONEPORT_CONNECTION_RELEASE_VERSION` | 고정 배포 버전 |
| `ONEPORT_CONNECTION_RUNTIME_TENANT_ID` | 이 배포의 브로커가 처리하는 단일 조직 |
| `ONEPORT_CONNECTION_RUNTIME_GATEWAY_ID` | 이 배포의 브로커 식별자 |

기존 플랫폼의 PostgreSQL 연결과 로그인 설정을 재사용합니다. 데이터베이스 준비 시 `014_connections.sql`이 적용됩니다. 요청은 조직·요청자·중복 방지 식별자로 유일하며, 등록 증명·승인·수집·등록 갱신은 동일 행 잠금 아래 수행됩니다. 승인 실패 횟수와 소비된 확인값은 오류 응답일 때도 저장합니다.

OpenSSL·Redis·배포 설정뿐 아니라 브로커의 **최근 60초 준비 확인**이 있어야 명령을 발급합니다. 브로커는 초기 인증서·네트워크 보조 프로그램·배포 검증 후 `/api/v1/connection-runtime/health`에 조직·게이트웨이·버전·프로필·스크립트 해시를 보고합니다. 서버의 설정과 다르면 거절합니다. 현재 서비스 인증값은 설정된 단일 조직에만 유효하며 authorize/tunnel/relay 모두 조직을 확인합니다. 다른 조직은 capabilities가 비활성입니다. 이 확인은 배포 검증과 실환경 인수 시험을 대신하지 않습니다.

브로커의 `/auth-check`와 `/auth-failure`는 `{source_ip}`를 받습니다. source_ip는 브로커가 직접 관찰한 TCP 상대 주소이며 전달 헤더를 신뢰하지 않습니다. 암호 서명·인증서 검증 실패만 하루 20회(KST) 집계하고 20번째 실패부터 24시간 차단합니다. 시간 초과·통신 단절·폐기된 정책 접근·이미 소비된 등록 요청의 재전송은 인증 실패로 집계하지 않습니다. 일반 요청 제한은 IP별 분당 120회이며 Redis 장애 시 허용하지 않습니다.

웹 요청 생성·변경은 로그인 계정과 조직별로, 단말 확인·증명 요청은 이미 존재하는 요청 식별자와 경로별로 분당 120회 제한합니다. 등록 증명, 공개 단말 요청, 터널 중계 요청의 제한을 분리합니다. 정상 확인값을 소비한 뒤 서명 검증에 실패했을 때만 해당 범위의 인증 실패 횟수를 올립니다. 웹 확인 코드는 요청별 5회 제한을 적용합니다. 프록시 주소나 전달 헤더는 이 제한의 키로 쓰지 않습니다.

브로커 내부 API는 승인된 서비스만 접속하도록 네트워크로 제한하고 HTTPS로 보호하거나 동일 호스트의 보호된 통신으로 연결해야 합니다. 단말 인증서 교환에 대한 IP 제한에는 브로커가 직접 확인한 주소만 사용합니다.

웹에서 기능을 사용할 수 없다는 응답을 받으면 연결 목록을 불러오지 않고 반복 조회를 멈춥니다. 배포가 준비되면 화면을 다시 열어 상태를 확인합니다. 기존 환경 목록은 연결 기록을 합치지 않으므로 연결 저장소 오류의 영향을 받지 않습니다.

## 브라우저 호출

연결 API의 정상·예상 오류 응답은 `Cache-Control: no-store`를 사용합니다. 모든 브라우저 호출은 기존 로그인 세션과 조직을 사용합니다. 변경에는 기존 owner/member 권한과 허용 Origin을 검사합니다. demo/operator 모드로 실단말 등록을 발급하지 않습니다.

| 메서드·경로 | 입력 | 결과 |
| --- | --- | --- |
| GET `/capabilities` | 없음 | available/reason/providers/bootstrapUrl/releaseVersion |
| GET 기본 경로 | 로그인 | 해당 조직의 ConnectionSnapshot 목록 |
| POST 기본 경로 | publicKey, operationId, 선택 provider | ConnectionSnapshot, deployment |
| GET `/{id}` | 로그인 | 최신 ConnectionSnapshot |
| POST `/{id}/cancel` | 빈 객체 | 미사용 요청 취소 |
| POST `/{id}/approve` | candidateDigest, code, 선택 replaceId | 단말 승인 및 선택한 이전 연결 교체 |
| POST `/{id}/reject` | 빈 객체 | 후보 폐기 |
| POST `/{id}/rediscover` | 빈 객체 | 승인된 클라우드 단말에서 대상 탐색 재요청 |
| POST `/{id}/collect` | discoveredId | 선택한 대상의 서명된 조회 작업 생성 |
| POST `/{id}/register` | collectedId, inventoryDigest, target | 환경등록 |
| POST `/{id}/reconnect` | 빈 객체 | 새 연결 요청 |
| POST `/{id}/revoke` | 빈 객체 | 서버 권한 철회, 별도 정리 상태 |

Snapshot의 `collectionPending`은 유효한 수집 작업이 있고 승인·터널 READY 상태일 때만 true입니다. 요청 접수와 완료를 구분하며 만료·키 세대 변경·철회·수집 완료 시 false로 표시합니다. 작업 서명·내부 payload는 브라우저 응답에 포함하지 않습니다.

`provider`는 `kubernetes`(기본값), `aws`, `gcp` 중 하나이며 명령 발급 후 바꿀 수 없습니다. 클라우드 요청의 탐색 완료 상태와 경고는 `discoveryComplete`와 `discoveryWarnings`로 전달합니다. 동일한 단말 연결에서 재탐색할 수 있으며 이전 탐색 세대의 결과는 수락하지 않습니다.

`target`은 organization/project/name/kind/region입니다. 이전 웹 화면의 role 필드는 호환을 위해 수락합니다. 조직·종류·리전·수집 대상은 서버 자료와 일치해야 하며 출발·도착은 작업 생성 요청에 저장합니다. 현재 플랫폼에는 별도 프로젝트 권한 엔티티가 없으므로 project는 **조직 권한 아래의 분류 이름**입니다. 독립 프로젝트 접근권한을 검증했다고 표시하지 않습니다.

환경등록은 고객 자원 생성·설치·데이터 이관 권한을 부여하지 않습니다. 기존 환경 조회 API에 연결된 환경을 `executable:false`로 추가하며, Provider 실행 등록과 분리합니다. 터널이 끊기거나 단말이 폐기되어도 등록 기록을 삭제하지 않습니다.

단말이 등록 서명 본문에 선택 필드 `replacement_version: 1`을 포함하면 웹 승인에 `replaceId`를 함께 보낼 수 있습니다. 서버는 같은 조직에서 선택한 이전 요청을 철회하고 새 요청을 승인하는 변경을 하나의 트랜잭션으로 저장합니다. 지원하지 않는 단말이나 이미 다른 요청으로 교체된 연결은 기존 권한을 변경하기 전에 거절합니다.

교체 승인 뒤 중계 서버는 이전 연결 식별자와 키 세대가 일치하는 터널을 정리하고 이전 요청에 `REVOKED` 또는 `REVOKE_BLOCKED`를 보고합니다. 새 단말은 중계 확인 뒤 같은 식별자로 로컬 터널을 확인하고 서명한 `replacement-cleanup` 요청으로 결과를 보고합니다. 새 연결은 두 확인을 모두 받은 뒤에만 `READY`로 전환합니다. 이전 요청의 `cleanup.local`은 이전 단말의 인증 상태 삭제 확인으로 따로 유지합니다.

## 단말 증명

`POST /{id}/challenge`의 입력은 `{candidateId}`이고 결과는 challenge_id/challenge/expires_at_ms입니다. 등록 요청 생성부터 60초, 확인값은 남은 시간과 30초 중 짧은 기간입니다.

`POST /{id}/prove`는 payload와 enrollment_signature/device_signature, 선택적인 metadata를 받습니다. payload의 정확한 필드는 version/request_id/candidate_id/challenge_id/challenge/device_auth_public_key/wireguard_public_key/audience/expires_at_ms입니다. ML-DSA-65 공개키(1952바이트), 서명(3309바이트), WireGuard 공개키(32바이트)는 패딩 없는 base64url로 전달합니다. audience는 migration-connector-enrollment입니다. metadata의 hostname/os/arch는 자기 보고이며 인증 근거가 아닙니다.

서명 자료는 `migration/enrollment/v1\n` 또는 `migration/device-possession/v1\n` 뒤에 정규화한 payload를 붙입니다. 정규화는 UTF-16 키 정렬과 UTF-8 문자열을 사용하고 실수·잘못된 Unicode·JavaScript 정확한 정수 범위를 벗어난 값·중복 필드를 거부합니다. 두 개인키의 소유 증명이 모두 성공해야 요청을 소비합니다.

성공 결과는 device_id/candidate_digest/confirmation_code/approval_expires_at_ms입니다. 확인 코드는 payload SHA-384의 앞 40비트를 Base32 8자리로 표현합니다. 브라우저 Snapshot에는 코드를 반환하지 않습니다. 웹 승인은 5분, 코드 실패는 5회이며 단말 키 수명은 승인부터 90일입니다.

`/{id}/device-challenge`에서 받은 확인값으로 지속 키 서명 요청을 생성합니다. payload는 version/request_id/candidate_id/challenge_id/challenge/action/data/expires_at_ms이며 접두부는 `migration/device-request/v1\n`입니다. 외부 `/{id}/device`는 status와 폐기·승인거절·승인기한 만료 후 cleanup만 처리하며 `/{id}/recover`는 status만 처리합니다. 응답 유실은 같은 후보 지속 키로 복구하며 등록 생성값을 재사용하지 않습니다.

## 터널 내부 작업

브로커는 runtime Bearer 인증으로 `/{id}/authorize`를 호출하여 승인된 단말 공개키와 서버가 결정한 조직을 확인합니다. 입력 tenant_id를 믿거나 클라이언트 인증서 자체 신고 이름으로 조직을 선택하지 않습니다.

브로커 보고 `/{id}/tunnel`은 epoch/connection_id/state를 필수로 받습니다. READY는 checks=2(양방향 확인), key_expires_at_ms(최대 30분), lease_expires_at_ms(최대 60초)를 요구합니다. 같은 세대의 키 수명을 heartbeat로 늘릴 수 없습니다. Snapshot의 tunnelChecks=4는 화면의 인증·키 설정·WireGuard·양방향 확인 네 단계를 뜻합니다. 새 세대는 진행 중 수집 작업을 무효화합니다.

단말의 poll/discovered/inventory 요청은 **터널 서비스가 중계한 runtime `/{id}/device`**에서만 처리합니다. 외부 공개 경로에서 같은 서명 요청을 보내도 거절합니다. 조회 작업은 job_id/operation/device_id/request_id/epoch/target_id/expires_at_ms를 담고 `migration/job/v1\n` 접두부로 서명합니다. 조회 작업의 유효기간은 120초이고 단말 수집 실행 상한은 90초입니다. 실행 중에도 15초마다 서명된 확인 요청으로 터널의 60초 임대를 유지하며 완료 시 작업 만료를 재검사합니다. operation은 environment.inspect만 허용합니다. 설치·셸·비밀 읽기 작업은 없습니다.

inventory 결과는 `{job_id,report}`입니다. report의 요청·단말·세대·선택 대상·수집 시각과 고정 허용 필드를 검사합니다. 결과와 표시 요약을 대조하여 원문 자격증명을 넣는 임의 필드를 허용하지 않습니다. 같은 조직의 브라우저라도 다른 대상·해시·세대의 결과로 등록할 수 없습니다.

철회는 새 작업·인증을 즉시 차단합니다. 게이트웨이와 단말 정리는 별도로 보고해야 완료로 표시합니다. 두 구성요소가 연락되지 않으면 미확인 상태로 유지하며 로컬 최대 60초 임대 만료는 네이티브 실행기의 책임입니다.

## 검증과 범위

`tests/test_control.py`, `test_control_api.py`는 상태·서명 실패·권한·시간 경계·전송 경로를 시험용 암호 구현으로 검증합니다. 이 시험과 별도로 `test_control_crypto.py`는 브라우저 noble 서명·Rust 실행기 서명을 Python OpenSSL 검증기로 확인하고 서버 ML-DSA-65 실제 서명을 검증합니다. `CONNECTION_TEST_OPENSSL`, `CONNECTION_TEST_NATIVE`, `CONNECTION_TEST_SIGNER`로 실행 파일을 지정합니다. 시험 전용 Rust signer는 등록키·지속키를 다른 고정 seed로 구성하고 실제 등록 증명 → 상태 복구 → 웹 승인 → 브로커 권한조회 → 터널 준비 보고 → 서명된 작업 조회 → 재전송 거절까지 HTTP 계약을 확인합니다. 이 통합 시험의 저장소·배포 준비 검사는 명시적 시험 대역이며 실브로커 네트워크를 구동했다고 주장하지 않습니다. 시험에 사용한 로컬 OpenSSL 3.6.3으로 운영의 OpenSSL 3.5 준비 조건을 우회하지 않습니다.

`test_control_integration.py`는 명시한 전용 PostgreSQL과 Redis에서 실행합니다. PostgreSQL은 임시 스키마만 생성·삭제하며 Redis는 전용 시험 데이터베이스를 비웁니다. 운영 저장소를 지정하지 않습니다.

```bash
CONNECTION_TEST_POSTGRES_DSN='<전용 PostgreSQL 연결>' \
CONNECTION_TEST_REDIS_URL='<전용 Redis 시험 DB>' \
python -m pytest migration/connection/tests/test_control_integration.py -q
```

공개키·감사 기록의 주기적 보존기간 정리, 별도 프로젝트 권한 모델, 실제 고객 네트워크와 Kubernetes 인수 시험은 추가 운영 검증 대상입니다. 연결 완료를 설치·이관 완료로 표시하지 않습니다.
