# 격리 시험의 실제 신뢰자료와 배포 묶음

`prepare_trust.py`는 명시한 현재 사용자 소유 0700 디렉터리 안에만 시험키를 만듭니다. 기존 키는 덮어쓰지 않습니다. 개인키와 런타임 토큰은 0600이며 값을 출력하지 않습니다. 실제 클러스터 주소나 인증정보를 소스에 기록하지 않습니다.

```sh
python3 migration/connection/tests/e2e/prepare_trust.py \
  --directory /private/tmp/onprem-live-e2e \
  --openssl /검토한/OpenSSL-3.5-이상/bin/openssl
```

- `ca.crt`, `control.crt`, `control.key`: `control.test` 일반 HTTPS용 별도 시험 인증기관과 서버 인증서입니다.
- `broker-ca.crt`, `broker.crt`, `broker.key`: `broker.test`의 실제 ML-DSA 인증기관과 서버 인증서입니다. 유효기간은 24시간입니다.
- `signing/`: 실제 ML-DSA-65·Ed25519 배포 서명 개인키입니다. 게시하지 않습니다.
- `job-signing-key.pem`, `runtime-token`: 제어 서버의 실제 작업 서명키와 중계 서비스 인증값입니다. 게시하지 않습니다.
- `public/release-build.env`, `public/job-public-key`: 공개 빌드 검증값과 공개 작업 검증키입니다. 단말 바이너리에 동일한 배포 검증키를 포함해야 합니다.

네이티브 빌드의 실제 ARM64 바이너리가 `bin/migration-connector`에 준비되면 다음 명령으로 배포 묶음을 생성합니다. 인증 검증을 끄거나 모의 서명을 사용하지 않습니다.

```sh
python3 migration/connection/tests/e2e/prepare_release.py \
  --directory /private/tmp/onprem-live-e2e \
  --sequence 1 \
  --openssl /검토한/OpenSSL-3.5-이상/bin/openssl
```

빌드 시작 시 생성한 `bin/.build-incomplete`가 남아 있으면 기존 실행 파일이 있어도 게시를 거부합니다. 성공한 최종 출력 이후에만 빌드 도구가 표식을 제거합니다. 배포 순번은 명시적으로 전달하고 재게시 시 이전보다 높여야 합니다. 첫 배포 후 수정 빌드에는 최소 `--sequence 2`가 필요합니다. 기존 게시 디렉터리는 자동으로 덮어쓰지 않으므로, 재게시 전 실행 중인 시험을 멈추고 기존 공개 묶음을 별도 비공개 보관 위치로 옮긴 뒤 진행합니다. 공개 메타데이터는 순번 비교를 위해 유지합니다.

게시 경로는 격리 Docker 네트워크의 `https://control.test/releases/live-e2e-1/`입니다. `bootstrap.sh`, 실제 바이너리, 공개 설정, 전용 중계 CA, 읽기 전용 수집기, 두 실제 서명이 포함된 배포 목록만 게시합니다. 배포 목록은 6시간 후 만료됩니다. 이번 시험의 x86_64 최초 실행은 명시적으로 거절하며 ARM64 성공을 다른 아키텍처 검증으로 간주하지 않습니다.

공개 배포 하위 디렉터리만 0755, 게시 파일만 0644로 설정하여 웹 서버가 읽을 수 있게 합니다. 상위 시험 디렉터리는 0700을 유지하고, 비밀파일은 게시 디렉터리 밖에 둡니다. HTTPS 웹 서버에는 이 공개 하위 디렉터리만 읽기 전용으로 연결합니다. 단말 컨테이너에는 일반 HTTPS CA를 신뢰하도록 설치하고 중계 전용 CA는 서명된 묶음에서 읽습니다. 호스트 전체의 인증기관 신뢰를 바꾸지 않습니다.

`public/release-metadata.json`의 실제 초기 스크립트 SHA-384를 제어 서버와 중계 서비스에 동일하게 적용해야 합니다. 변경된 최초 실행 스크립트나 바이너리를 기존 서명·해시와 섞지 않습니다. 생성 완료는 실제 연결 흐름 시험 통과를 의미하지 않습니다.
