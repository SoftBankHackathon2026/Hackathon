# 시험 결과 구분 기록

2026-11-07 실행 기록입니다. 자동 회귀시험, 정상 API 대체 전달 시험, 실제 Chrome 명령 복사부터 등록까지의 시험을 구분합니다. 마지막 시험은 같은 요청의 생성·증명·웹 승인·터널·실제 Kubernetes 수집·등록까지 완료했습니다.

## 자동 회귀시험

| 범위 | 명령 | 결과 | 증명하는 범위 |
| --- | --- | --- | --- |
| 연결 제어·HTTP API | `.venv/bin/pytest migration/connection/tests/test_control.py migration/connection/tests/test_control_api.py -q` | 45개 통과 | 상태·권한·서명 실패·수집 대기·새 키 세대 수명. 저장소와 암호 시험 대역 사용 |
| 프런트 전체 단위 | `npm test` (`platform/frontend`) | 216개 통과 | UI 모델·API 어댑터 계약·문자 검증·복사 시간 제한·URL 경로 검증 등 |
| 프런트 빌드 | `npm run build` (`platform/frontend`) | 통과 | TypeScript 타입 검사와 Vite 번들 생성 |
| 모의 API 브라우저 계약 | `ONEPORT_BROWSER_TEST_PORT=4337 npm run test:browser -- browser-tests/live-connections.spec.ts --reporter=line` | Chromium·Firefox·WebKit 3개 통과, 17.1초 | `route.fulfill`·클립보드 대역을 사용한 화면 계약. **실제 서버·단말·터널 성공 근거가 아님** |
| 전체 자동 브라우저 회귀 | `ONEPORT_BROWSER_TEST_PORT=4337 npm run test:browser -- --reporter=line` | 37개 통과, 2개 의도적 제외, 45.1초 | 위 모의 계약·미리보기·반응형 화면·Chromium 미리보기 실제 클립보드. 위 3개와 중복 |

모의 API 브라우저 시험은 모든 `/api/v1/**` 요청을 가로채며 등록되지 않은 요청에도 시험 응답을 반환합니다. 실제 backend로 부수 요청을 전달하지 않습니다. 별도 포트 4337을 사용하여 수동 실제 Chrome 시험의 포트 4327과 분리합니다.

전체 자동 회귀의 제외 2개는 Firefox·WebKit에서 의도적으로 실행하지 않는 실제 클립보드 시험입니다. 처음 전체 실행은 이전 계약의 잘못된 확인 코드 `ZZZZ9999`와 미확인 정리를 완료로 기대하던 문구 때문에 6개 실패했습니다. 형식상 유효한 틀린 코드 `ZZZZ7777`, 실제 미확인 문구로 시험을 바로잡고 제품 소스 변경 없이 전체를 재실행했습니다. 최종 로그는 `control-browser-full.log`, 수정 전 실패 로그는 `control-browser-full-before-fixture-fix.log`에 별도 보존했습니다.

디스크 부족 이전의 브라우저 회귀 시도는 ENOSPC로 실패했으며 성공 건수에 포함하지 않습니다. 위 브라우저 결과는 저장 공간 회복과 API 격리 수정 후 다시 실행한 결과입니다. 빌드에는 의존 패키지의 `use client` 지시문 무시 경고가 있었으나 타입 검사·번들 생성은 성공했습니다. Python 시험에는 Starlette의 httpx TestClient 지원 중단 예정 경고 1개가 있습니다.

이전 단계의 실제 PostgreSQL·Redis와 암호 상호 운용 97개 회귀 기록은 `../../docs/control-validation.md`에 있습니다. 현재 표와 중복되므로 두 숫자를 합산하지 않습니다. 재접속 핫픽스 후 위 제어 시험과 `test_release_tools.py`, `test_e2e_release.py`를 함께 실행한 결과는 54개 및 하위 검사 12개 통과였습니다.

루트 담당의 최종 Python 통합 묶음은 실제 클러스터 조회를 포함하여 **77개 및 하위 검사 37개 통과, 7.13초**였습니다. 구성은 제어·API 45개, inventory 실제 클러스터 포함 14개, release/e2e release 9개, 보호된 클립보드 전달 시험 9개입니다. 위 개별 결과와 중복되므로 합산하지 않습니다. 이 중 저장소·암호 시험 대역을 사용하는 제어 단위시험과 실제 클러스터 조회를 혼동하지 않습니다.

실제 게시 경로인 `bootstrap.sh`로 계약시험 입력을 바꾼 직후 WebKit에서 복사 결과 대기 실패가 한 번 있었습니다. 소스 변경을 멈추고 같은 시험을 다시 실행한 결과 세 브라우저가 모두 통과했습니다. 최초 실패를 성공으로 간주하지 않으며 동시 개발 서버 갱신의 영향인지는 확정하지 않았습니다.

