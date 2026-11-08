# 실제 Ubuntu 네이티브 시험 준비

이 도구는 로컬 Docker의 `mc-live-e2e` 전용 네트워크에서 실제 단말·브로커·WireGuard 커널·제한된 관리자 보조 프로그램을 실행합니다. 서버의 READY 상태를 대신 입력하거나 서명을 생략하지 않습니다. 기본 재현 경로는 실제 웹 화면에서 명령을 복사하고 단말을 승인하는 방식입니다. 아래 정상 API 대체 전달 도구를 사용한 실행은 브라우저 복사·붙여넣기 검증과 구분합니다. 연결 대상 K3s 서버에 프로그램을 설치하거나 서버 설정을 변경하지 않습니다.

## 빌드

`prepare_trust.py`로 만든 공개 릴리스 신뢰키를 `build_native.py`가 읽습니다. 개인키와 kubeconfig는 Docker 빌드에 전달하지 않습니다. 빌드 맥락은 `migration/connection`이며 임시 디렉터리는 그 밖에 두어야 합니다.

```sh
python3 migration/connection/tests/e2e/build_native.py \
  --private-dir /private/tmp/onprem-live-e2e
```

Docker 이미지 `mc-live-e2e-native:local`은 Ubuntu24.04 arm64입니다. OpenSSL3.5.9를 공식 소스에서 SHA256 검증 후 정적 링크합니다. Rust1.93으로 세 실행파일을 빌드하고 실제 상호 TLS·exporter·고전 키 교환 거절 및 릴리스 검증 시험을 실행합니다. 바이너리는 임시 디렉터리의 `bin/`으로 복사됩니다. 기본 빌드는 arm64이며, AMD64는 아래처럼 별도 이미지에서 빌드·시험합니다. Apple Silicon Mac에서는 AMD64 실행이 Docker의 CPU 에뮬레이션(다른 CPU의 명령 실행)에 의존하므로 실제 AMD64 서버 검증과 구분합니다.

