# Harbor에서 AWS ECR로 컨테이너 이미지 이관

온프레미스 Harbor의 지정 프로젝트를 Harbor API로 전수 조회하고, 지정한 AWS 계정·리전의 비공개 ECR로 복사합니다. 원본 주소를 내용 고유값인 다이제스트로 고정해 복사하고, ECR에 도착한 이미지의 다이제스트가 Harbor 원본과 같은지 검증한 뒤 주소 대응표를 플랫폼에 전달합니다. [ECR에서 Harbor로 옮기는 도구](../image-migration/README.md)의 반대 방향이며 계획 파일 형식, 재개 방식, 결과 형식을 공유합니다.

## 실행 환경

- Python 3.12 이상, 프로젝트에 지정한 버전의 Skopeo(레지스트리 간 이미지 복사 도구)가 필요합니다. [Skopeo 복사 옵션](https://github.com/podman-container-tools/skopeo/blob/main/docs/skopeo-copy.1.md)
- Harbor 원본은 **robot 계정**으로 읽습니다. 대상 프로젝트마다 저장소 목록 조회(`repository:list`), 이미지 다운로드(`repository:pull`), 아티팩트와 태그 조회(`artifact:list`, `artifact:read`, `tag:list`) 권한을 부여합니다. 쓰기와 삭제 권한은 필요 없습니다. robot 비밀값은 설정 파일에 직접 쓰지 않고 `secret_ref`로 참조합니다. `file`은 실행 사용자만 읽을 수 있는(0600) 파일 경로, `env`는 환경 변수 이름입니다. 둘 중 하나만 지정합니다. 도구는 요청마다 비밀값을 다시 읽으므로 실행 중 교체할 수 있습니다. 자체 인증 기관을 쓰는 경우 `ca_ref`에 공개 인증서 파일을 지정합니다. Harbor 인증 토큰 발급 주소는 접속 주소와 같은 HTTPS 호스트·포트여야 합니다. [Harbor robot 계정](https://goharbor.io/docs/main/working-with-projects/project-configuration/create-robot-accounts/)
- AWS 역할에는 다음 ECR 권한이 필요합니다. 인증 토큰 발급(`ecr:GetAuthorizationToken`), 저장소 조회·생성(`ecr:DescribeRepositories`, `ecr:CreateRepository`), 이미지 조회(`ecr:DescribeImages`, `ecr:BatchGetImage`), 업로드(`ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage`). KMS 암호화를 쓰면 지정 키에 대한 `kms:CreateGrant`, `kms:DescribeKey`, `kms:GenerateDataKey`, `kms:Decrypt`도 필요합니다. AWS 자격증명은 boto3(AWS용 Python 라이브러리)의 기본 방식이나 `target.profile`에서 받으며 Skopeo에는 전달하지 않습니다. [ECR 이미지 업로드 권한](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html)
- 실행 환경에서 Harbor, AWS API 주소, ECR 레지스트리 주소로 직접 접속할 수 있어야 합니다.

## 설정

[예제 설정](../../config.image-export.example.yaml)의 `<...>` 자리를 실행 환경 값으로 바꿉니다. `state_dir`은 실행 사용자 소유의 접근 권한 0700 디렉터리입니다. 없으면 도구가 생성합니다.

| 항목 | 의미 |
| --- | --- |
| `source.projects` | 이관할 Harbor 프로젝트 목록 |
| `source.robot.secret_ref` | `file: <경로>` 또는 `env: <변수 이름>` 중 하나 |
| `target.repository_prefix` | ECR 저장소 이름 앞에 붙일 경로. `team/api`는 `onprem/team/api`가 됩니다. 생략하면 Harbor 경로를 그대로 씁니다 |
| `target.repository.tag_immutability` | 새로 만드는 저장소의 태그 변경 방지. 기본값 `true` |
| `target.repository.scan_on_push` | 업로드 시 취약점 검사. 기본값 `true` |
| `target.repository.encryption` | `AES256`(기본값) 또는 `KMS`. `KMS`에서 `kms_key`를 생략하면 AWS 관리 키를 씁니다 |
| `execution.exclusive_write` | 작업 중 단독 쓰기나 태그 변경 방지를 마련한 뒤 `true`로 바꿉니다. `false`이면 `apply`를 거부합니다 |

저장소 설정은 계획에 묶입니다. 계획 후 설정을 바꾸면 `apply`가 `PLAN_BINDING_MISMATCH`로 멈추므로 새 계획을 만듭니다.

## 명령

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install './migration[image-migration]'
.venv/bin/python -m migration.image_export discover --config /run/oneport/export.yaml --output inventory.json
.venv/bin/python -m migration.image_export plan --config /run/oneport/export.yaml --output plan.json
```

`discover`는 Harbor만 읽어 프로젝트의 모든 저장소, 태그 없는 이미지, 서명 같은 부속 자료까지 열거합니다. `plan`은 원본을 다시 수집하고 각 매니페스트를 다이제스트로 읽어 형식과 하위 이미지를 확인합니다. ECR은 조회만 하며 만들어야 할 저장소(`target_exists: false`), 기존 저장소의 설정 차이(`settings_mismatch`), 이미 같은 다이제스트로 있는 태그(`present_tags`), 다른 내용이 있는 태그(`TARGET_TAG_CONFLICT`)를 기록합니다. `plan`은 Harbor와 ECR에 아무것도 쓰지 않습니다. 결과 파일은 새 이름으로 저장하며 기존 파일을 덮어쓰지 않습니다.

```sh
plan_digest='계획 출력에서 받은 sha256 값'
.venv/bin/python -m migration.image_export apply --config /run/oneport/export.yaml --plan plan.json --plan-digest "$plan_digest"
.venv/bin/python -m migration.image_export verify --config /run/oneport/export.yaml --plan plan.json --plan-digest "$plan_digest" > verified.json
```

`apply`는 계획 파일의 해시와 설정이 일치할 때만 실행합니다. 없는 ECR 저장소를 설정대로 만들고, Harbor 원본을 `저장소@sha256:...`로 고정해 `skopeo copy --all --preserve-digests`로 복사합니다. 복사 전후로 ECR의 태그가 가리키는 다이제스트를 조회해 같은 내용이 이미 있으면 재전송을 생략하고, 다른 내용이면 그 태그를 차단합니다. 진행 기록은 `state_dir`의 계획별 기록 파일에 남습니다. 중단 후에는 같은 명령으로 재실행하며, 도구는 이전 기록과 ECR의 현재 상태를 함께 확인합니다. 일시적 오류와 ECR 토큰 만료는 최대 3회 재시도합니다.

명령은 표준 출력에 JSON 한 건을 출력합니다. 성공은 종료 코드 0, 부분 완료·실패·중단은 3, 잘못된 명령 인자는 2입니다. 비밀값과 인증 토큰은 출력하지 않습니다. 주소 인계는 `verify` 결과의 `verified_mappings`를 사용합니다.

## 이미지 보존과 검증

- 여러 운영체제·CPU용 이미지는 목록과 하위 이미지 전체를 같은 다이제스트로 옮깁니다. 다른 목록에 속하지 않는 태그 없는 이미지는 `oneport-untagged-sha256-<64자리>` 태그로 보존합니다.
- Harbor에 연결된 서명 등 부속 자료와 Helm 배포 묶음은 `excluded_attachment`로 표시하고 복사하지 않습니다. 판별할 수 없는 형식은 미완료로 남습니다.
- `verify`는 ECR에서 태그와 다이제스트 주소를 모두 읽어 매니페스트 해시가 계획의 Harbor 다이제스트와 같은지 확인합니다. 여러 CPU용 목록의 모든 하위 매니페스트를 따라가며 이미지 설정과 파일 층이 ECR에 있고 크기가 맞는지 확인합니다. 이어서 Harbor를 다시 조회해 추가·삭제·태그 변경이 없어야 `verified`를 반환합니다. 원본이 바뀌면 새 계획을 만들고 이미 도착한 데이터는 재사용합니다.

## 제한 사항

- ECR의 파일 층은 다운로드하지 않습니다. 지정 권한에 `ecr:GetDownloadUrlForLayer`가 없기 때문입니다. 파일 층 내용은 ECR이 업로드 완료 시 다이제스트로 검사하며, 도구는 존재와 크기만 확인합니다.
- 기존 ECR 저장소의 태그 변경 방지·검사·암호화 설정은 바꾸지 않고 `settings_mismatch`로만 보고합니다. 암호화 설정은 저장소 생성 후 바꿀 수 없습니다.
- 태그 변경 방지 저장소에서 계획 이후 누군가 같은 태그에 다른 내용을 올리면 그 태그는 차단으로 남습니다. 서명은 복사하지 않으므로 ECR 측 서명 정책이 필요하면 별도로 다시 서명합니다.
- 로컬 잠금은 같은 상태 디렉터리를 쓰는 프로세스에만 적용됩니다. 여러 실행 호스트가 같은 계정·리전에 쓴다면 플랫폼에서 별도로 직렬화합니다.
- ECR 저장소 이름 규칙(소문자, 구분자 연속 불가, 256자 이하)에 맞지 않는 Harbor 경로는 `INVALID_TARGET_REPOSITORY`로 계획 단계에서 멈춥니다.
- ECR 할당량(저장소당 이미지 수, 업로드 속도)은 AWS 계정 설정을 따릅니다. 대량 이관 전에 시험 프로젝트 한 건으로 복사와 검증부터 진행합니다.
