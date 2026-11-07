# 실행·검증·교체 안내

이 명령은 운영 환경에 자동 실행하지 않습니다. 문서의 `/secure` 경로와 계정·주소는 예시이며 실제 승인된 입력으로 교체해야 합니다. 구현 설명은 [모듈 README](../../migration/secrets/README.md)를 참고하십시오.

## 준비 결과와 권한

1. `migration.openbao_infra configure`, `verify`, `issue-migration-token`을 별도 준비 실행기에서 수행합니다.
2. 공개 신뢰 인증서, configure·verify의 구조화된 결과, 단기 토큰 보호 파일을 실행기에 전달합니다. 결과는 같은 준비 요청의 것이어야 합니다. 토큰 결과의 `credential_ref` 절대 경로 앞에 `file:`을 붙여 설정합니다.
3. 준비·이관·운영자·키 인계·앱 검사는 구분됩니다. 이관 도구는 service·migration 검사를 필수로 받고 나머지 미완료 상태를 보존합니다. 설치기의 백업 복원 시험 미실시 상태가 자동으로 합격하는 것은 아닙니다.
4. PR #51의 계정 도구에서 설정, 원본 조사, 차단 없는 계획을 준비합니다. 사용자 비밀 경로는 이관 경로와 정확히 일치시킵니다. 예: `kv/data/migration/orders/dev/database`.

계정 대응이 아직 없는 환경은 `identity`를 생략하거나 null로 두고 대응표에서 `access_mode: deferred`, `identity_assignments: []`를 명시합니다. 이는 제한된 이관 실행기로 값을 적재하는 방식이며 소비자의 권한·원본 신원 조사와 검증은 미완료입니다. 사용자 권한을 부여하거나 기존 프로그램 실행 신원을 자동 변환하지 않습니다. 기본 `access_mode: identity`에서는 계정 계획과 정확한 할당을 요구합니다.

AWS 실행 신원에는 승인된 범위의 `ListSecrets`, `DescribeSecret`, `ListSecretVersionIds`, `GetResourcePolicy`, `GetSecretValue`, 계정 확인 권한이 필요합니다. 고객 관리형 암호화 키에는 `DescribeKey`, `GetKeyPolicy`, 실제 비밀 조회에 필요한 `Decrypt`도 필요합니다. 원본 쓰기·정책 변경 권한은 사용하지 않습니다. 다른 계정·리전은 별도 설정으로 실행하며, 기본 인증 체인이 잘못된 계정을 선택해도 실행 전 계정 식별 대조로 차단합니다.

OpenBao 이관 토큰에는 지정 데이터 경로의 생성·수정·조회가 필요합니다. 전역 저장소 조회, 메타데이터 변경, 삭제나 정책 관리 권한을 추가하지 않습니다. 인증서 확인을 해제하거나 초기 관리자 토큰으로 대체하지 마십시오. 단기 토큰이 만료되면 새 토큰을 전달하고 새 실행 프로세스로 같은 계획·기록을 재개합니다. 토큰 파일을 실행 도중 바꾸면 중단합니다.

## 조사와 검토

저장소 루트에서 전용 Python 환경을 사용합니다. 예시는 `python`이 해당 환경의 실행 파일이라는 전제입니다.

```sh
install -d -m 700 /secure/secret-import
install -m 600 migration/secrets-examples/config.example.yaml /secure/secret-import/config.yaml
# 설정의 계정·주소·인증서·보호 파일 경로를 실제 값으로 편집합니다.
python -m migration.secrets validate --config /secure/secret-import/config.yaml
python -m migration.secrets inventory --config /secure/secret-import/config.yaml --out /secure/secret-import/inventory.json
python -m migration.secrets mapping-template --config /secure/secret-import/config.yaml --inventory /secure/secret-import/inventory.json --out /secure/secret-import/mapping.json
```

