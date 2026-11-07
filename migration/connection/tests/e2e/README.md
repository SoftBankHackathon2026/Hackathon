# 실제 연결 수동 인수 시험용 제어 서버

이 구성은 기존 플랫폼 로그인·PostgreSQL·Redis·OpenSSL 3.5와 실제 연결 API를 구동합니다. 가짜 단말 상태, 메모리 저장소, 준비 조건 우회는 사용하지 않습니다. 원격 고객 Kubernetes 변경은 수행하지 않으며 실제 단말과 브라우저는 별도로 실행합니다.

시험 파일은 `/private/tmp/onprem-live-e2e`에 보관합니다. 최상위 디렉터리는 0700, 개인키·서비스 인증값·DB 비밀번호·브라우저 계정과 쿠키는 0600입니다. `release/`에는 공개 배포물만 배치하며 정적 웹서버 읽기용으로 디렉터리 0755/파일 0644를 사용합니다. 비밀정보와 실제 고객 kubeconfig는 저장소에 넣지 않습니다.

## 준비

이 디렉터리의 인증서·배포 준비 도구로 아래 파일을 먼저 준비합니다.

- `job-signing-key.pem`: 실제 ML-DSA-65 작업 서명키
- `runtime-token`: 브로커 서비스 인증값
- `control.crt`, `control.key`, `ca.crt`: `control.test` HTTPS와 신뢰 인증기관
- `release/releases/live-e2e-1/bootstrap.sh`와 서명 배포물: 처음 서버를 시작할 때 없어도 되지만 명령 발급은 비활성으로 유지됩니다.

저장소 루트에서 실행합니다.

```bash
python3 migration/connection/tests/e2e/control_stack.py prepare
python3 migration/connection/tests/e2e/control_stack.py build
python3 migration/connection/tests/e2e/control_stack.py up
python3 migration/connection/tests/e2e/control_stack.py account
```

`account`는 정상 회원가입 후 로그아웃·재로그인하여 `live-e2e-team` 소유자 권한을 검사합니다. 로그인 값은 `browser-credentials.json`, 발급된 시험 세션은 `browser-session.cookies` 보호 파일에만 저장합니다. 계정이나 세션을 DB에 직접 주입하지 않습니다. 별도 접속 PIN은 선택 설정이며 이 시험에서는 기존 LOGIN 모드의 사용자 인증을 사용합니다. 기존 `ONEPORT_FLEET_CONFIG` 설정으로 환경·이관 목록이 빈 `mode:live` 자료를 사용하므로 기본 시연 클러스터를 실제 결과처럼 표시하지 않습니다. 새 연결로 등록한 환경은 별도 실제 등록 목록에 나타납니다.

Vite 개발 서버의 `ONEPORT_API_TARGET=http://127.0.0.1:18320`, 브라우저 주소는 `http://localhost:4327`입니다. 서버의 허용 Origin도 이 주소를 포함합니다. backend 포트 18320과 HTTPS 확인 포트 18443은 호스트 루프백에만 열립니다.

## 네트워크와 활성화

`mc-live-e2e` Docker 네트워크는 `172.30.87.0/24`입니다. 기존 동명 네트워크의 범위가 다르면 스크립트가 중단됩니다.

| 구성요소 | 주소 |
| --- | --- |
| 플랫폼 backend | 172.30.87.10 |
| HTTPS / `control.test` | 172.30.87.11 |
| PostgreSQL | 172.30.87.12 |
| Redis | 172.30.87.13 |
| 별도 실제 브로커 예약 | 172.30.87.20 |
| 별도 실제 단말 예약 | 172.30.87.21 |

브로커는 `https://control.test/api/v1/connection-runtime`, 단말은 `https://control.test/api/v1/connections`를 사용합니다. 인증기관 신뢰를 설치하고 인증서 검증을 켜야 합니다. 브로커용 상호 인증 인증기관은 HTTPS 인증기관과 분리합니다.

서명 배포물 생성 후 `up`을 다시 실행하면 실제 bootstrap 파일 SHA-384가 서버 설정에 반영됩니다. 실제 브로커가 일치하는 조직·게이트웨이·릴리스·해시를 보고하기 전까지 capabilities는 계속 false입니다. `/health`를 시험 스크립트에서 대신 호출하거나 상태를 강제로 READY로 만들지 않습니다.

## 실제 Chrome 복사 명령의 임시 전달

이 시험 환경에서는 자동화 도구의 클립보드 읽기 결과가 빈 값이어서 해당 기능만으로 복사 내용을 확인할 수 없었습니다. `clipboard_transfer.py`는 Chrome에서 실제로 붙여넣은 값을 로컬 비공개 FIFO(일반 파일에 저장하지 않는 프로세스 간 파이프)로 전달하는 시험 보조 도구입니다. 제품의 명령 발급이나 단말 인증을 대체하지 않으며, 화면 복사부터 등록 완료까지 성공했다는 증거도 아닙니다.

먼저 별도 터미널 두 개에서 다음 실행기와 전달 서버를 시작합니다. 실행기는 원래 플랫폼이 발급한 명령의 정확한 형식과 서명 배포 주소를 확인한 뒤 격리 Ubuntu 단말의 표준입력으로만 전달합니다.

```bash
python3 migration/connection/tests/e2e/run_enrollment.py
```

```bash
python3 migration/connection/tests/e2e/clipboard_transfer.py
```

실행기가 대기 중인 상태에서 플랫폼 화면의 정상 복사 버튼을 누릅니다. 같은 Chrome에서 `http://localhost:18327`을 열고 비밀번호 입력칸에 실제 붙여넣기한 뒤 한 번 전달합니다. 명령 원문을 대화, 개발자 도구, 일반 파일에 옮기거나 입력칸 값을 자동화 도구로 읽지 않습니다. 초대 유효시간 안에 수행해야 하며, 이미 취소되거나 교체된 명령이 거절된 결과는 정상 등록 성공으로 기록하지 않습니다.

전달 서버는 루프백에만 열리고 동일 출처·일회용 확인값을 검사합니다. 본문 크기와 읽기 시간을 제한하고, 실제 열린 파이프의 소유권·0600 권한·파일 종류를 다시 확인합니다. 명령 본문을 기록하거나 응답에 되돌리지 않습니다. 한 번 전달한 뒤에는 두 시험 프로세스를 종료하고, 새로운 시도가 필요할 때 실행기를 다시 준비합니다. 실행 중인 전달 서버는 소스 수정만으로 갱신되지 않습니다.

`test_clipboard_transfer.py`는 실제 전달 서버와 분리된 임의 루프백 포트와 임시 파이프로 검사합니다. 파일 교체·재전송·잘못된 출처·잘못된 요청 거절을 검증하지만 제품 전체 흐름 시험을 대신하지 않습니다. 이 시험을 실행하려면 로컬 소켓 생성 권한이 필요합니다.

## 정리

브라우저와 단말·브로커 시험을 마친 후 다음 명령으로 이 구성의 컨테이너와 전용 DB 볼륨을 제거합니다.

```bash
python3 migration/connection/tests/e2e/control_stack.py down
```

이 명령은 다른 담당 구성요소가 사용할 수 있는 `mc-live-e2e` 외부 네트워크와 보호된 임시 증거 파일을 삭제하지 않습니다. 시험 전체 종료 때 소유한 단말·브로커·네트워크와 비밀정보를 별도로 정리합니다. 이 구성은 운영 배포 사양이 아니며 공개 주소로 노출하지 않습니다.
