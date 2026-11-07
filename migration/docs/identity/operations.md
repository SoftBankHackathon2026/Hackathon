# 계정·권한 이관 운영 안내

## 전제와 입력

아래 명령은 저장소 루트에서 실행합니다. `migration[identity,dev]`를 별도 가상환경에 설치해야 합니다. 기준 소스는 열린 Keycloak 사전 준비 PR #27의 `2942dfa`입니다. 구현 PR은 해당 브랜치를 대상으로 하며 사전 준비 PR 병합 후 기본 브랜치로 대상을 바꿔야 합니다.

`identity-examples/config.example.json`을 비공개 작업 경로에 복사해 실제 값을 지정합니다. JSON·YAML을 읽지만 중복 키·알 수 없는 최상위/설정 필드·잘못된 형식은 거절합니다.

| 입력 | 의미 |
|---|---|
| `migration_id` | 이관 소유 범위. 다른 이관과 상태 디렉터리를 공유하지 않음 |
| `source.instance_arn / identity_store_id / region` | 자체 디렉터리가 연결된 Identity Center 인스턴스 |
| `source.account_ids` | 조사할 AWS 계정 목록. 계정 자동 전체 탐색·조직 생성 없음 |
| `source.profile` | Identity Store와 SSO 관리 조회용 AWS 실행 프로필. 생략하면 표준 인증 경로 사용 |
| `source.account_profiles` | 계정 ID → IAM 정책 조회용 로컬 AWS 프로필. 각각 STS로 실제 계정을 대조 |
| `targets.keycloak` | 암호화 접속 주소, `master`가 아닌 전용 업무 realm, 관리자 토큰 환경변수 참조, 선택적 CA 인증서 파일 |
| `targets.keycloak.infra_result` | PR #27의 `inspect` 또는 설치/검증 결과 파일. 접속 주소와 공개키 검사를 대조하며 관리자 권한 검증을 대체하지 않음 |
| `targets.openbao` | 주소·토큰 환경변수·전용 `identity-*` 인증 경로·`identity-*` 토큰 수신 대상·선택적 CA |
| `targets.kubernetes` | 정적 접속 설정 파일·문맥·그룹 접두어·`identity-*` 토큰 수신 대상·정확한 로그인 복귀 주소 |
| `targets.applications` | 앱 식별자·전용 `identity-*` 공개 client·정확한 로그인 복귀 주소 |

AWS 인증은 Boto3의 표준 경로로 해석하며 토큰·키를 작업 명세에 넣지 않습니다. 대상 토큰은 실행 시 환경변수에서만 읽습니다. 대상 주소는 HTTPS(인증서로 보호되는 암호화 통신)만 허용하고 인증서 검사를 생략할 수 없습니다. 요청이 다른 주소로 이동하도록 응답해도 따라가지 않습니다. 환경 프록시·netrc 인증은 사용하지 않습니다.

Kubernetes 접속 설정은 토큰, 토큰 파일 또는 클라이언트 인증서·키를 지원합니다. 인증서의 파일 경로와 내장된 인증서 자료 모두 지원하며 임시 비밀 파일은 권한 0600입니다. `exec` 플러그인과 외부 인증 공급자 자동 실행, 인증서 검증 생략, 사용자/비밀번호 인증은 지원하지 않습니다. 플랫폼에서 유효한 정적 자격증명을 준비해야 합니다. 사용자 검증에서는 관리자의 토큰·클라이언트 인증서·키를 사용하지 않고 지정 사용자 토큰으로 완전히 대체합니다.

원본 조회 권한은 [읽기 전용 예제](../../identity-examples/source-read-policy.example.json)를 참고하십시오. 정책 예제의 `Resource: "*"`는 목록 조회 등이 필요한 수집 계정용이며, 이관할 사용자에게 부여하는 권한이 아닙니다. 조직에서 실제 대상 인스턴스와 계정 범위로 제한해 사용하십시오.

## 실행 순서

```bash
python -m migration.identity collect --config /run/platform/identity.local.json --out migration/identity-results/inventory.json
python -m migration.identity mapping --inventory migration/identity-results/inventory.json --out migration/identity-results/mapping.json
```

`collect`는 서비스별 전체 페이지를 조회합니다. 고객 관리형 정책은 각 계정의 실제 기본 버전 문서를 읽습니다. IAM과 Identity Center의 페이지 규약도 각각 처리합니다. 한 범위라도 실패하면 해당 범위를 `unknown`으로 남기고 `partial`을 반환합니다. `mapping`은 검토할 틀을 생성하며 종료 코드 3을 반환합니다. 아직 검토되지 않았다는 뜻이지 명령 오류가 아닙니다.

대응표의 각 `bindings[]`에는 다음을 입력합니다.