템플릿은 고유 식별자에 기반한 경로 후보를 만들지만 업무·환경·계정 할당·검토 근거를 추측하지 않습니다. 빈 항목을 채우고 계정 도구의 경로와 일치시킨 뒤 계획을 생성합니다. 선택된 비밀을 조용히 생략하지 않으며 범위에서 제외하려면 선택 설정을 바꾸어 다시 조사해야 합니다.

목록·메타데이터 조회에 실패하면 `partial`과 범위별 누락 사유를 결과 파일에 보관합니다. 이를 비밀 부재나 완전한 조사로 판단하지 않습니다. 이런 결과로는 실행 계획을 통과할 수 없습니다.

```sh
python -m migration.secrets plan --config /secure/secret-import/config.yaml --inventory /secure/secret-import/inventory.json --mapping /secure/secret-import/mapping.json --out /secure/secret-import/plan.json
python -m migration.secrets preflight --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --out /secure/secret-import/preflight.json
```

계획에 포함되는 원본 정책·태그·신원 정보도 민감한 운영 정보입니다. 일반 로그나 저장소에 올리지 않습니다. 조사·계획·사전검사는 `--out`을 요구하고 표준 출력에는 결과 위치와 상태만 반환합니다. 공개 예제 파일을 운영 입력으로 바로 읽지 않으며 사용자 소유·0600 파일로 복사해야 합니다.

## 복사·재개·검증

`PLAN_HASH`에는 검토한 계획의 `plan_hash` 값을 넣습니다. 이 값은 입력 일치를 확인하며 승인자나 호출자를 인증하지 않습니다.

```sh
python -m migration.secrets apply --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --preflight /secure/secret-import/preflight.json --state /secure/secret-import/copy.sqlite3 --confirm-plan PLAN_HASH
python -m migration.secrets resume --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --preflight /secure/secret-import/preflight.json --state /secure/secret-import/copy.sqlite3 --confirm-plan PLAN_HASH
python -m migration.secrets verify --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --preflight /secure/secret-import/preflight.json --state /secure/secret-import/copy.sqlite3
python -m migration.secrets report --config /secure/secret-import/config.yaml --state /secure/secret-import/copy.sqlite3
```

현재·직전의 원본 버전 식별자를 지정해 읽고 값·형식을 재비교합니다. 원본 목록·정책·버전 표식이 바뀌면 중단하며 새 계획을 기존 기록에 임의로 연결하지 않습니다. 동일 주소의 저장소가 교체되거나 저장 엔진이 다시 생성되어도 식별자를 대조해 차단합니다.

쓰기 성공 여부를 모르면 기록을 남기고 중단합니다. 재개 시 같은 작업·원본 연결·값을 모두 확인해야 성공으로 대조합니다. 원격 값이 보이지 않는 불명 요청은 지연 반영 가능성이 있어 무조건 재쓰기하지 않습니다. 운영자가 실제 상태를 조사해야 합니다. 현재 자동 원본 정지·상시 동기화·변경 중 무중단 전환은 제공하지 않습니다.

## 계정 이관의 접근 검증 결과 대조

복사 후 계정 도구에서 해당 비밀 경로와 범위 밖 경로에 대해 실제 사용자 허용·거부 검사를 실행합니다. 그 도구가 사용한 원래 `checks` 파일도 함께 전달합니다.

```sh
python -m migration.secrets access-report --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --identity-application /secure/identity-application.json --identity-verification /secure/identity-verification.json --identity-checks /secure/identity-checks.json --out /secure/secret-import/access-report.json
```

다른 비밀의 조회 성공이나 다른 사용자의 거부 결과를 합쳐 현재 비밀의 접근 검증 완료로 표시하지 않습니다. 결과는 공급된 과거 증거 대조이며 서명된 실행 증명이나 새 사용자 시험이 아닙니다. 사람이 비활성 상태라 로그인하지 못한 경우에도 전체 이관 완료로 표시하지 않습니다.

## PostgreSQL 교체

