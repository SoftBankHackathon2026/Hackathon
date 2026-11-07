# crosscloud AWS 모듈

AWS↔GCP 이전에서 AWS 쪽 자원을 만드는 Terraform 루트 모듈이다. AWS→GCP
이전에서는 원본 고정물을, GCP→AWS 이전에서는 이전 대상을 같은 모듈로 만든다.
방향은 입력 `direction` 으로 고른다.

## 층

| 층 | 만드는 것 | `aws_to_gcp` | `gcp_to_aws` |
|---|---|---|---|
| `modules/base` | 전용 VPC(퍼블릭 서브넷 2, NAT 없음) · RDS PostgreSQL(논리 복제 켬) · Secret 그릇 · ECR · ECS 클러스터·실행 역할·로그 그룹 | 원본 고정물 | 대상 기반 |
| `modules/app` | 인터넷 연결형 ALB(HTTP 80) → ECS Fargate 서비스(공인 IP) | 원본 고정물 | 대상 앱 |

`include_app = false` 면 기반 층만 만든다. 대상(`gcp_to_aws`)은 기반 층을
먼저 깔고, 이미지 복사·Secret 값 이관 뒤 `include_app = true` 로 앱 층을
더한다. 해체는 그 반대다.

## 공통 입력

| 변수 | 뜻 |
|---|---|
| `region` | 리전. 기본값 `ap-northeast-2` |
| `name_prefix` | 실행 접두사 `ccx-<run>`. run 은 영문 소문자·숫자 1–16자 |
| `direction` | `aws_to_gcp`(원본 고정물) 또는 `gcp_to_aws`(대상) |

모든 자원 이름은 `name_prefix` 이거나 `name_prefix-` 로 시작하고, 모든 자원에
`crosscloud-run`(= `name_prefix`)과 `crosscloud-direction` 태그가 붙는다.
해체됐다는 1차 근거는 `terraform destroy` 뒤 state 가 빈 것이다. 잔여
검사(`crosscloud.provision.residue_aws`)는 보조 확인이다. 이 모듈이 만드는 자원
종류마다 서비스 API 를 직접 조회해(EC2 망 자원은 태그 필터, 나머지는 이름
접두사) state 밖에 남은 자원을 찾는다. 조회 리전은 루트 출력 `region` 을
명시해 넘긴다. 모듈에 새 자원 종류를 더하면 `residue_aws._SCANS` 에도 더한다.

## 시험 환경 결정

2026-11-07 사용자 결정에 따라 운영 옵션을 뺐다.

*   RDS 는 단일 AZ·최소 등급이고 백업·삭제 보호가 없다. 공개 엔드포인트에 SG
    `0.0.0.0/0`(5432)을 두고 TLS 와 강한 무작위 비밀번호로 막는다.
*   앱–DB TLS 는 유지한다. `app_db_ssl_ca` 에 RDS 리전 CA 번들 PEM 을 주면 앱
    env `DB_SSL_CA` 로 들어가 앱이 인증서를 검증한다(비밀값 아님).
*   ALB 는 HTTP 80 이다. 운영자 PC 의 ingress 가 인터넷으로 부른다.
*   ALB 상태 확인 기본 경로는 DB 를 읽는 `/readyz` 다.

## 비밀값

Terraform state 에 비밀 평문을 두지 않는다.

*   RDS 마스터 비밀번호는 RDS 가 만들어 Secrets Manager 에 둔다
    (`manage_master_user_password`). 위치는 출력 `db.master_user_secret_arn`.
*   Secret 은 그릇만 만든다. 값(버전)은 Terraform 밖에서 넣는다. 값이 들어가기
    전에는 ECS 작업이 시작에 실패하고 재시도한다.
*   `app_env` 에는 비밀이 아닌 값만 넣는다. 비밀과 DB 접속 대상은
    `app_secret_env` 로 Secret 을 참조한다.

## Terraform 밖에서 바뀌는 값

리스너 기본 동작과 ECS 서비스의 작업 정의는 `ignore_changes` 다. 전환 단계가
앱을 멈추거나 대상 DB 를 가리키는 작업 정의로 바꾼 뒤, 재 apply 가 앱을 원본
DB 로 되돌리지 못하게 한다.

전환 단계와의 계약: 작업 정의를 바꿀 때는 같은 family(`<name_prefix>-app`)의
새 리비전으로 등록한다. 그 리비전이 참조할 Secret 은 `secret_names` 에 미리
선언해 이 모듈이 그릇을 만들게 한다. 그래야 실행 역할이 그 Secret 을 읽을 수
있고, 잔여 검사가 그 리비전과 Secret 을 찾는다.

Fargate 작업 정의는 플랫폼을 따로 정하지 않아 `LINUX/X86_64` 로 돈다. 앱
이미지에는 linux/amd64 가 있어야 한다.

## 실행

`crosscloud.provision.terraform.Terraform` 으로 돌린다. 루트 모듈은 빈
`backend "local" {}` 를 선언하고, 실행기가 state 경로를 init 때 넘긴다.

검증만 할 때(자격 증명 불필요, `terraform test` 는 가짜 공급자로 plan 만):

```shell
terraform fmt -check -recursive
for dir in . modules/base modules/app; do
  terraform -chdir="${dir}" init -backend=false
  terraform -chdir="${dir}" validate
  terraform -chdir="${dir}" test
done
```
