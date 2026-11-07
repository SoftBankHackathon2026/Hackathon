# crosscloud GCP 모듈

AWS↔GCP 이전에서 GCP 쪽 자원을 만드는 Terraform 루트 모듈 셋이다. AWS→GCP
이전에서는 이전 대상을, GCP→AWS 시험에서는 원본 고정물을 같은 모듈로 만든다.
방향은 입력 `direction` 으로 고른다.

## 모듈

| 모듈 | 만드는 것 |
|---|---|
| `registry` | Artifact Registry Docker 저장소 |
| `target_base` | Cloud SQL PostgreSQL(논리 복제 켬) · 앱 DB · Secret Manager 비밀 그릇 |
| `target_app` | Cloud Run 서비스 · 실행 서비스 계정 · 외부 Application LB(서버리스 NEG, HTTP 80) |

적용 순서는 `registry` → `target_base` → (이미지 복사·Secret 값 이관) →
`target_app` 이다. 해체는 그 반대다.

## 공통 입력

| 변수 | 뜻 |
|---|---|
| `project_id` | GCP 프로젝트 ID |
| `region` | 리전. 기본값 `asia-northeast3` |
| `name_prefix` | 실행 접두사 `ccx-<run>`. run 은 영문 소문자·숫자 1–16자 |
| `direction` | `aws_to_gcp`(대상) 또는 `gcp_to_aws`(원본 고정물) |

모든 자원 이름은 `name_prefix` 로 시작한다. 라벨을 받는 자원에는
`crosscloud-run`(= `name_prefix`)과 `crosscloud-direction` 라벨이 붙는다.
잔여 검사(`crosscloud.provision.residue_gcp`)는 이름 접두사로 한 번 질의한다.

방향이 동작을 바꾸는 곳은 `target_app` 의 Cloud Run 진입(ingress) 하나다.
대상(`aws_to_gcp`)은 전환 전까지 진입을 닫아 두고, 원본 고정물(`gcp_to_aws`)은
처음부터 LB 로 받는다.

## 시험 환경 결정

2026-11-07 사용자 결정에 따라 운영 옵션을 뺐다.

*   Cloud SQL 은 단일 존·최소 등급이고 백업·삭제 보호가 없다. 공개 IP 에 허용
    망 `0.0.0.0/0` 을 두고 TLS 필수(`ENCRYPTED_ONLY`)와 강한 무작위 비밀번호로
    막는다.
*   Cloud Run 은 VPC egress·Cloud NAT 없이 기본 출구로 나간다.
*   LB 프런트엔드는 HTTP 다. 시연 DNS 가 Route 53 비공개 영역이라 인증서를 받을
    도메인이 없다.

## 비밀값

Terraform state 에 비밀 평문을 두지 않는다.

*   Secret 은 그릇만 만든다. 값(버전)은 Terraform 밖에서 넣는다.
*   Cloud SQL `postgres` 사용자 비밀번호를 여기서 정하지 않는다.
*   `target_app` 의 `env` 에는 비밀이 아닌 값만 넣는다. 비밀과 DB 접속 대상은
    `secret_env` 로 Secret 을 참조한다.

## 전제

*   API: `artifactregistry` · `sqladmin` · `secretmanager` · `run` ·
    `compute` · `iam` · `cloudasset`(잔여 검사).
*   `target_app` 은 비밀 IAM 바인딩과 Cloud Run 호출자 IAM 끄기를 한다. 실행
    신원에 IAM 정책 설정 권한이 있어야 한다(프로젝트 Editor 만으로는 부족할 수
    있다 — 실측 전).

## 실행

`crosscloud.provision.terraform.Terraform` 으로 돌린다. 각 모듈은 빈
`backend "local" {}` 를 선언하고, 실행기가 state 경로를 init 때 넘긴다.

검증만 할 때:

```shell
terraform fmt -check -recursive
cd registry && terraform init -backend=false && terraform validate
```