최종 프런트 시험에는 게시 URL의 점 문자 허용, 취소·대기 상태의 정확한 표시, RFC4648 Base32 확인 코드 문자, 복사 무응답 시간 제한이 포함됩니다. 복사 완료를 15초 안에 확인하지 못하면 발급 요청을 취소하며, 뒤늦은 발급 응답도 취소 후 명령 Blob을 만들지 않습니다. 취소 확인도 5초로 제한하고 확인되지 않은 취소는 완료로 표시하지 않습니다. 클립보드가 나중에 완료되더라도 해당 요청을 성공으로 반환하지 않습니다. 불완전한 네트워크에서 취소를 확정할 수 없을 때 서버의 기존 60초 만료가 유지됩니다.

최종 명령 로그는 시험 임시 디렉터리의 `control-frontend-unit.log`, `control-frontend-build.log`, `control-browser-contract.log`에 보관했습니다. 전체 단위 216개·빌드·모의 계약 3개가 모두 통과한 뒤 실제 브라우저 시험 중에는 프런트 파일 변경을 중단했습니다. 실제 Chrome 자동화 환경에서 클립보드 Promise가 끝나지 않은 현상은 도구 호환성 가능성을 포함하며 원인을 확정하지 않았습니다. 시간 제한 보완을 실제 복사 성공의 근거로 사용하지 않습니다.

## 실제 제어 인프라 관측

- PostgreSQL 스키마 준비, 실제 회원가입·로그아웃·로그인으로 조직 소유자 계정을 확인했습니다. 계정이나 세션을 DB에 직접 주입하지 않았습니다.
- OpenSSL 3.5.7 서명 준비 검사, 실제 Redis 연결, PostgreSQL 저장소 사용을 확인했습니다.
- `control.test` HTTPS는 실제 시험 인증기관으로 검증했고, 내려받은 bootstrap 파일의 SHA-384가 게시한 파일과 일치했습니다.
- 기존 `ONEPORT_FLEET_CONFIG`의 `mode:live`·빈 목록을 사용하여 시연 클러스터를 실제 수집 결과처럼 표시하지 않았습니다.
- 브로커의 실제 주기 보고는 2026-11-06 22:38:57 UTC부터 약 20초 간격으로 서버에서 HTTP 200을 확인했습니다. 네이티브의 HTTPS 연결 구현 수정·릴리스 sequence 2 반영 후 관측입니다.

### 성공 근거에서 제외한 진단 호출

2026-11-06 17:39:00 UTC에 네이티브 담당의 직접 Python `/connection-runtime/health` 진단 요청이 한 번 실제 60초 준비 임대를 생성했습니다. 26초 후 브라우저가 요청을 발급·취소했습니다. 이 기록은 **실제 브로커 준비나 전체 연결 성공 근거에서 제외**합니다. 이후 Redis 임대가 자연 만료하여 `capabilities:false`가 된 것을 확인했고 상태를 강제로 수정하지 않았습니다. 해당 요청은 `CANCELLED / device=NONE`이었으며 증명·승인·터널 보고가 없었습니다.

이후 readiness를 직접 보고하는 진단은 금지하고, 실제 네이티브 프로세스의 연속 주기 보고·서버 로그·Redis 읽기 조회만 사용합니다. 실제 브라우저 후속 등록·수집·철회 결과는 별도의 최종 인수 기록에서 확인해야 합니다.

## 실제 단말 증명·웹 승인·수집·등록

브라우저 클립보드 전달 문제와 나머지 실연결 흐름을 분리하기 위해, 사용자 승인을 받은 정상 API 대체 전달 시험을 실시했습니다. 무작위 등록 생성값으로 noble 공개키를 만들고 정상 로그인 세션으로 등록 요청을 발급한 뒤 제품 bootstrap의 표준 입력으로 전달했습니다. 인증·서명·기한·터널 준비 검사는 우회하지 않았습니다. **이 결과를 브라우저 명령 복사부터 전체 경로가 성공한 것으로 표시하지 않습니다.**

실제 요청의 서버 관측 시각(UTC)은 다음과 같습니다.

| 단계 | 시각 | 관측 |
| --- | --- | --- |
| 단말 등록 증명 | 2026-11-06 22:51:48 | challenge·prove HTTP 200, PostgreSQL 단일 소비 |
| Chrome 단말 승인 | 22:52:42 | APPROVED, 이어서 브로커 authorize 200 |
| 실제 터널 준비 | 22:52:44 | epoch 1, READY, 화면 확인 단계 4개 |
| Chrome 수집 요청 | 22:53:12 | 선택한 Kubernetes 대상의 서명된 작업 생성 |
| 수집 완료 | 22:53:13 | Kubernetes v1.37.2+k3s1, 노드 1, 네임스페이스 5, 스토리지 클래스 1 |
| Chrome 환경등록 | 22:53:28 | 등록 ID·수집 digest 저장, collectionPending=false |

