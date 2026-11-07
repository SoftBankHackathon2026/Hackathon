# AWS ECR에서 Harbor로 컨테이너 이미지 이관

온프레미스 실행 환경에서 지정한 AWS 계정·리전의 비공개 ECR을 전수 조회합니다. 이미지 주소를 내용 고유값인 다이제스트로 고정해 [Harbor 준비 도구](../harbor-infra/README.md)가 마련한 프로젝트로 복사하고, 검증된 주소 대응표를 플랫폼에 전달합니다.

## 실행 환경

- Python 3.12 이상, 프로젝트에 지정한 버전의 Skopeo(레지스트리 간 이미지 복사 도구)가 필요합니다. Skopeo는 실행 환경의 이미지 서명 신뢰 정책을 사용합니다. [Skopeo 복사 옵션](https://github.com/podman-container-tools/skopeo/blob/main/docs/skopeo-copy.1.md)
- AWS 역할에는 전체 대상 저장소의 목록과 이미지 조회 권한(`ecr:DescribeRepositories`, `ecr:DescribeImages`), 이미지 다운로드 권한(`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability`)과 인증 토큰 발급 권한(`ecr:GetAuthorizationToken`)이 필요합니다. 작업 실행자가 지정 계정의 모든 저장소에 접근할 수 있도록 확인합니다. AWS 자격증명은 boto3(AWS용 Python 라이브러리)의 기본 방식이나 지정 프로필에서 받습니다. [AWS ECR 인증 및 다운로드](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-pull-ecr-image.html)
- Harbor 목적지는 사전에 만든 **비공개 일반 프로젝트**입니다. 준비 도구가 제공한 `credentials.json` 또는 만료 전 인계 자료의 `binding`과 `accounts` 구조를 사용합니다. 해당 프로젝트의 `writer`·`reader` 항목을 제공하고, 작업 사용자만 읽을 수 있는 파일에 저장합니다. 자체 인증 기관을 쓰는 경우 공개 인증서 파일도 전달합니다. Harbor 인증 토큰 발급 주소는 접속 주소와 같은 HTTPS 호스트·포트여야 합니다.
- 실행 환경에서 AWS 조회 주소·ECR·데이터 다운로드 주소·Harbor로 직접 접속할 수 있어야 합니다. Harbor 운영자는 프로젝트 할당량과 태그 보존 정책을 확인합니다. 복사 시 덮어쓰기를 막으려면 대상 태그 변경 방지 규칙이나 작업 중 단독 쓰기 권한을 마련한 뒤 `execution.exclusive_write`를 활성화합니다. 로컬 잠금은 같은 상태 디렉터리를 사용하는 프로세스에 적용됩니다. 여러 실행 호스트가 같은 대상을 쓴다면 플랫폼에서 별도로 직렬화합니다. [Harbor 태그 변경 방지 규칙](https://goharbor.io/docs/main/working-with-projects/working-with-images/create-tag-immutability-rules/)

## 명령

저장소 루트에서 다음과 같이 설치하고 [예제 설정](../../config.image-migration.example.yaml)의 주소, 계정, 경로와 Skopeo 버전을 실행 환경에 맞춥니다. `state_dir`은 실행 사용자 소유의 접근 권한 0700 디렉터리입니다. 없으면 도구가 생성합니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install './migration[image-migration]'
.venv/bin/python -m migration.image_migration discover --config /run/oneport/images.yaml --output inventory.json
.venv/bin/python -m migration.image_migration plan --config /run/oneport/images.yaml --output plan.json
```

`discover`는 빈 저장소, 태그 없는 이미지, 보관 및 복원 중인 이미지까지 모두 열거합니다. `plan`은 원본을 다시 수집하고, 같은 경로와 태그를 사용할 Harbor 주소 및 기존 태그 충돌을 계산합니다. 예를 들어 `oneport/api:release-1`은 `registry.example.com:8444/migrated/oneport/api:release-1`로 옮깁니다. 조사나 계획 파일은 새 이름으로 저장합니다.

계획의 출력에 있는 `plan_digest`를 인자로 전달합니다. `apply`는 계획 파일과 대상 설정이 일치할 때만 실행합니다. 원본은 다이제스트로 읽고, 목적지에 동일한 태그와 내용이 있으면 재전송을 생략합니다. 같은 태그의 내용이 다르면 그 태그를 차단합니다. 중단 후에는 같은 명령으로 재실행합니다. 도구는 이전 기록과 대상의 현재 상태를 함께 확인합니다.

```sh
plan_digest='계획 출력에서 받은 sha256 값'
.venv/bin/python -m migration.image_migration apply --config /run/oneport/images.yaml --plan plan.json --plan-digest "$plan_digest"
.venv/bin/python -m migration.image_migration verify --config /run/oneport/images.yaml --plan plan.json --plan-digest "$plan_digest" > verified.json
```

명령은 표준 출력에 JSON(구조화된 데이터) 한 건을 출력합니다. 각 명령 성공은 종료 코드 0, 부분 완료·실패·중단은 3입니다. 잘못된 명령 인자는 2입니다. `apply`의 `copied` 상태는 복사 단계 완료를 뜻하며, 주소 인계는 `verify` 결과의 `verified_mappings`를 사용합니다. 원본 재조회에서 이동하거나 삭제된 주소는 이 목록에서 빠집니다.

## 이미지 보존과 검증

- 여러 운영체제·CPU용 이미지의 목록과 하위 이미지 전체를 유지합니다. 다른 목록에 속하지 않는 태그 없는 이미지는 `oneport-untagged-sha256-<64자리>` 태그로 보존합니다. 목록으로 이관한 태그 없는 하위 이미지는 다이제스트 주소로 검증합니다. 보관 상태의 이미지가 있으면 원본 담당자가 복원한 뒤 재개합니다. 보관 중이라 하위 목록을 확인할 수 없었던 여러 CPU용 이미지는 복원 후 새 계획이 필요합니다. [AWS 이미지 조회 상태](https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_DescribeImagesFilter.html) · [보관 이미지 복원](https://docs.aws.amazon.com/AmazonECR/latest/userguide/restore-image.html)
- 독립적으로 연결된 서명과 구성 목록, Helm 배포 묶음은 결과에 별도 자료로 표시합니다. 판별할 수 없는 형식은 미완료로 남습니다. Harbor가 서명이나 취약점 정책에 따라 이미지 읽기를 거부하는 경우에도 미완료입니다. 빈 원본 저장소는 계획의 대응 경로에 기록합니다.
- `verify`는 읽기 계정으로 태그와 다이제스트 주소를 모두 조회합니다. 여러 CPU용 목록의 모든 하위 명세, 이미지 설정, 파일 층을 내려받아 해시를 검증합니다. 같은 저장소의 공통 데이터는 한 번 읽습니다. 목록 조회에 성공하고, 계획 전체가 검증됐으며, 원본 재조회에서 추가·삭제·태그 변경이 없어야 `verified` 상태를 반환합니다. 원본이 바뀌면 새 계획을 만들고 이미 도착한 데이터는 재사용합니다. [OCI 이미지 레지스트리 규격](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)

현장 적용은 별도 시험 계정·Harbor 프로젝트에서 여러 CPU용 이미지 한 건의 복사와 읽기 계정 검증부터 진행합니다. 이후 업무 계정의 전수 목록·용량·보관·충돌 내역을 검토하고 전체 계획을 적용합니다. 주소를 받은 서비스의 앱 설정 변경과 재배포는 별도 작업입니다.
