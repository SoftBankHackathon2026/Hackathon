# 계정·권한 이관 도구

이 구현은 기존 데이터베이스 도구와 분리된 `python -m migration.identity` 명령입니다. **읽기 전용 수집, 좁은 객체 읽기 계획, 항목별 추가 적용기와 상태 기록을 제공합니다. 실제 이관 완료를 검증한 결과는 아닙니다.**

적용 기준은 [승인된 계획](./approved-plan.md)과 [기능·관계도 기준](./migration-flow.md)이며, 현재 구현 상태와 검사 근거는 [진행 기록](./implementation-status.md)에 있습니다. 실제 계정·자원·자격증명은 제공되지 않아 외부 계정에 접속하거나 권한을 변경하지 않았습니다.

## 설치와 오프라인 실행

Python 3.12 이상을 사용합니다. 저장소 루트에서 아래처럼 실행합니다.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -c migration/identity-requirements.lock 'migration[dev,identity-all]'
.venv/bin/python -m migration.identity --help
.venv/bin/python -m migration.identity collect --spec migration/identity-examples/aws-gcp-readonly.yaml --inventory migration/identity-examples/aws-gcp-inventory.json --out run-artifacts/inventory.json
.venv/bin/python -m migration.identity plan --spec migration/identity-examples/aws-gcp-readonly.yaml --inventory run-artifacts/inventory.json --out run-artifacts/plan.json
.venv/bin/python -m migration.identity preflight --plan run-artifacts/plan.json
.venv/bin/python -m migration.identity report --plan run-artifacts/plan.json --out run-artifacts/report.json --markdown run-artifacts/report.md
```

예시 자료의 `complete`는 **모의 시험 전제**입니다. 실제 환경에서 조사했다는 뜻이 아니며, 예시를 운영 적용에 사용하면 안 됩니다. `collect --inventory`는 기존 자료를 검증해 가져오는 동작이고 실제 수집으로 표시하지 않습니다. 도움말·오프라인 계획에는 네트워크·클라우드 자격증명·선택 의존성이 필요하지 않습니다. 역방향 예시는 `gcp-aws-readonly.yaml`과 `gcp-aws-inventory.json`이며, 네임스페이스 모의 예시는 `keycloak-kubernetes-readonly.yaml`과 대응 inventory 파일입니다.

## 자료와 변경 경계

명세는 버전·방향·업무·원본/대상 공급자·관리 범위·신원·자원·변경 허용 목록·검증 사례를 갖습니다. 알 수 없는 필드, 잘못된 타입, 중복 키, 비밀 값 필드는 거절합니다. 신원은 공급자·관리 범위·불변 식별자와 함께 기록합니다. 이름만 같은 역할이나 계정으로 바뀌면 적용·재개·검증을 중단합니다.

자료 계약은 `models.py`, 제품 공통 계약은 `adapters/base.py`에 있습니다. 관찰은 `{exists, body, version}`이며 없는 자원과 조회 실패를 구분합니다. 규칙 자료는 설치 패키지에서 읽으며 버전은 `2026-11-04.1`입니다. 기존 DB 설정·상태 내부 클래스와 OPA의 DB 단계 판정은 사용하지 않습니다.

AWS 실행 Role은 신원, AWS 정책은 작업 권한, GCP Role은 권한 묶음, 버킷 정책은 주체와 Role의 연결입니다. 신뢰·대행·실행 신원 부착과 자원 접근을 별도로 기록합니다. 실행 환경 부착은 외부 선행 작업이며 자동 배포하지 않습니다. 관계도 원문 근거는 완전한 실효 권한 평가 결과가 아닙니다.

최초 AWS 규칙은 지정 버킷의 `s3:ListBucket`과 전체 객체의 `s3:GetObject`입니다. GCP 사용자 정의 Role에 `storage.objects.list/get`만 넣고 버킷 하나에 연결합니다. 더 넓은 사전 정의 역할로 대체하지 않습니다. 거부·조건·제외·작업 와일드카드·접두사 제한·불명확한 상위 제한은 버리지 않고 차단합니다. 역방향은 별도 규칙을 사용합니다.

## 수집·계획·적용 지원표

| 경로 | 수집과 계획 | 적용 코드 | 실제 환경 검증 |
| --- | --- | --- | --- |
| AWS→GCP 객체 읽기 | 지정 Role 정책/신뢰·버킷 제한, 대상 SA/Role/버킷 정책. 읽기 두 작업만 계획 | SA·사용자 정의 Role 신규 생성/동일 재사용, 버킷 연결 병합 | 미실행 |
| GCP→AWS 객체 읽기 | SA·버킷/프로젝트 Allow 정책·Role 정의·상위 조사 누락. 별도 역방향 계획 | EC2 신뢰 Role·신규 inline 읽기 정책. 인스턴스 프로파일은 독립 적용기에서만 지원 | 미실행 |
| AWS 실행 유지, 직접 접근 | 원문 근거 보존·미지원 계획 | 신원 풀/공급자 자동 구성 미지원 | 미실행 |
| AWS 실행 유지, SA 대행 | 직접 접근과 구분해 미지원 표시 | 대행 연결 자동 구성 미지원 | 미실행 |
| Keycloak→Kubernetes | 그룹/사용자/역할/client 및 네임스페이스 조회. 별도 pod 읽기 권한 원본 근거가 있을 때 계획 후보 | Keycloak 신규 최상위 그룹, Kubernetes 신규 Role/RoleBinding. Keycloak 그룹만으로 권한을 추정하지 않음 | 로그인·다른 네임스페이스·그룹 탈퇴 시험 미실행 |
| OpenStack | 지정 그룹/프로젝트/Role 할당 및 선택 연합 자료 조회 | 명시된 그룹/프로젝트/Role 추가 연결의 독립 적용기 | 연합 protocol/mapping 변환 및 서비스 권한 동등성 미지원·미검증 |
| 온프레미스→클라우드·일반 제품 | 제품별 업무 변환 규칙 없음, 차단 보고 | 미지원 | 미실행 |

**현재 실제 수집은 AWS 조직·세션 제한, 개별 객체 암호화, GCP 상위 Deny·경계·객체 정책, 온프레미스 중첩 권한 등을 완전하게 조사하지 못합니다. 따라서 실제 수집 결과의 `unknown`을 그대로 사용하면 `apply`는 차단됩니다.** 이를 무시하는 옵션은 없습니다. 적용기 모의 시험의 성공을 실환경 적용 가능 또는 권한 동등성 입증으로 해석하면 안 됩니다. 완전한 상위 수집/증거 계약을 추가하는 것은 남은 개발 작업입니다.

## 실환경 명령과 자격증명 참조

실제 사용자가 지정한 시험 범위·준비된 자원·분리된 수집/적용/업무 시험 신원이 있을 때만 아래 형식을 사용합니다. 범위는 명세에 고정하며 인증된 계정·프로젝트 번호·불변 신원·버킷 소유·클러스터 서버를 다시 확인합니다.

```sh
python -m migration.identity collect --live --spec /secure/job.yaml --out run-artifacts/inventory.json
python -m migration.identity preflight --live --plan run-artifacts/plan.json
python -m migration.identity apply --live --plan run-artifacts/plan.json --review-ref /secure/review.json --state-dir run-artifacts/state
python -m migration.identity resume --live --plan run-artifacts/plan.json --review-ref /secure/review.json --state-dir run-artifacts/state
python -m migration.identity status --state-dir run-artifacts/state
python -m migration.identity verify --live --plan run-artifacts/plan.json --state-dir run-artifacts/state --report run-artifacts/verification.json
python -m migration.identity rollback-plan --plan run-artifacts/plan.json --state-dir run-artifacts/state --out run-artifacts/rollback.json
```

검토 파일은 `review.example.json` 형식의 `{schema_version:1, plan_digest, reviewer, reference}`입니다. 정확한 계획 내용·범위·수집 자료·규칙 버전에 묶으며 자체 인증이나 클라우드 접근 권한을 부여하지 않습니다. 직접 편집해 식별값을 다시 계산한 임의 작업도 규칙으로 재생성한 계획과 다르면 거절합니다.

- AWS: 공식 기본 인증 체인 또는 명세의 `profile`/`region` 참조. `scope`는 계정 번호, `identity`는 Role ARN, `resource`는 버킷 이름입니다. `stable_id`로 알려진 RoleId를 고정할 수 있습니다.
- GCP: 환경 기본 인증. `scope`는 프로젝트 ID, `identity`는 서비스 계정 이메일, `resource`는 버킷 이름입니다. `project_number`와 `stable_id`는 문자열로 지정합니다.
- Keycloak: `options.url`의 HTTPS 주소·`scope`의 realm·`options.version`의 지원 26.x 버전을 실제 서버와 대조합니다. 토큰은 `IDENTITY_KEYCLOAK_TOKEN` 환경에서만 읽습니다. 신규 그룹은 `identity: new:/readers`처럼 명시합니다.
- Kubernetes: `scope`와 `resource`는 같은 네임스페이스, `identity`는 실제 그룹 주장 문자열, `options.context`와 `options.url`은 고정한 kubeconfig 이름과 HTTPS API 서버입니다. 실제 로그인에서 확인한 `group_claim_confirmed: true`가 필요합니다. 클러스터 인증 설정이나 ClusterRoleBinding은 바꾸지 않습니다.
- OpenStack: 명세 `options.url`에 고정한 주소와 일치하는 HTTPS `OS_AUTH_URL` 및 명시적인 `OS_*` 인증 환경을 사용하며 암묵적인 `OS_CLOUD` 선택과 인증서 검증 해제를 거절합니다. 도메인·프로젝트·그룹·Role 식별자를 지정합니다.

비밀번호·토큰·개인 키를 명세·예시·계획·로그·상태·보고서에 넣지 않습니다. API 오류 원문을 출력하지 않으며 비밀 필드 입력은 거절합니다. 실행 산출물·가상환경·상태 파일은 버전 관리에서 제외합니다. 알 수 없는 제품별 자유 문자열에 포함된 모든 비밀을 탐지하는 기능은 아니므로 원본 정책에 비밀을 기록하지 않는 운영 규칙도 필요합니다.

## 상태·재시작·복구

별도 `identity.sqlite3`와 프로세스 잠금을 사용합니다. 쓰기 전에 변경 전 상태를 영속 기록하고, 실제 변경 직전 원본·대상 정책 문맥과 선행 자원의 불변 식별자를 다시 확인합니다. 버킷 연결은 기존 조건과 다른 구성원을 보존하며 정책 버전 식별값을 사용합니다. Kubernetes 기존 객체는 변경하지 않고 버전·UID를 비교한 뒤 신규 또는 동일 객체만 다룹니다.

응답 유실은 실패 확정으로 처리하지 않습니다. 제한된 재조회로 결과를 확인하고 `reconciled` 또는 `unknown`을 남깁니다. 일치한 결과를 찾아도 소유가 불명확하면 도구가 만들었다고 주장하지 않습니다. 재시작은 영속 기록과 실제 상태를 대조하고, 다른 변경은 충돌로 중단합니다. SDK의 자동 쓰기 재시도는 제한합니다.

AWS IAM은 조건부 버전 갱신이 없어 최종 재조회 이후 외부 변경과의 경쟁을 완전히 제거할 수 없습니다. 동일한 작업은 반드시 같은 상태 디렉터리와 단일 작업자로 실행해야 합니다. 이 잠금은 다른 디렉터리·다른 호스트의 실행을 통제하는 분산 잠금이 아닙니다.

`rollback-plan`은 읽기 전용 복구 제안입니다. 기록된 정확한 연결 차이·신규 자원·현재 상태 전제만 제시하며 **자동 복구 적용은 미지원**입니다. 기존 권한·다른 구성원·소유 불명 자원을 일괄 삭제하지 않습니다. 부분 적용·불명 상태에서 수동 재조회와 새 검토가 필요할 수 있습니다.

종료 코드: 0은 해당 명령 성공(실제 이관 완료와 다름), 1 일반 실패, 2 설정 오류, 4 차단/검증 미완료, 5 부분 적용 후 중단, 6 상태 불명입니다. 누락된 검토 파일은 2, 다른 계획에 대한 검토는 4입니다. 별도 검토 대기 3 코드는 현재 사용하지 않습니다. 차단 계획도 파일을 남기고 4로 종료합니다.

## 실제 접근 검증의 한계

구성 조회 성공과 실제 사용자·프로그램 접근 성공을 구분합니다. AWS/GCP의 읽기 전용 실제 주체 시험 코드는 별도의 `IDENTITY_VERIFY_AWS_PROFILE` 또는 외부 파일 경로 `IDENTITY_VERIFY_GCP_CREDENTIALS`로 업무 신원을 선택하고, 계획/실행 기록에 고정한 불변 신원과 대조합니다. 관리자 조회로 존재와 소유를 확인한 시험 자원에서만 읽기·목록을 시험합니다. 반환한 데이터나 자격증명은 기록하지 않습니다.

네트워크·인증·없는 자원은 거부 성공으로 집계하지 않습니다. GCP HTTP 403만으로 성공 처리하지 않으며 작업 권한을 지목한 구조화된 IAM 거부 근거가 없으면 불명입니다. 이 보수적 판정은 일반적인 Storage 403 응답을 불명으로 남길 수 있습니다.

쓰기·삭제·대행·실제 로그인·그룹 탈퇴의 실환경 시험 및 격리 자료 생성/정리 수명주기는 아직 구현하지 않았습니다. 일반 운영 객체로 시험하지 않습니다. 따라서 현재 기본 실환경 검증기는 전체 인수에 필요한 모든 허용/거부 시험을 완료했다고 판정할 수 없습니다. 오프라인 `verify`는 항상 실제 접근 미검증으로 기록합니다.

## 패키지·이미지·자동 검사

`identity-aws/gcp/keycloak/k8s/openstack/all` 선택 설치를 제공합니다. 직접 및 하위 의존성은 `identity-requirements.lock`에 고정했으며 Python 3.12에서 전체 묶음 설치와 새 이미지 빌드를 확인합니다. 설치형 wheel과 원본 배포 파일에 하위 패키지·규칙 자료가 포함됩니다.

새 `Dockerfile.identity`는 별도 비관리자 실행 이미지입니다. 기본 명령은 도움말이며 자격증명이나 실행 산출물을 굽지 않습니다. `.github/workflows/identity.yml`은 모의·계약·기존 회귀 시험, OPA 검사, 저장소 밖 wheel 설치 및 이미지 빌드를 수행하며 이미지 게시와 실제 환경 시험은 하지 않습니다. 워크플로 원격 실행 결과는 아직 없습니다. 기존 `migration-images.yml`은 `migration/**` 변경의 영향을 계속 받으며 PR에서는 기존 이미지도 빌드, main 병합 뒤에는 기존 설정대로 게시가 발생합니다. 기존 OPA 정책과 DB 동작은 수정하지 않았습니다.

## 공식 근거와 용어

[아마존 IAM 수집](https://docs.aws.amazon.com/IAM/latest/APIReference/API_GetAccountAuthorizationDetails.html), [구글 버킷 정책 병합·버전](https://docs.cloud.google.com/storage/docs/access-control/using-iam-permissions), [구글 Storage 오류 분류](https://docs.cloud.google.com/storage/docs/json_api/v1/status-codes), [Keycloak 관리 인터페이스](https://www.keycloak.org/docs-api/latest/rest-api/index.html), [Kubernetes 버전·페이지 처리](https://kubernetes.io/docs/reference/using-api/api-concepts/), [OpenStack 역할 연결](https://docs.openstack.org/openstacksdk/latest/user/proxies/identity_v3.html)을 확인했습니다.

IAM은 신원 및 접근 관리, API는 프로그램 간 호출 인터페이스, SDK는 공식 개발 도구 묶음, SA는 프로그램용 서비스 계정, ARN은 아마존 자원 식별자, OPA는 정책 판정 도구, UID는 변하지 않는 객체 식별자, etag/resourceVersion은 동시 변경을 확인하는 버전 식별값입니다. wheel은 설치형 Python 배포 파일, PR은 브랜치 변경 검토 요청입니다.