실제 양측 WireGuard의 핸드셰이크와 송수신 바이트는 네이티브 담당이 별도 조회했고 `public/tunnel-evidence.json`에 비밀키 없이 기록했습니다. 제어 서버의 요약·감사 이벤트는 `public/control-registration-evidence.json`에 기록했습니다. 고객 클러스터의 자원을 생성하거나 변경한 작업은 없습니다.

## 브로커 중단과 재접속

실제 브로커를 중단하고 기존 권한 임대가 자연 만료되도록 두었습니다. 마지막 임대 만료는 22:55:54 UTC, Chrome 재접속 요청 접수는 22:56:06 UTC였습니다. 재기동 후 REKEYING은 수락됐지만 READY가 409로 거절되는 실제 결함을 발견했습니다.

새 세대 REKEYING에서 이전 세대의 키 만료값을 보존하여 새 세대의 첫 READY를 수명 연장으로 잘못 거절한 것이 원인이었습니다. 단위시험에서 `KEY_LIFETIME_CHANGED`를 재현한 뒤, 유효한 새 세대로 넘어가는 비READY 전환에서만 이전 수명값을 초기화했습니다. 같은 세대 REKEYING과 heartbeat를 이용한 수명 연장은 계속 거절하도록 시험했습니다.

수정 서버 재시작 후 실제 네이티브의 다음 교환에서 epoch 24·READY로 복구됐고, 기존 등록 ID가 유지됐습니다. 실패 재시도 때문에 키 세대 번호가 증가했으며 상태를 강제로 READY로 설정하지 않았습니다. 증거는 `public/control-reconnect-evidence.json`에 보관했습니다.

## 실제 철회

Chrome에서 연결 해제를 실행한 22:59:48 UTC에 서버 revoke 200 직후 브로커 authorize가 403으로 전환됐습니다. 22:59:51 UTC에는 게이트웨이 정리 보고가 수락됐으며, 서버 스냅샷은 REVOKED·NONE과 server/gateway/local 정리 확인 true를 반환했습니다. 고객 자원 생성은 수행하지 않았으므로 resources 정리는 해당 기능이 만든 자원이 없다는 의미입니다. 기존 환경등록 ID는 유지됐습니다. 서버 증거는 `public/control-revoke-evidence.json`에 보관했습니다.

## 별도 실제 Chrome 복사 경로 재시험

클립보드 API 가상화 영향을 분리하기 위해 새 실제 Chrome 탭에서 제품 명령 복사 버튼을 누르고, OS 붙여넣기로 보호된 로컬 전달 폼·FIFO에 전달했습니다. 새 요청은 정상 제품 화면에서 발급됐으며 앞 절의 API 대체 발급과 별개입니다. 요청 ID 일치만 확인하고 등록 생성값은 모델 출력이나 로그에 노출하지 않았습니다.

서버 발급은 23:01:16.353 UTC, 단말 challenge 200은 23:01:41.354 UTC, prove 200은 23:01:41.385 UTC였습니다. 발급 후 약 25초 안에 실제 등록 증명이 접수됐습니다. Chrome 승인 200은 23:02:21.603 UTC, 실제 터널 READY는 23:02:23 UTC에 관측됐습니다. 양측 실제 WireGuard epoch 1·핸드셰이크·양방향 확인은 `public/clipboard-native-evidence.json`으로 별도 보관했습니다.

화면 잠금 동안 수집·등록 API를 대신 호출하지 않았으며, 사용자가 잠금을 해제한 뒤 같은 Chrome 요청으로 직접 이어서 진행했습니다. Chrome 수집 요청은 23:05:59.648 UTC에 200, 실제 수집 완료는 23:06:02.048 UTC, Chrome 환경등록은 23:06:14.453 UTC에 200으로 확인했습니다. 최종 상태는 APPROVED·READY·epoch 1·COLLECTED·collectionPending=false이며 동일 요청에서 등록 ID가 생성됐습니다. 실제 수집 요약은 Kubernetes v1.37.2+k3s1, 노드 1, 네임스페이스 5, 스토리지 클래스 1입니다.

최종 연속 흐름의 서버 감사 기록과 자격증명 없는 요약은 `public/browser-full-flow-evidence.json`, 실제 네이티브 터널 증거는 `public/clipboard-native-evidence.json`에 보관했습니다. 이 요청에는 대체 API 발급이나 수동 준비·터널 상태 보고를 사용하지 않았습니다. 시험용 로컬 전달 폼과 FIFO는 실제 붙여넣은 제품 명령을 비밀 노출 없이 단말로 전달하는 역할만 수행했습니다.
