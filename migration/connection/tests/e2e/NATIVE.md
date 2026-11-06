# 실제 Ubuntu 네이티브 시험 준비

이 도구는 로컬 Docker의 `mc-live-e2e` 전용 네트워크에서 실제 단말·브로커·WireGuard 커널·제한된 관리자 보조 프로그램을 실행합니다. 서버의 READY 상태를 대신 입력하거나 서명을 생략하지 않습니다. 기본 재현 경로는 실제 웹 화면에서 명령을 복사하고 단말을 승인하는 방식입니다. 아래 정상 API 대체 전달 도구를 사용한 실행은 브라우저 복사·붙여넣기 검증과 구분합니다. 연결 대상 K3s 서버에 프로그램을 설치하거나 서버 설정을 변경하지 않습니다.

## 빌드

`prepare_trust.py`로 만든 공개 릴리스 신뢰키를 `build_native.py`가 읽습니다. 개인키와 kubeconfig는 Docker 빌드에 전달하지 않습니다. 빌드 맥락은 `migration/connection`이며 임시 디렉터리는 그 밖에 두어야 합니다.

```sh
python3 migration/connection/tests/e2e/build_native.py \
  --private-dir /private/tmp/onprem-live-e2e
```

Docker 이미지 `mc-live-e2e-native:local`은 Ubuntu24.04 arm64입니다. OpenSSL3.5.9를 공식 소스에서 SHA256 검증 후 정적 링크합니다. Rust1.93으로 세 실행파일을 빌드하고 실제 상호 TLS·exporter·고전 키 교환 거절 및 릴리스 검증 시험을 실행합니다. 바이너리는 임시 디렉터리의 `bin/`으로 복사됩니다. 시험은 현재 arm64만 다루며 다른 CPU의 성공을 표시하지 않습니다.

- [OpenSSL 공식 배포 목록](https://openssl-library.org/source/)
- [OpenSSL3.5.9 공식 SHA256](https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz.sha256): `603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a`
- [kubectl 공식 Linux 설치 설명](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)
- [kubectl1.37.2 Linux arm64 공식 SHA256](https://dl.k8s.io/release/v1.37.2/bin/linux/arm64/kubectl.sha256): `fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509`

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

bootstrap은 실제 HTTPS 다운로드·첫 바이너리 SHA384·manifest 이중서명을 검증한 후 실행합니다. foreground 단말은 systemd를 사용하지 않으므로 systemd 실행 여부를 가짜 디렉터리로 충족시키지 않습니다. 필요한 커널 네트워크 기능은 실제 helper가 검사합니다.

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
