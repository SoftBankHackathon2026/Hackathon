# 배포 묶음과 최초 실행

이 디렉터리는 배포 준비 도구입니다. 운영 주소·제품 서명 개인키·고객 인증정보를 저장하지 않습니다. 주소·아키텍처별 실행 파일 해시·빌드 시 검증키가 준비되지 않으면 최초 실행과 배포 검증은 실패하도록 구현했습니다. 실제 배포·Linux 단말 통합 검증 완료를 의미하지 않습니다.

## 신뢰 순서

1. 일반 HTTPS로 게시자의 초기 Bash 스크립트를 받습니다.
2. 초기 스크립트에 내장한 아키텍처별 SHA-384로 최소 실행 파일을 검사합니다.
3. 실행 파일은 빌드에 내장한 ML-DSA-65·Ed25519 공개키로 배포 목록의 두 서명을 모두 확인합니다.
4. 승인한 배포 목록에 포함된 모든 파일의 길이와 SHA-384를 확인합니다.
5. 검증된 실행 파일이 검증된 설정·공개 인증기관·수집기를 사용합니다.

초기 HTTPS 페이지·스크립트 자체가 변조된 상황까지 이 방식으로 해결하지는 않습니다. 운영 서명키와 단말·서버 인증키는 분리해야 합니다. 개인키를 이 저장소나 배포 묶음에 포함하지 않습니다.

## 아키텍처별 묶음

게시자 주소 아래 `linux-amd64/`, `linux-arm64/` 각각에 다음 파일을 준비합니다.

| 파일 | 내용 |
| --- | --- |
| `migration-connector` | 해당 Linux 아키텍처용 Rust 실행 파일. 운영체제 OpenSSL에 의존하지 않는 검증된 OpenSSL 포함 빌드 필요 |
| `connector.json` | 공개 제어 주소·키 설정 중계 주소·공개 작업 검증키·제한된 프로그램 경로 등 설정 |
| `root-ca.pem` | 키 설정 중계 서비스의 공개 신뢰 인증기관 |
| `inventory.pyz` | 읽기 전용 Kubernetes 수집기. Python 표준 라이브러리만 사용 |
| `migration-net-helper`·`net-policy.json` | 관리자 승인 설치를 제공할 때 함께 포함하는 보조 실행 파일과 고객용 네트워크 정책. 두 파일도 반드시 이중서명 목록에 포함 |
| `manifest.json` | 버전·순번·만료 시각·파일명·길이·해시와 두 서명 |

설정에는 고객 비밀을 넣지 않습니다. 설정의 상대 경로는 검증된 묶음 디렉터리를 기준으로 해석해야 합니다. 묶음 경로·파일은 현재 사용자 소유여야 하며 심볼릭 링크·외부 쓰기 가능 파일을 거부합니다. 배포 목록의 파일명은 경로 구분자 없는 제한된 이름만 허용합니다. 묶음은 최대 32개 파일, 파일당 256MiB, 전체 512MiB로 제한합니다.

수집기는 다음 명령으로 만듭니다. 같은 소스는 동일한 아카이브를 만듭니다. 기존 출력 파일은 덮어쓰지 않습니다.

```sh
python3 migration/connection/releases/build_inventory.py /reviewed/output/inventory.pyz
```

## 서명·게시 준비

Ubuntu 24.04의 기본 OpenSSL은 이 연결 프로필의 실행 기준이 아닙니다. `releases/Dockerfile`은 Ubuntu 24.04 환경에서 검증한 OpenSSL 3.5 소스를 정적 라이브러리로 만들고 Rust 실행 파일에 연결합니다. `ldd`에 `libssl`·`libcrypto` 동적 의존성이 남으면 빌드를 실패시킵니다. 출력은 실행 파일 세 개와 `inventory.pyz`, SHA-384 목록이며, 운영 설정이나 서명 개인키는 출력하지 않습니다.

빌드 시에는 다음 자료를 직접 준비해야 합니다. 이 저장소에는 검증하지 않은 해시나 다운로드 주소를 대신 넣지 않았습니다.

