# 제어 서버 검증 기록

2026-11-07, 격리 개발 환경에서 아래 명령으로 **97개 통과**를 확인했습니다. Starlette의 httpx TestClient 지원 중단 예정 경고 1개가 있었으며 실패는 없었습니다. 전체 플랫폼 시험이 아니라 연결 기능과 변경에 관련된 기존 API·접근 PIN·등록 시험 범위입니다.

- 상태·HTTP 계약: 39개. 시간 경계, 요청 중복·응답 유실, 조직·권한, 잘못된 입력, 단말 폐기, 직접 공개 경로 작업 차단, 120초 수집 작업, 캐시 방지.
- 실제 암호: 4개. noble 브라우저 서명/Rust 서명 → Python OpenSSL 검증, 서버 실제 서명, Rust 등록키·지속키 분리 서명의 HTTP 흐름과 재전송 거절.
- 실제 PostgreSQL·Redis: 6개. 동시 증명의 단일 소비, 중복 요청 생성, 실패 코드 횟수 커밋, 20번째 실패부터 24시간 차단과 KST 자정, Redis 장애 시 차단, 준비 확인 만료.
- 기존 backend 관련 회귀: 48개.

실암호 시험에서는 OpenSSL 3.6.3을 사용했습니다. 운영의 OpenSSL 3.5 조건을 변경하지 않았습니다. Rust HTTP 흐름 시험의 배포 준비 검사·저장소는 시험 대역을 사용하고 서명·검증만 실구현을 사용합니다. 실제 PostgreSQL·Redis 동시성은 별도의 시험으로 검증했습니다. 실제 HTTPS 배포·브로커와 단말의 WireGuard 네트워크·고객 Kubernetes까지 연결한 전체 인수 시험은 이번 기록에 포함되지 않습니다.

## 재현 명령

저장소 루트에서 전용 Python 환경에 두 패키지를 설치한 뒤 실행합니다. `CONNECTION_TEST_SIGNER`는 `agent/examples/sign-fixture.rs`를 빌드한 **시험 전용 고정키 실행 파일**입니다. 운영 배포에 포함하지 않습니다.

```bash
CONNECTION_TEST_POSTGRES_DSN='<전용 PostgreSQL DSN>' \
CONNECTION_TEST_REDIS_URL='<전용 Redis URL 및 시험 DB>' \
ONEPORT_TEST_DATABASE_URL='<전용 PostgreSQL DSN>' \
CONNECTION_TEST_OPENSSL='<ML-DSA 지원 OpenSSL 절대 경로>' \
CONNECTION_TEST_NATIVE=migration/connection/agent/target/debug/migration-connector \
CONNECTION_TEST_SIGNER=migration/connection/agent/target/debug/examples/sign-fixture \
.venv/bin/pytest \
  migration/connection/tests/test_control.py \
  migration/connection/tests/test_control_api.py \
  migration/connection/tests/test_control_crypto.py \
  migration/connection/tests/test_control_integration.py \
  platform/backend/tests/test_api.py \
  platform/backend/tests/test_access_pin.py \
  platform/backend/tests/test_enrollments.py -q
```

실행 결과: `97 passed, 1 warning in 7.40s`.

해당 제어·계약·플랫폼 연결 모듈과 변경한 backend 파일, 제어 시험만 대상으로 `ruff check` 및 `ruff format --check`를 실행했고 모두 통과했습니다. 기존 플랫폼 전체 코드의 형식을 일괄 변경하지 않았습니다.
