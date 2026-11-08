# 온프레미스 OpenBao 비밀값의 AWS Secrets Manager 이관

`python -m migration.secrets_export`는 온프레미스 OpenBao KV v2의 현재 버전 값을 AWS Secrets Manager에 복사하고 다시 읽어 검증합니다. [AWS → OpenBao 이관 도구](../../migration/secrets/README.md)의 반대 방향이며 같은 설정 엄격성·보호 파일·실행 기록·출력 규약을 사용합니다. **값 복사만 수행합니다. 자격증명 교체, 앱 설정·배포 변경, 원본 삭제는 수행하지 않습니다.**

## 1. 구현 위치와 책임

| 파일 | 역할 |
|---|---|
| `migration/secrets_export/common.py` | 설정 검사, 대상 이름 규칙, 값 요약값(HMAC) 계산 |
| `migration/secrets_export/source.py` | OpenBao 상태·저장 엔진·토큰 확인, 읽기 전용 권한 검사, 경로 목록·현재 버전 조회 |
| `migration/secrets_export/target.py` | 계정 확인, Secrets Manager 조회·생성·새 버전 저장·태그 |
| `migration/secrets_export/plan.py` | 생성/갱신/동일 판단과 차단 사유를 담은 읽기 전용 계획 |
| `migration/secrets_export/engine.py` | 적용·재개·검증, 원문 없는 실행 기록 |

보호 파일 읽기·0600 원자적 저장·산출물 봉인·OpenBao 통신·실행 기록과 잠금은 `migration.secrets`의 구현을 그대로 가져와 사용합니다.

## 2. 실행 흐름

```sh
install -d -m 700 /secure/secret-export
install -m 600 migration/secrets-examples/export.config.example.yaml /secure/secret-export/config.yaml
# 설정의 <...> 값과 /secure 경로를 실제 승인된 입력으로 바꿉니다.
openssl rand -hex 32 > /secure/secrets-export-hash.key && chmod 600 /secure/secrets-export-hash.key
python -m migration.secrets_export validate --config /secure/secret-export/config.yaml
python -m migration.secrets_export plan --config /secure/secret-export/config.yaml --out /secure/secret-export/plan.json
python -m migration.secrets_export apply --config /secure/secret-export/config.yaml --plan /secure/secret-export/plan.json --state /secure/secret-export/export.sqlite3 --confirm-plan PLAN_HASH
python -m migration.secrets_export resume --config /secure/secret-export/config.yaml --plan /secure/secret-export/plan.json --state /secure/secret-export/export.sqlite3 --confirm-plan PLAN_HASH
python -m migration.secrets_export verify --config /secure/secret-export/config.yaml --plan /secure/secret-export/plan.json --state /secure/secret-export/export.sqlite3
python -m migration.secrets_export report --config /secure/secret-export/config.yaml --state /secure/secret-export/export.sqlite3
```

- **plan**은 OpenBao와 Secrets Manager를 읽기만 합니다. 원본 경로마다 대상 이름, 동작(`create`/`update`/`unchanged`/`blocked`), 원본·대상 값 요약값만 기록하며 값은 기록하지 않습니다. 결과는 `--out` 보호 파일로만 저장하고 표준 출력에는 위치와 상태만 반환합니다.
- **apply/resume**은 쓰기 전에 모든 원본의 현재 버전과 값 요약값이 계획과 같은지 확인합니다. 하나라도 다르면 아무것도 쓰지 않고 `SOURCE_CHANGED_REPLAN_REQUIRED`로 멈춥니다. 대상이 없으면 `CreateSecret`, 값이 다르면 `PutSecretValue`로 새 버전을 저장하고, 동일하면 쓰지 않습니다.
- **verify**는 쓰지 않고 Secrets Manager의 `AWSCURRENT` 버전을 다시 읽어 버전 식별자와 값 요약값을 계획과 비교합니다. 실패하면 실행 기록을 복구 필요(`needs_recovery`)로 낮춥니다.

대상 이름은 `name_template`의 `{path}`를 원본 KV 경로로 바꿔 만듭니다. 예: `onprem/{path}` → `onprem/apps/orders/database`. KV 문서의 키·값 전체를 정렬된 JSON 한 줄로 `SecretString`에 저장합니다.

새로 만든 비밀에는 설정한 KMS 키(`kms_key_id`, 생략 시 AWS 관리형 키)와 `secrets-export:migration-id`, `secrets-export:binding`, `secrets-export:source-mount`, `secrets-export:source-path`, `secrets-export:source-version` 태그를 붙입니다. 태그에는 경로·버전·식별자만 있고 값이나 값 요약값은 없습니다.

## 3. 안전 규칙