- [OpenSSL 공식 배포 목록](https://openssl-library.org/source/)
- [OpenSSL3.5.9 공식 SHA256](https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz.sha256): `603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a`
- [kubectl 공식 Linux 설치 설명](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)
- [kubectl1.37.2 Linux arm64 공식 SHA256](https://dl.k8s.io/release/v1.37.2/bin/linux/arm64/kubectl.sha256): `fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509`
- [kubectl1.37.2 Linux amd64 공식 SHA256](https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl.sha256): `c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4`

## AMD64 추가

기존 ARM64 이미지와 실행 중인 컨테이너를 덮어쓰지 않고 다음을 실행합니다.

```sh
python3 migration/connection/tests/e2e/build_native.py \
  --private-dir /private/tmp/onprem-live-e2e \
  --arch amd64 --image mc-live-e2e-native:amd64
```

AMD64 출력은 `bin/linux-amd64/`, ARM64 출력은 기존 `bin/`에 보관됩니다. 각각 연결 프로그램·중계 프로그램·관리자 네트워크 보조 프로그램을 생성하며, 빌드 중 실제 Rust 시험이 실패하면 게시용 완료 표식을 만들지 않습니다. 로그는 `native-build-amd64.log`와 `native-build-arm64.log`입니다.

두 빌드가 준비되면 [서명·게시 절차](TRUST.md#두-아키텍처-게시)에 따라 새로운 버전에 함께 게시합니다. 일반 설치 스크립트가 단말의 `uname -m` 결과에 따라 `linux-amd64` 또는 `linux-arm64`를 선택합니다. ARM64 전용 모드로 게시한 기존 스크립트는 자동으로 바뀌지 않습니다.

원격 단말의 보조 프로그램·정책이 없는 경우 관리자 승인 설치를 사용할 수 있습니다. `prepare_release.py --client-net-policy /private/approved-client-policy.json`으로 실제 고객 망에 맞는 `role: "client"`, `uid: 0` 정책을 지정하면 각 아키텍처의 보조 프로그램과 정책을 함께 이중서명합니다. 정책은 배포 환경의 입력값이며 제품 코드에 시험 IP를 고정하지 않습니다. 단말 증명·웹 승인 후 터미널의 설치 동의와 `sudo` 인증을 거쳐 설치합니다. 인증기관 신뢰와 중계 서버까지의 포트·라우팅 준비는 [단말 설정](../../agent/README.md)을 따르며 Docker 내부 주소를 원격 네트워크에 그대로 복사하지 않습니다.

실제 관리자 설치 검사는 아래 명령으로 새 임시 Ubuntu 컨테이너에서 실행합니다. 이 도구는 시험 컨테이너의 실제 패키지를 제거한 뒤 실제 관리자 인증·Ubuntu 저장소 설치·파일 권한·제한된 실행 권한을 검사합니다. 기존 컨테이너와 고객 단말은 바꾸지 않습니다. 승인 거부, 터미널 부재, 변조, 기존 정책 충돌도 검사합니다.

```sh
python3 migration/connection/tests/e2e/check_host_install.py \
  --bundle /private/signed-release/linux-arm64 \
  --image <검증할-ARM64-이미지> --arch arm64
```

AMD64는 대응하는 묶음과 이미지, `--arch amd64`를 사용합니다. 설치 시험은 터널 개통 전체 흐름과 별도로 결과를 기록합니다.

## 실행 준비

먼저 제어 서버와 HTTPS 배포 경로를 준비하고 `prepare_release.py`로 실제 바이너리를 이중서명하여 게시합니다. 그 뒤 다음을 실행합니다.

```sh
python3 migration/connection/tests/e2e/start_native.py \
  --private-dir /private/tmp/onprem-live-e2e
```

스크립트는 기존 이름의 컨테이너를 교체하지 않으며, 충돌하면 중단합니다. 브로커와 단말은 별도 네트워크 이름공간에서 각각 `172.30.87.20`, `172.30.87.21`을 사용합니다. 터널 주소는 `10.240.87.1`, `10.240.87.2`입니다. 브로커8443·WireGuard51820·작업18080·양방향확인18081은 호스트에 공개하지 않습니다.

단말 사용자 `connector`는 uid1001이며 고정 관리자 보조 프로그램에만 비밀번호 없는 sudo 권한이 있습니다. 원본 kubeconfig를 읽어 컨테이너 안의 사용자 전용0400 파일로 복사합니다. kubeconfig 내용은 출력하지 않습니다. 컨테이너가 제거되면 그 복사본도 삭제됩니다. 이 시험은 사용자 제공 계정의 기존 권한을 바꾸지 않으며 수집 프로그램은 읽기 요청만 수행합니다.

## 웹 명령 실행

웹에서 실제로 복사한 연결 명령은 다음 프로세스의 **표준입력**으로만 전달합니다. 명령 문자열을 `bash -c` 인수에 넣지 않습니다. 명령에는 짧게 유효한 등록 seed가 있으므로 기록이나 시험 보고서에도 넣지 않습니다.

```sh
docker exec -i -u connector mc-live-e2e-client bash
```

bootstrap은 실제 HTTPS 다운로드·첫 바이너리 SHA384·manifest 이중서명을 검증한 후 실행합니다. Docker 시험 도구는 `--foreground`를 명시하여 전경 모드에서 등록과 터널을 검증합니다. 초기 명령 종료와 서비스 재시작은 systemd가 실행되는 Ubuntu에서 별도로 검증해야 합니다. 필요한 커널 네트워크 기능은 실제 helper가 검사합니다.

브로커의 비밀 없는 오류 로그는 컨테이너 `/var/log/migration-broker.log`에 있습니다. 실제 승인·터널 상태·환경 선택·수집·최종 등록 결과를 화면과 서버 기록으로 확인해야 전체 흐름 시험이 끝납니다. 이 문서나 도구가 존재한다는 사실은 성공 증거가 아닙니다.

## 정상 API 대체 전달 도구와 이번 실행의 구분

데스크톱 클립보드 전달이 지연되거나 이전 명령이 남아 있는 경우, 다음 도구는 복사·전달 구간만 대체하여 실제 단말·암호·터널 흐름을 시험합니다. 이 도구의 성공을 브라우저 복사·붙여넣기 성공으로 기록하지 않습니다.

```sh
node migration/connection/tests/e2e/enroll_native_api.mjs
```

저장소 루트에서 실행하며 프런트엔드의 실제 `@noble/post-quantum` 의존성이 설치되어 있어야 합니다. 보호된 임시 디렉터리의 `browser-session.cookies`에 저장된 정상 로그인 세션으로 일반 공개 `POST /api/v1/connections`를 호출합니다. 매 실행마다 안전한 난수32바이트와 실제 ML-DSA-65 키 쌍을 생성하며, 서버에는 공개키만 보냅니다. 등록 자격60초·등록 증명·웹 확인 코드 승인·상호 TLS·서명 검증·터널 준비 조건을 그대로 적용합니다. DB나 READY 상태를 직접 수정하지 않습니다.

제품과 같은 bootstrap 명령을 메모리에서 만들고 `docker exec -i -u connector ... bash`의 표준입력에만 전달합니다. seed·개인키·명령은 일반 파일, 명령 인수, 도구 출력에 저장하지 않습니다. 제품이 초기 bootstrap에 사용하는 `MIGRATION_ENROLL` 환경변수 전달과 즉시 제거는 원래 설계의 수용된 위험에 해당하며, 네이티브 실행 프로그램에는 전용 표준입력 파이프로 전달됩니다. 키 재료 버퍼는 사용 후 지우고, 출력에는 공개 요청 ID와0600 단말 로그 경로만 표시합니다.

이번 실증의 첫 클립보드 전달에서는 폐기된 요청이 정상 거절됐습니다. 그 뒤 이 도구로 수행한 **API 대체 실행**에서 실제 단말·암호 터널과 Chrome 승인·수집·등록을 확인했습니다. 이후 별도의 **실제 Chrome 복사 실행**에서는 새 명령 복사→보호된 입력 경로와 FIFO→제품 bootstrap→소유 증명→Chrome 승인→실제 터널 개통→Chrome 환경정보 수집→최종 환경 등록까지 성공했습니다. 이 실행의 수집·등록을 대체 API로 수행하지 않았으며, 완료 후 연결은 사용자의 Chrome 확인을 위해 유지했습니다. 첫 API 대체 실행에서 확인한 자동 재접속·새 키 세대 적용·철회와 정리 결과는 별도 요청의 결과로 구분합니다. 두 실행은 요청 ID와 증거 파일을 분리해 보관하며, 25분 정기 키 교체까지 검증했다고 확대하지 않습니다. 단계별 증거와 한계는 [상위 실증 기록](../../docs/live-acceptance.md)을 기준으로 확인합니다.

## 정리

시험을 마친 후 이 도구가 만든 `mc-live-e2e-broker`, `mc-live-e2e-client` 컨테이너를 명시적으로 제거합니다. 제어 서버 담당 도구가 자기 컨테이너·네트워크를 정리한 뒤0700 임시 디렉터리의 비밀파일을 제거합니다. 기존 서버, 다른 컨테이너, Docker 전체 볼륨은 정리 대상이 아닙니다.

## 2026-11-07 AMD64 추가 검증

Mac의 Docker에서 `--platform linux/amd64`로 Ubuntu 24.04 실행 파일을 빌드하고 실행했습니다. Apple Silicon의 CPU 에뮬레이션 검사이며 물리 AMD64 서버의 전체 연결 검사를 대체하지 않습니다.

- OpenSSL 3.5.9를 포함한 연결·중계·관리자 보조 프로그램의 AMD64 빌드 성공. 별도 이미지 `mc-live-e2e-native:amd64`에 보관하고 ARM64 이미지는 유지했습니다.
- AMD64 Rust 암호·통신 시험 13개 통과: 실제 상호 TLS/양자내성 키 교환, HTTPS 클라이언트, 배포 서명·무결성 등.
- 배포 도구 검사 11개 및 하위 검사 12개 통과. CPU 종류가 다른 파일의 오게시와 미완료 AMD64 빌드를 거절하는 검사 포함.
- `live-e2e-3`, 배포 순번 3으로 AMD64·ARM64를 각각 이중서명했습니다. 두 Ubuntu 컨테이너에서 실제 HTTPS 인증서 검증·다운로드·초기 해시 검증·다운로드한 프로그램의 실행·배포 이중서명 검증에 성공했습니다. 실행 중 외부 `libssl`/`libcrypto` 공유 라이브러리 의존성이 없는 것도 확인했습니다.
- 제어 서버와 중계 프로그램을 같은 새 버전·초기 스크립트 해시로 전환한 뒤 실제 `/api/v1/connections/capabilities`의 `available: true` 및 새 배포 주소를 확인했습니다. 기존 TCP 443·8443과 UDP 51820 공개 설정은 유지했습니다.

새 명령은 웹 화면을 새로고침한 뒤 다시 복사합니다. 이전에 복사한 ARM64 전용 명령의 URL이나 해시를 편집해서 재사용하지 않습니다. 원격 AMD64 단말의 보조 프로그램 설치·권한·터널 정책 변경과 해당 단말에서의 승인→터널→수집 전체 흐름은 이번 추가 검사의 범위가 아닙니다. 시험 인증서와 서명 목록의 만료 시각도 별도로 관리해야 합니다.

## 관리자 설치 경로의 kubectl 검사

`live-e2e-4`에서는 최초 실행 스크립트의 고정 검색 경로에 `/usr/local/sbin`과 `/usr/local/bin`을 포함했습니다. 기존 시스템 경로를 우선하고 사용자가 전달한 임의 PATH는 상속하지 않습니다. 필요한 도구가 없으면 누락된 이름과 검색 경로를 표시합니다.

```sh
python3 migration/connection/tests/e2e/check_bootstrap_tools.py \
  --bootstrap /private/tmp/onprem-live-e2e/release/releases/live-e2e-4/bootstrap.sh \
  --arch amd64
```

`--arch arm64`로도 실행할 수 있습니다. 두 아키텍처에서 실제 kubectl 실행 파일을 `/usr/local/bin`으로 옮긴 경우의 검색 성공과 파일이 없는 경우의 명확한 오류를 확인했습니다. 이 검사는 네트워크가 차단된 일회용 컨테이너에서 관리자 보조 프로그램을 의도적으로 제외해 다운로드·등록 전에 종료하는 사전검사 회귀시험입니다. 원격 등록이나 터널 성공의 근거로 사용하지 않습니다.