- 생성된 `assignment_id`와 `source`는 유지합니다.
- `business`, `environment`, `review_ref`와 `source_access_evidence`를 채웁니다.
- `source_access_evidence`에는 원본 자원 범위·조직 제한·자원 정책·EKS 접근 설정 등 별도 조사 근거를 연결합니다. 단순 임의 문자열이 원본 실효 권한의 등가성을 증명하지는 않습니다.
- 대상에 `kubernetes.namespace/rules`, `openbao[].path/capabilities`, `applications[].id/roles` 중 필요한 것을 입력합니다.
- 이관하지 않을 할당은 `excluded_assignments`로 이동하고 사유를 기록합니다. 제외도 보고서에 남습니다.

초기 지원은 Kubernetes 전용 사용 구획의 안전한 자원 목록에 대한 `get/list/watch`, OpenBao KV v2의 정확한 데이터 경로에 대한 `read`입니다. Kubernetes Secret, 실행 명령, 역할 관리, 수정·삭제, 포괄 권한은 지원하지 않습니다. 대상 네임스페이스와 OpenBao KV v2 저장 엔진은 미리 존재해야 합니다. 새 저장 엔진·앱·업무 데이터를 암묵적으로 만들지 않습니다.

```bash
python -m migration.identity plan --config /run/platform/identity.local.json --inventory migration/identity-results/inventory.json --mapping migration/identity-results/mapping.json --out migration/identity-results/plan.json
python -m migration.identity report --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --out migration/identity-results/review.md
python -m migration.identity export --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --out migration/identity-results/handoffs.json
python -m migration.identity preflight --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --out migration/identity-results/preflight.json
```

`plan`이 차단되었어도 계획 파일 안에 차단 사유가 저장됩니다. 차단된 계획은 적용할 수 없습니다. 원본이나 대응표가 바뀌면 새 계획을 만듭니다. 무결성 해시는 수정 감지용이며 전자서명·사용자 인증이 아닙니다. 산출물과 입력 경로에 접근할 수 있는 운영자를 신뢰하는 실행 모델입니다.

`export`는 제어 서버용 구조화 인증 설정, 앱용 발급자·client·역할 정보, 자격증명 등록 요구 사항을 내보냅니다. 파일 생성이 실제 서버 반영을 뜻하지 않습니다. 제어 서버 배포판·기존 기동 설정·앱 설정 저장 방식이 미확정이므로 이를 자동 변경하지 않습니다. 생성된 인증 설정은 필요한 항목을 기존 설정에 병합하고 인증서 신뢰·기동 인자·마운트 경로·기존 로그인 경로 보존을 운영자가 검증해야 합니다.

사전검사에서 기존 객체가 원하는 상태와 다르면 차단합니다. 기존 사용자 자동 병합이나 기존 realm/client/역할/정책 덮어쓰기는 없습니다. 소유 범위가 제한된 새 객체를 생성하거나 동일 상태를 그대로 사용합니다. Keycloak 이 순서 없이 돌려주는 redirect URI 는 정렬해 비교합니다. 적용 직전에도 계획의 Kubernetes Role 규칙과 OpenBao 경로를 다시 검사해, 해시만 다시 계산한 계획 수정으로 넓은 권한을 적용할 수 없습니다. 새 사용자는 비활성 상태로 생성하며 비밀번호 설정과 다중 인증 등록 후 별도 절차로 활성화해야 합니다. Keycloak 24 이상은 선언하지 않은 사용자 속성을 기본으로 버리므로, 이 도구가 새로 만든 realm 은 사용자 프로필의 `unmanagedAttributePolicy` 를 `ADMIN_EDIT` 으로 바꿔 `identity.*` 속성(관리자만 보고 고침)을 남깁니다. 이미 있던 realm 은 바꾸지 않습니다.

계획의 `plan_hash`를 확인해 아래 `<검토한 plan_hash>` 자리에 전달합니다. 플랫폼이 사용자 검토 결과를 보관하고 이 값을 전달할 수 있습니다. 이 값 자체는 관리자 권한이나 사람의 승인 여부를 인증하지 않습니다.

```bash
python -m migration.identity apply --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --preflight migration/identity-results/preflight.json --state-dir migration/identity-state/orders --confirm-plan '<검토한 plan_hash>' --out migration/identity-results/application.json
python -m migration.identity status --state-dir migration/identity-state/orders
python -m migration.identity resume --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --preflight migration/identity-results/preflight.json --state-dir migration/identity-state/orders --confirm-plan '<검토한 plan_hash>' --out migration/identity-results/resumed.json
```

사전검사 당시 상태와 다른 객체가 생기면 적용을 중단합니다. 쓰기 전에 `applying`을 영속 기록하며 응답 유실은 `unknown`으로 남깁니다. 재개는 같은 작업을 바로 재시도하지 않고 실제 객체부터 다시 읽습니다. 응답 유실 후 같은 상태가 확인되어도 이 도구가 생성했다고 단정하지 않으므로 자동 복구 소유권을 부여하지 않습니다.