- OpenSSL 3.5 소스 압축 파일과 별도 확인한 SHA-256. 압축 파일은 빌드 문맥인 `migration/connection/` 안의 검토한 임시 위치에 둡니다. 저장소에는 커밋하지 않습니다.
- 검토한 Rust 공식 이미지 참조와 Ubuntu 24.04 이미지 참조. 변경 불가능한 이미지 해시로 지정합니다. Rust 이미지는 `rust:1.93-bookworm` 계열처럼 Ubuntu 24.04보다 glibc 기준이 낮은 배포판을 사용하고 실제 실행을 확인합니다. `trixie` 계열 도구는 Ubuntu 24.04보다 새 glibc를 요구할 수 있으므로 그대로 복사하지 않습니다.
- 아래의 공개 검증키와 최소 배포 순번.

```sh
docker buildx build --platform linux/amd64 \
  -f migration/connection/releases/Dockerfile \
  --build-arg RUST_IMAGE=<검토한-Rust-이미지와-해시> \
  --build-arg UBUNTU_IMAGE=<검토한-Ubuntu-24.04-이미지와-해시> \
  --build-arg OPENSSL_TARBALL=<빌드문맥-내-소스파일> \
  --build-arg OPENSSL_SHA256=<별도-확인한-64자리-해시> \
  --build-arg MIGRATION_RELEASE_MLDSA_PUBLIC_KEY \
  --build-arg MIGRATION_RELEASE_ED25519_PUBLIC_KEY \
  --build-arg MIGRATION_MIN_RELEASE_SEQUENCE \
  --output type=local,dest=/reviewed/output/linux-amd64 \
  migration/connection
```

arm64는 플랫폼과 출력 디렉터리를 함께 바꿔 별도로 빌드합니다. 이 Dockerfile의 추가 자체는 두 아키텍처 릴리스 빌드·Ubuntu 실행 검증이 완료되었다는 의미가 아닙니다. 공개 검증키는 셸 환경에서 전달해도 되지만 서명 개인키는 빌드 인자·빌드 문맥에 넣지 않습니다.

Rust 빌드 시 다음 환경값이 실행 파일에 포함됩니다. 모두 공개 정보이며, 값이 없는 개발 빌드는 암호 단위 시험은 가능하지만 실제 배포 묶음 실행은 거절합니다.

- `MIGRATION_RELEASE_MLDSA_PUBLIC_KEY`: 주소 안전 Base64, 패딩 없는 1952바이트 공개키
- `MIGRATION_RELEASE_ED25519_PUBLIC_KEY`: 같은 인코딩의 32바이트 공개키
- `MIGRATION_MIN_RELEASE_SEQUENCE`: 허용할 최소 배포 순번

빌드 도구가 포함하는 OpenSSL·Rust 의존성·두 아키텍처 실행 파일을 검증하고 잠근 뒤, 공개 신뢰 자료와 설정을 포함한 묶음을 준비합니다. `sign_manifest.py`는 외부에서 지정한 두 개인키 파일로 OpenSSL 서명을 생성합니다. 실제 키 값은 명령 인자·결과·배포 목록에 포함하지 않습니다.

```sh
python3 migration/connection/releases/sign_manifest.py \
  --bundle /reviewed/output/linux-amd64 \
  --sequence 1 --expires-at-ms <검토한-정수-만료시각> \
  --mldsa-key /private/signing/release-mldsa.pem \
  --ed25519-key /private/signing/release-ed25519.pem \
  --openssl /reviewed/openssl-3.5-or-newer/bin/openssl
```

서명 대상은 `migration/release/v1`과 줄바꿈, 정규화한 `payload`의 결합입니다. 배포 목록은 알 수 없는 필드·중복 필드를 거부하고 유효기간을 검사합니다. 단말은 마지막 검증 배포 순번을 사용자 전용 상태에 기록하며 더 낮은 순번으로 되돌아가지 않습니다. 새 검증키 전환은 아직 자동화하지 않았으므로 신뢰키 변경을 기존 배포 갱신과 동일하게 처리하지 않습니다.

양쪽 실행 파일의 실제 해시와 게시 주소를 확정한 뒤 `render_bootstrap.py`로 초기 스크립트를 생성합니다. 도구는 HTTPS 외 주소, 인증정보·셸 구문이 포함된 주소, 잘못된 해시를 거부합니다. 기존 파일을 덮어쓰지 않습니다.

