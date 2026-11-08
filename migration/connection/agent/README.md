# 네이티브 단말 및 키 브로커

이 모듈은 단말 등록 증명, 웹 승인 대기, 상호 TLS 인증, WireGuard 키 교체, 서명된 읽기 전용 Kubernetes 수집을 수행합니다. 기존 플랫폼 설치기와 Keycloak/OpenBao 구성은 호출하지 않습니다. 배포용 서명된 릴리스와 root 관리 네트워크 정책을 먼저 준비해야 합니다. 저장소의 예시를 복사하는 것만으로 운영 연결이 활성화되지는 않습니다.

## 실행

플랫폼에서 복사한 초기 설치 명령을 Ubuntu 24.04의 일반 사용자 터미널에서 실행합니다. 웹 승인과 관리자 설치 동의를 마치면 운영체제의 서비스 관리자(systemd)가 같은 일반 사용자로 `migration-connector.service`를 실행합니다. 양방향 터널이 확인되면 초기 명령이 셸을 반환하고, 서비스가 환경 후보를 계속 탐색합니다. 처음 시작은 최대 90초 동안 기다립니다.

```sh
systemctl status migration-connector
sudo systemctl start migration-connector
sudo systemctl stop migration-connector
journalctl -u migration-connector
```

서비스는 부팅 시 시작하고 비정상 종료 후 재시작합니다. 관리자가 중지한 서비스는 단말에서 다시 시작해야 합니다. 서버가 단말 승인을 철회하면 작업 요청과 새 터널을 차단합니다. 단말은 철회를 확인한 뒤 관리 인터페이스와 인증 상태를 정리합니다. 정리 보고가 서버에 도착하지 않으면 웹에서 단말 정리가 확인될 때까지 대기 상태가 표시됩니다.

웹에서 교체를 승인한 연결이 이 단말의 서비스에서 실행 중이면 새 설치가 해당 서비스만 중지하고 새 요청을 시작합니다.

전경 실행을 이미 종료한 단말이나 서비스 배포를 갱신할 단말은 **기존 등록과 동일한 사용자**로 새 서명 배포의 초기 스크립트를 실행하고 `--resume REQUEST_ID`를 전달합니다. 공개 요청 식별자와 기존 인증키를 사용합니다. 기존 인증 상태가 없거나 승인이 철회되면 새 등록이 필요합니다.

```sh
curl -qfsS --proto '=https' https://APPROVED_RELEASE_HOST/RELEASE/bootstrap.sh | bash -s -- --resume REQUEST_ID
```

직접 실행하는 단말 래퍼의 `run` 명령도 서비스 시작과 터널 확인 후 종료합니다. 전경 시험 실행은 중단 신호를 받으면 자기 터널을 정리합니다. 강제 종료로 남은 단말 터널은 새 연결 시작 때 기존 통신 허용 시간이 끝났고 저장된 상대가 실제 인터페이스와 일치하는 경우 정리합니다. 살아 있는 다른 연결을 즉시 교체하려면 웹에서 해당 연결의 교체를 승인합니다. 같은 요청의 동시 실행은 파일 잠금으로 차단합니다. 전경 실행이 필요한 격리 시험은 초기 스크립트에 `--foreground`를 지정하거나 검증된 묶음에서 아래 명령을 사용합니다.

```sh
/path/to/verified-bundle/migration-connector resume /path/to/verified-bundle/connector.json REQUEST_ID
/path/to/verified-bundle/migration-connector prepare-host /path/to/verified-bundle
/usr/local/libexec/migration-key-broker /etc/migration-connector/broker.json
```

요청별 상태는 `$HOME/.local/state/migration-connector/REQUEST_ID`의 사용자 전용 파일에 저장합니다. 서비스는 설치 시 설정한 사용자 홈과 Kubernetes 접속 설정 파일 경로를 사용합니다. 서비스가 참조하는 검증 묶음을 유지하고 배포 목록이 만료되기 전에 같은 요청으로 갱신합니다. 웹의 재접속 요청은 전원이 켜져 있고 연결 프로그램이 실행 중인 단말의 응답을 기다립니다.

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

정상적으로 사전 설치된 단말은 네트워크 보조 프로그램 설치를 건너뜁니다. 준비가 안 된 단말은 단말 증명과 웹 승인을 먼저 마친 뒤 터미널에 설치 내용과 연결 상대를 표시합니다. 사용자가 동의하면 `sudo`가 관리자 인증을 직접 받고, Ubuntu 패키지 `iproute2`·`nftables`·`wireguard-tools` 중 없는 항목을 설치합니다. 서명된 배포 묶음의 보조 프로그램과 고객용 정책을 관리자 소유로 설치한 뒤 같은 요청의 터널 연결을 이어갑니다. 사용자 입력은 `/dev/tty`에서 받으므로 초기 등록 생성값의 표준입력과 섞이지 않습니다. 터미널이 없거나 동의를 거절하면 설치하지 않습니다.

이 경로에는 이중서명 배포 목록에 포함된 `migration-net-helper`와 `net-policy.json`이 모두 필요합니다. 게시 정책은 `role: "client"`, `uid: 0`을 사용하고, 설치할 때 실제 실행 사용자의 UID(운영체제 사용자 번호)로 바꿉니다. 터널 상대·주소·포트는 게시자가 고객 네트워크에 맞게 준비해야 합니다. 기존 정책과 충돌하면 자동으로 바꾸지 않습니다. `prepare-host` 명령으로 등록 요청을 발급하기 전에 같은 관리자 승인 절차를 실행할 수도 있습니다.

보조 프로그램은 `/usr/local/libexec/migration-net-helper`에 root:root0755, `/etc/migration-connector`는 root 소유0755, `net-policy.json`은 root:root0644로 설치합니다. helper는 Linux `CAP_NET_ADMIN`과 `/usr/sbin/ip`, `/usr/sbin/nft`, `/usr/bin/wg`를 요구합니다. 단말 일반 사용자에게 허용하는 sudoers 항목은 다음 한 명령이며 추가 인수를 허용하지 않습니다.

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

브로커 정책은 `role: "gateway"` 및 실행 uid를 사용합니다. helper는 표준입력의 고정 JSON 요청으로 네트워크 준비, 상태 확인, 갱신, 연결 및 정리를 수행합니다. 원격 작업은 helper 인수, 주소, 인터페이스, 파일 경로를 정할 수 없습니다. `reset`은 재시작 시 root 소유 관리 상태가 남긴 인터페이스만 제거하며 소유 상태가 없는 인터페이스는 거절합니다.

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
