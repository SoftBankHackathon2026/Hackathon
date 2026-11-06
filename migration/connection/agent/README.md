# 네이티브 단말 및 키 브로커

이 모듈은 단말 등록 증명, 웹 승인 대기, 상호 TLS 인증, WireGuard 키 교체, 서명된 읽기 전용 Kubernetes 수집을 수행합니다. 기존 플랫폼 설치기와 Keycloak/OpenBao 구성은 호출하지 않습니다. 배포용 서명된 릴리스와 root 관리 네트워크 정책을 먼저 준비해야 합니다. 저장소의 예시를 복사하는 것만으로 운영 연결이 활성화되지는 않습니다.

## 실행

다음 세 명령이 운영 진입점입니다. `REQUEST_ID`는 공개 요청 식별자이며 seed가 아닙니다. 최초 `run`의 표준입력은 검증된 bootstrap이 비공개 파이프로 `request_id.seed`를 전달합니다. 이를 명령 인수, 환경변수, 셸 기록에 넣지 마십시오.

```sh
/path/to/verified-bundle/migration-connector run /path/to/verified-bundle/connector.json
/path/to/verified-bundle/migration-connector resume /path/to/verified-bundle/connector.json REQUEST_ID
/usr/local/libexec/migration-key-broker /etc/migration-connector/broker.json
```

단말은 현재 포그라운드 프로세스입니다. 터미널 종료 시 계속 실행되는 서비스는 설치하지 않습니다. 다시 연결할 때는 동일 사용자로 `resume`을 실행하며 새 초대 seed를 요구하지 않습니다. 기존 상태가 없거나 장치 승인이 철회되면 새 등록이 필요합니다. 상태는 `$HOME/.local/state/migration-connector/REQUEST_ID`에 디렉터리0700·파일0600으로 저장합니다. 동시 실행은 파일 잠금으로 거절합니다. 승인 대기 중 재시작하면 확인 코드를 다시 표시합니다.

## 서명된 단말 설정

`connector.json`은 이 이름 그대로 릴리스 이중서명 대상에 포함되어야 합니다. 아래 대문자 값은 운영자가 교체할 자리표시자입니다.

```json
{
  "public_api_url": "https://PLATFORM_HOST/api/v1/connections",
  "broker_host": "BROKER_CERTIFICATE_DNS_NAME",
  "broker_port": 8443,
  "root_ca": "root-ca.pem",
  "job_public_key": "BASE64URL_RAW_MLDSA65_JOB_PUBLIC_KEY",
  "collector_archive": "inventory.pyz",
  "helper_path": "/usr/local/libexec/migration-net-helper"
}
```

`root-ca.pem`과 `inventory.pyz`는 설정 파일과 같은 서명된 묶음 안의 고정 파일입니다. 작업 서명키는 서버 설정의 작업 서명키와 일치해야 합니다. 공개 API는 HTTPS만 허용합니다. 작업 `poll/discovered/inventory`는 정책에 고정된 WireGuard 내부 주소의 HTTP 서비스를 통해서만 전달합니다. 해당 HTTP는 WireGuard 암호화 및 두 단말의 방화벽으로 보호됩니다.

## root helper 설치 및 네트워크 정책

현재 구현은 **네트워크 이름공간 하나에 관리 인터페이스 하나·활성 연결 하나**를 지원합니다. 브로커를 테넌트마다 분리하고 전용 컨테이너나 네트워크 이름공간에서 실행하십시오. 여러 테넌트의 경로를 한 helper에 공유하는 배포는 지원하지 않습니다. 양쪽의 WireGuard UDP endpoint와 포트는 운영자가 미리 도달 가능하게 설정해야 하며 자동 NAT 중계는 구현하지 않았습니다.

관리자가 검증된 `migration-net-helper`를 `/usr/local/libexec/migration-net-helper`에 root:root0755로 설치합니다. `/etc/migration-connector`는 root 소유0755, `net-policy.json`은 root:root0644로 설치합니다. helper는 Linux `CAP_NET_ADMIN`과 `/usr/sbin/ip`, `/usr/sbin/nft`, `/usr/bin/wg`를 요구합니다. 단말 일반 사용자에게 허용하는 sudoers 항목은 다음 한 명령이며 추가 인수를 허용하지 않습니다.

```sudoers
CONNECTOR_USER ALL=(root) NOPASSWD: /usr/local/libexec/migration-net-helper ""
```

정책 예시는 JSON 구조를 보여줍니다. IP와 사용자 숫자는 각 격리 환경에 맞게 대체하십시오. 양쪽 정책은 local/peer 주소가 서로 반대이며 job/probe 포트가 같아야 합니다.

```json
{
  "uid": 1000,
  "interface": "mc0",
  "local_address": "10.240.0.2",
  "peer_address": "10.240.0.1",
  "endpoint": "192.0.2.10:51820",
  "listen_port": 51820,
  "job_port": 18080,
  "probe_port": 18081,
  "role": "client"
}
```

브로커 정책은 `role: "gateway"` 및 실행 uid를 사용합니다. helper는 표준입력의 고정 JSON 요청으로만 check/configure/status/refresh/remove/reset을 받습니다. 원격 작업은 helper 인수, 주소, 인터페이스, 파일 경로를 정할 수 없습니다. `reset`은 재시작 시 root 소유 관리 상태가 남긴 인터페이스만 제거하며 소유 상태가 없는 인터페이스는 거절합니다.