예시는 단일 문자열과 JSON 형식으로 각각 제공합니다. JSON은 `/credentials/password` 같은 명시적 포인터를 사용합니다. 배열·없는 필드·문자열이 아닌 필드는 거부합니다. 계정·접속 주소·데이터베이스를 JSON에서 추측하지 않고 교체 명세의 대상과 서버 고유 식별자로 고정합니다. 관리 계정에는 서버 식별 조회와 대상 역할 비밀번호 변경에 필요한 권한이 있어야 합니다. 대상은 특권을 갖거나 특권 그룹을 상속하지 않는 기존 로그인 역할이어야 합니다.

앱 담당자가 새로운 값을 사용하는 방법과 연결 갱신·운영 실패 대응을 준비한 뒤 준비 확인을 작성합니다. 원본 자동 교체도 담당자가 실제로 중지해야 합니다. `issued_at`, `expires_at`은 현재 시각 기준, 유효 기간은 최대 30분입니다. 예제의 거짓 확인 값을 기계적으로 참으로 바꾸는 것으로 준비를 대신하지 않습니다.

```sh
python -m migration.secrets rotate-postgres --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --preflight /secure/secret-import/preflight.json --state /secure/secret-import/copy.sqlite3 --confirm-plan PLAN_HASH --source-arn FULL_SOURCE_ARN --spec /secure/postgres-json.json --readiness /secure/rotation-readiness.json --operation-id orders-rotation-001
python -m migration.secrets check-postgres --config /secure/secret-import/config.yaml --plan /secure/secret-import/plan.json --preflight /secure/secret-import/preflight.json --state /secure/secret-import/copy.sqlite3 --source-arn FULL_SOURCE_ARN --spec /secure/postgres-json.json --operation-id orders-rotation-001
```

재개는 같은 교체 명령·작업 식별자·명세로 호출합니다. 준비 확인이 만료되면 같은 작업 범위의 새 확인이 필요합니다. 후보는 OpenBao에 보관하며 교체 기록에는 원문을 쓰지 않습니다. 교체 중 명세·대상·현재 문서가 바뀌면 중단합니다. 새 작업으로 미확정 작업을 우회할 수 없습니다. 교체 완료 재실행은 현재 후보로 실제 접속을 확인하고 값을 새로 생성하지 않습니다.

외부 변경이 성공한 뒤 참조 변경에 실패할 수 있으므로 자동 원상 복구를 제공하지 않습니다. OpenBao의 pending 후보와 실제 데이터베이스를 조사하고 같은 후보로 재개합니다. 앱 변경·접속 중단·연결 풀의 실제 동작은 이 도구가 검증하지 않습니다. 교체 후 옛 AWS 비밀번호가 유효하다고 가정하지 마십시오.

JSON의 호스트·사용자·데이터베이스 등 다른 필드는 유지합니다. 비밀번호가 대상 서버에서 검증되어도 JSON에 남은 주소가 대상 서버를 가리킨다는 보장은 없습니다. 앱 담당자는 비밀번호 외의 접속정보도 검토·전환해야 합니다. 교체 결과의 `credential_metadata_rewrite_performed`는 거짓입니다.

## 상태와 종료 코드

- 0: 요청한 명령을 완료했습니다. 전체 업무 이관 완료를 의미하지 않습니다.
- 2: 입력·접속·권한·충돌·실행 실패입니다. 고정 사유 코드만 출력합니다.
- 3: 조사 부분 완료, 계획 차단 또는 접근 증거 미완료입니다.

결과는 `copy_status`, `access_status`, `rotation_status`, `application_status`를 분리합니다. 이 도구만으로 앱 전환을 완료할 수 없어 `migration_complete`는 거짓입니다. 상태 파일을 다른 계획에 재사용하거나 수동 수정해 차단을 우회하지 마십시오.

용어: PR은 변경 검토 요청, KMS는 키 관리 서비스, JSON Pointer는 이름·값 자료의 특정 필드를 지정하는 경로, ARN은 AWS 자원의 고유 식별 문자열입니다.