```sh
python3 migration/connection/releases/render_bootstrap.py \
  --base-url https://<승인한-게시호스트>/<고정-배포경로> \
  --amd64-sha384 <실제-96자리-해시> \
  --arm64-sha384 <실제-96자리-해시> \
  --output /reviewed/output/i
```

위 값은 설명용 자리표시자입니다. 운영값을 추측해 만들거나 게시하지 않습니다. 서비스는 게시된 초기 스크립트 주소·배포 버전·검증 자료가 준비된 후에만 명령 복사를 활성화해야 합니다.

## 단말 동작과 현재 제한

초기 스크립트는 등록 입력을 즉시 비공개 셸 변수로 옮기고 환경변수를 제거합니다. 22자리 요청 식별자와 43자리 생성값만 허용하며, 단말 프로그램에는 Bash 내부 `printf`에서 전용 표준입력 파이프로 전달합니다. 생성값을 자식 프로세스의 환경변수나 외부 명령 인자로 다시 전달하지 않습니다. 셸 추적 출력은 끕니다. 최초 복사 명령의 셸 기록 노출 위험은 원래 계획의 수용 범위로 남습니다.

Ubuntu 24.04, amd64/arm64, curl·kubectl·Python 3을 요구합니다. 네트워크 보조 프로그램·정책·관련 패키지가 없으면 단말 증명과 웹 승인 후 터미널에서 설치 내용을 안내합니다. 동의한 경우 `sudo`의 관리자 인증을 거쳐 필요한 패키지와 서명된 보조 프로그램·정책을 설치하고 같은 요청을 계속합니다. 관리자 비밀번호는 연결 프로그램이 읽거나 저장하지 않습니다. 터미널이 없거나 동의를 거절하면 중단합니다.

이 기능을 게시하려면 해당 아키텍처의 `migration-net-helper`와 고객용 `net-policy.json`을 배포 묶음에 함께 넣고 서명하십시오. 정책의 `role`은 `client`, `uid`는 설치할 사용자 번호를 나타내는 자리표시자 `0`으로 작성합니다. 실제 UID는 관리자 승인 설치 중 채웁니다. 연결 상대·주소·포트는 운영자가 고객 네트워크에 맞게 지정하며 자동으로 추측하지 않습니다. 기존 정책이 다르면 덮어쓰지 않습니다. 두 파일이 없는 이전 배포 묶음은 사전 설치된 단말에서 계속 사용할 수 있습니다.

운영 초기 스크립트는 systemd로 부팅한 Ubuntu에서 실행합니다. 웹 승인과 관리자 동의 후 부팅 시 시작하는 단말 서비스를 등록하고, 양방향 터널 확인과 환경 후보 전달 후 셸을 반환합니다. systemd가 없는 격리 시험에서는 명시적으로 `--foreground`를 사용합니다. 기존 단말의 전환과 서비스 배포 갱신은 [단말 실행 안내](../agent/README.md#실행)를 따릅니다.

묶음은 `~/.local/state/migration-connector/release.*/bundle/`에 0700 디렉터리로 보관합니다. 초기 실행 파일만 정리하고 검증된 묶음은 서비스 실행을 위해 유지합니다. 배포 목록은 서비스 재시작 시에도 검증하므로 만료 전에 기존 요청으로 갱신합니다. 되돌릴 때는 이전 동작을 복원한 묶음을 더 높은 순번으로 다시 서명합니다.

등록 증명의 60초 기한에는 다운로드 시간이 포함됩니다. 시간이 부족하면 등록을 연장하거나 우회하지 않고 새 복사를 안내합니다. 관리자 승인·패키지 설치는 단말 증명과 웹 승인 후 수행하므로 이 60초 기한을 늘리지 않습니다. 설치 도중 웹에서 승인을 철회하면 설치가 끝난 뒤 상태를 다시 확인하고 터널을 열지 않습니다. 이미 관리자가 승인하여 설치한 패키지는 유지되며 연결 해제가 패키지 삭제를 뜻하지는 않습니다. 게시 서버 성능과 실제 두 아키텍처의 60초 내 실행 가능 여부는 Linux 인수 시험에서 확인해야 합니다.