방화벽은 고정 peer의 작업 TCP와 양방향 확인 UDP만 허용하고 관리 인터페이스의 나머지 입출력 및 모든 forwarding을 차단합니다. 허용 lease는 최대60초이며 갱신해도 키 절대 만료시각30분을 넘지 않습니다. 프로세스 중단이나 승인 확인 실패 시 lease가 소멸하여 트래픽이 차단됩니다. 인터페이스 제거를 확인하지 못하면 차단 규칙을 남기고 정리 완료로 보고하지 않습니다.

## 브로커 설정

```json
{
  "listen": "0.0.0.0:8443",
  "certificate": "/etc/migration-connector/gateway-chain.pem",
  "private_key": "/etc/migration-connector/gateway-key.pem",
  "runtime_api_url": "https://PLATFORM_HOST/api/v1/connection-runtime",
  "public_api_url": "https://PLATFORM_HOST/api/v1/connections",
  "runtime_token_file": "/etc/migration-connector/runtime-token",
  "tenant_id": "CONFIGURED_TENANT_ID",
  "gateway_id": "CONFIGURED_GATEWAY_ID",
  "helper_path": "/usr/local/libexec/migration-net-helper",
  "release_version": "PUBLISHED_RELEASE_VERSION",
  "bootstrap_sha384": "96_LOWERCASE_HEX_CHARACTERS_OF_PUBLISHED_BOOTSTRAP"
}
```

브로커 설정·인증서·키·토큰은 root 소유이며 그룹/타인 쓰기를 금지합니다. 키와 토큰 파일은0600으로 제한합니다. 인증서는 단말 묶음의 CA로 검증되며 ML-DSA-65 서명과 broker_host 이름을 만족해야 합니다. 서비스 토큰은 서버의 고정 테넌트·게이트웨이 설정과 함께 맞춥니다. 브로커가 준비된 후20초마다 health를 갱신하지만 실제 기능 노출에는 서버의 서명 릴리스 배포 설정 및 저장소 가용성 검사가 함께 필요합니다.

TLS1.3/X25519MLKEM768/ML-DSA-65만 허용합니다. 세션 재개와0-RTT는 사용하지 않습니다. TLS exporter에 테넌트·장치·게이트웨이·양쪽 WireGuard 공개키·연결 세대·주소·서비스 범위·만료를 결합하여32바이트 PSK(미리 공유하는 키)를 만듭니다.25분 경과 시 새 전체 인증을 시작하고 이전 작업 경로를 닫은 후 새 키를 적용합니다. 실패하면 이전 키로 되돌아가지 않습니다.

## 조회와 실패 처리

작업은 `environment.inspect` 서명과 장치·세대·만료를 확인한 후 고정된 `inventory.pyz`만 실행합니다. 임의 셸 명령은 받지 않습니다. 로컬 발견은 최초 READY에서 수행하며 수집은 최대90초, 실행 중10초마다 승인된 터널 poll 및 방화벽 lease 갱신을 수행합니다. 결과 전송 전에 작업 만료를 다시 확인합니다.

연결 오류는 비밀 없는 고정 오류 코드로 표시합니다. 터널 오류는 재협상을 시도하고, 프로세스가 종료되면 웹 상태를 확인한 뒤 `resume`으로 복구합니다. 웹 철회 확인 시 관리 인터페이스를 제거하고 로컬 장치키·상태파일을 삭제한 뒤 정리 완료를 보고합니다. 오프라인 상태에서는 로컬 정리가 아직 확인되지 않았다는 서버 표시가 유지될 수 있습니다. lease 차단과 실제 파일/인터페이스 제거는 별도 상태입니다.

## 빌드 및 검증

Rust1.93 이상과 OpenSSL3.5 이상 개발 헤더가 필요합니다. 운영 릴리스는 계획서의 고정 OpenSSL 버전으로 빌드·서명해야 하며 개발 머신의 버전을 운영 보증으로 취급하지 않습니다.

```sh
OPENSSL_DIR=/path/to/pinned-openssl cargo build --locked --release
OPENSSL_DIR=/path/to/test-openssl cargo test --locked --all-targets
cargo fmt --all --check
```

릴리스 빌드 시 `MIGRATION_RELEASE_MLDSA_PUBLIC_KEY`, `MIGRATION_RELEASE_ED25519_PUBLIC_KEY`, `MIGRATION_MIN_RELEASE_SEQUENCE`를 공개 신뢰값으로 주입하십시오. 없으면 릴리스 검증이 실패합니다. 자세한 서명/배포 절차는 [릴리스 문서](../releases/README.md)를 참조하십시오. `examples/sign-fixture.rs`와 `crypto-vector`는 공개된 고정 시험 seed만 사용하는 상호운용 시험용이며 운영 키로 사용하면 안 됩니다.

자동 시험은 고정 noble ML-DSA 자료 검증, 실제 로컬 상호 TLS 및 양측 exporter 일치, 고전 X25519만 제안한 연결 거절, 릴리스 위변조·만료·이중서명·다운그레이드·심볼릭 링크 거절을 다룹니다. WireGuard 커널 시험은 [터널 시험 도구](../tests/linux/)의 결과를 별도로 확인해야 합니다. macOS 암호 시험과 Debian Linux 빌드 결과는 Ubuntu24.04 전체 운영 배포 검증을 대체하지 않습니다.