- **값 요약값은 HMAC-SHA256입니다.** 키는 `hash_key_ref`의 보호 파일(32자 이상)에서 읽습니다. 키가 없으면 계획 파일의 요약값으로 짧은 비밀번호를 대입 추정할 수 없습니다. 키가 바뀌면 기존 계획은 `HASH_KEY_CHANGED`로 거부합니다.
- **소유하지 않은 기존 비밀은 덮어쓰지 않습니다.** 같은 이름의 비밀에 이 도구의 `secrets-export:binding` 태그가 없으면 `TARGET_EXISTS_NOT_OWNED`로 계획을 차단합니다. 검토 후 넘겨받을 때만 `adopt_existing: true`를 사용합니다. 삭제 예약, 자동 교체 설정, 교체 대기 버전, 다른 AWS 서비스가 관리하는 비밀도 차단합니다.
- **계획 이후 대상이 바뀌면 쓰지 않습니다.** 갱신은 계획 시점의 `AWSCURRENT` 버전이 그대로일 때만, 생성은 대상이 여전히 없을 때만 수행합니다.
- **응답 유실은 같은 요청 토큰으로 재개합니다.** 쓰기 요청의 `ClientRequestToken`은 계획·바인딩에서 결정적으로 만든 값이고 실제 버전 식별자가 됩니다. 응답을 받지 못하면 기록을 `unknown`으로 남기고 중단합니다. `resume`은 같은 토큰으로 다시 요청하므로 Secrets Manager가 중복 버전을 만들지 않습니다.
- **원본 토큰은 읽기 전용이어야 합니다.** 매 실행마다 OpenBao 클러스터·버전·KV v2 저장 엔진·비루트 단기 토큰(남은 수명 24시간 이하)을 확인하고, `sys/capabilities-self`로 대상 데이터 경로에 `read` 외 권한이 없는지, 삭제·파기·복원·정책 관리 경로가 거부되는지 검사합니다. 토큰은 `file:/` 보호 파일 참조만 허용하며 설정에 직접 쓰거나 환경변수로 전달할 수 없습니다.
- 모든 설정·계획 입력은 실행 사용자 소유 0600 파일이어야 합니다. 알 수 없는 필드·중복 키·심볼릭 링크는 거부합니다. 오류는 고정 사유 코드만 출력하고 AWS·OpenBao의 오류 문구를 전달하지 않습니다.

## 4. 필요한 권한

AWS 실행 신원(대상 계정·리전 한 쌍):

| 권한 | 용도 |
|---|---|
| `sts:GetCallerIdentity` | 설정한 `<계정 ID>`와 실제 계정 대조 |
| `secretsmanager:DescribeSecret` | 대상 존재·소유 태그·버전 표식 확인 |
| `secretsmanager:GetSecretValue` | 계획의 기존 값 비교, 적용 후·`verify` 재읽기 |
| `secretsmanager:CreateSecret` | 없는 비밀 생성 |
| `secretsmanager:PutSecretValue` | 값이 다른 비밀의 새 버전 저장 |
| `secretsmanager:TagResource` | 생성 시 태그, 갱신 후 원본 버전 태그 |
| `kms:Encrypt`, `kms:GenerateDataKey`, `kms:Decrypt` | 고객 관리형 키를 지정한 경우 그 키에 한해 필요 |

권한 범위는 `name_template`의 접두어(예: `arn:aws:secretsmanager:<리전>:<계정 ID>:secret:onprem/*`)로 제한합니다. 삭제·정책·교체 설정 권한은 사용하지 않습니다.

OpenBao 토큰 정책 예시는 다음과 같습니다. 정확한 경로만 `read`를 부여합니다. `prefixes`를 사용할 때만 해당 접두어의 `metadata` 목록 권한을 추가합니다.

```hcl
path "kv/data/apps/orders/database" { capabilities = ["read"] }
path "kv/metadata/apps/orders/config/*" { capabilities = ["list"] }
path "kv/data/apps/orders/config/*" { capabilities = ["read"] }
```

## 5. 상태와 종료 코드

- 0: 요청한 명령을 완료했습니다. 앱 전환까지 끝났다는 뜻은 아닙니다.
- 2: 입력·접속·권한·충돌·검증 실패입니다. 고정 사유 코드만 출력합니다.
- 3: 계획이 차단되었습니다. `blockers`의 경로별 사유를 해소하고 다시 계획합니다.

결과의 `rotation_status`는 `not_supported_by_this_tool`, `application_status`는 `external_handoff_required`, `migration_complete`는 거짓입니다. 상태 파일은 계획 하나에 묶이므로 새 계획은 새 상태 파일로 실행합니다.

## 6. 한계와 후속 작업

- **자격증명 교체 미지원:** 반대 방향 도구의 PostgreSQL 비밀번호 교체(`rotate-postgres`)에 해당하는 기능이 없습니다. 이관 후 교체는 Secrets Manager 자동 교체나 별도 도구로 진행해야 하며 후속 과제입니다.
- 현재 버전만 복사합니다. 과거 버전·삭제 이력·`custom_metadata`·OpenBao 정책은 옮기지 않습니다.
- KV 문서 전체를 JSON 문자열로 저장합니다. 단일 필드만 문자열로 꺼내 저장하거나 바이너리로 저장하는 형식 변환은 없습니다. 64KiB를 넘는 문서는 차단합니다.
- 기존 비밀의 KMS 키는 바꾸지 않습니다. `kms_key_id`는 새로 만드는 비밀에만 적용됩니다.
- 앱의 비밀 조회 방식 변경, AWS 쪽 IAM 정책 생성, 원본 OpenBao 폐기, 상시 동기화, 여러 실행 호스트 간 분산 잠금은 제공하지 않습니다. 같은 대상에 대한 실행은 플랫폼이 직렬화해야 합니다.
- 실제 AWS 계정과 운영 OpenBao를 사용한 인수 시험은 수행하지 않았습니다. 단위 시험은 가짜 OpenBao 통신과 가짜 Secrets Manager 클라이언트로 수행합니다.

용어: KV는 키·값 저장 기능, KMS는 키 관리 서비스, HMAC은 비밀 키로 계산하는 요약값, ARN은 AWS 자원의 고유 식별 문자열입니다.