`complete`는 요청 명령을 완료했다는 뜻입니다. 구성 적용 완료와 전체 이관 완료를 구분하며 `migration_status`로 후속 검증 상태를 표시합니다. 원본 정책의 지속 동기화·자동 권한 회수·원본 사용자 삭제·키 폐기는 수행하지 않습니다.

## 접근 검증

```bash
python -m migration.identity verify --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --checks /run/platform/identity-checks.json --out migration/identity-results/verification.json
python -m migration.identity report --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --state-dir migration/identity-state/orders --verification migration/identity-results/verification.json --out migration/identity-results/final-report.md
```

`checks.example.json`의 `expected_subject`는 이관 대상 Keycloak 사용자 UUID로 바꿉니다. 환경변수에는 직접 발급받은 유효한 사용자 토큰을 주입합니다. Keycloak의 그룹 전달과 수신 대상이 검증되는 토큰을 사용해야 합니다. 사용자마다 자격증명 등록이 끝났다는 사실은 이 도구의 구성 비교로 확인할 수 없습니다.

- Kubernetes는 사용자 토큰으로 `SelfSubjectReview`를 호출하여 주체·그룹을 확인하고 `SelfSubjectAccessReview`로 서버의 권한 판단을 확인합니다. 이 검사는 실제 데이터 조회나 업무 거래 시험과 구분됩니다. 쓰기·삭제 검증도 실제 변경하지 않고 권한 판단만 요청합니다.
- OpenBao는 계획에서 만든 역할에 사용자 JWT로 로그인한 뒤 실제 비밀 경로를 읽어 허용·거부를 확인하고 시험 토큰을 회수합니다. 읽힌 비밀 원문은 산출물·출력에 포함하지 않습니다. 허용 시험 경로에는 비밀이 존재해야 하며 404를 권한 거부로 간주하지 않습니다.
- 대상 권한 그룹마다 허용·거부 사례가 모두 있어야 해당 범위가 검증된 것으로 표시합니다. 부정 시험은 다른 사용 구획·서비스·작업 등 실제 경계 사례로 구성하십시오.
- 이 명령은 자격증명 등록·앱 설정 반영·제어 서버 설정 반영을 전체 인증할 수 없어 최종 이관 상태를 계속 부분 완료로 보고합니다. `configuration_status`, `access_status`, `missing_access_coverage`, `handoffs`를 각각 확인해야 합니다.

## 중단과 복구

```bash
python -m migration.identity rollback-plan --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --state-dir migration/identity-state/orders --out migration/identity-results/rollback.json
python -m migration.identity rollback --config /run/platform/identity.local.json --plan migration/identity-results/plan.json --state-dir migration/identity-state/orders --confirm-plan '<검토한 plan_hash>' --out migration/identity-results/rollback-result.json
```

첫 명령은 읽기 전용입니다. 복구 적용은 이 실행이 생성한 것으로 확인되는 그룹 구성원 연결·앱 역할 연결·Kubernetes RoleBinding·OpenBao 역할과 정책만 회수합니다. 대상이 변경되었으면 중단합니다. 사용자·realm·그룹·client·Role·인증 경로·업무 데이터는 보존합니다. 이미 존재했던 동일 객체나 생성 여부 불명인 객체는 회수하지 않습니다.

OpenBao 역할 삭제만으로 발급된 모든 토큰이 즉시 회수되는 것으로 간주하면 안 됩니다. 새 정책도 회수하고 잔여 세션과 감사 기록을 확인하십시오. Keycloak 토큰·앱 세션 전체 폐기나 신원 삭제는 별도 운영 절차입니다. 복구된 계획을 `resume`할 수 없으며 다시 검토해 새 계획과 상태 디렉터리를 사용해야 합니다.

동시 실행은 같은 상태 디렉터리 안에서 잠금으로 막습니다. 서로 다른 상태 디렉터리나 다른 운영 도구와의 분산 잠금은 아닙니다. 동일 대상 범위를 동시에 적용하지 않도록 플랫폼이 조정해야 합니다.

## 종료 코드·산출물 보호

| 코드 | 상태 | 해석 |
|---|---|---|
| 0 | `complete`, `planned` | 요청 명령의 완료 또는 차단 없는 계획 생성 |
| 1 | `failed` | 접속·처리·검증 실패 |
| 2 | `blocked` | 입력·정책·검토·대상 충돌로 차단 |
| 3 | `partial`, `review_required` | 수집/적용/검증 일부 미확인 또는 대응표 검토 필요 |

표준 출력은 단일 JSON입니다. `--out`을 지정하면 상세 내용은 해당 파일에 권한 0600으로 원자적으로 저장하고 표준 출력은 요약합니다. 상태 디렉터리는 0700, SQLite·잠금은 0600입니다. 비밀 원문을 저장하지 않더라도 사용자 목록·권한·정책은 민감한 운영 정보이므로 저장소에 커밋하지 마십시오. 예제만 가상 자료입니다.

`report`와 `export`도 차단된 계획의 사유를 읽을 수 있어야 하며 운영 적용은 `plan`과 사전검사를 모두 통과해야 합니다.
