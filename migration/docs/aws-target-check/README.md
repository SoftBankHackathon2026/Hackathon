# AWS 대상 기반 점검

온프레미스에서 AWS로 이전할 때 "대상 기반 점검" 단계에서 사용하는 읽기 전용 도구입니다. 지정한 AWS 계정·리전이 이전 대상으로 준비됐는지 레지스트리, 비밀 저장소, 인증, 감사로그, 데이터 대상 순서로 확인합니다. 클라우드 → 온프레미스 방향의 [Harbor](../harbor-infra/README.md)·[OpenBao](../openbao-infra/README.md)·Keycloak·[Wazuh](../wazuh-infra/README.md) 기반 점검에 대응하는 AWS 쪽 점검입니다.

구현 경로는 `migration/migration/aws_target_check/`입니다. AWS 자원을 만들거나 바꾸지 않으며 조회 API만 호출합니다. 부족한 기반을 자동으로 구성하지 않습니다.

## 설치와 입력

Python 3.12 이상이 필요합니다. AWS 접속 라이브러리는 이전 도구 이미지가 이미 포함하는 `policy-runtime` 추가 구성을 재사용합니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install './migration[policy-runtime]'
```

[예시 설정](../../config.aws-target-check.example.yaml)을 복사하고 `<계정 ID>`, `<리전>` 같은 자리 표시를 실제 값으로 바꿉니다. 예시는 자리 표시를 포함하므로 그대로 실행하면 `INVALID_CONFIG`로 차단됩니다.

- AWS 자격증명은 boto3(AWS용 Python 라이브러리)의 기본 방식이나 설정의 `profile`에서 받습니다. 설정 파일에 접근 키를 넣는 항목은 없으며, 알 수 없는 항목은 거부합니다.
- 점검 전에 `sts:GetCallerIdentity`로 자격증명의 계정이 `account_id`와 같은지 확인합니다. 다르면 다른 점검을 호출하지 않고 `AWS_ACCOUNT_MISMATCH`로 차단합니다.
- `registry`, `secret_store`, `identity`, `audit_log` 네 항목은 반드시 있어야 하고, `data_target`은 선택입니다. 각 항목의 `required`는 기본값이 `true`입니다. `false`로 두면 결과에는 표시하지만 전체 판정에는 반영하지 않습니다.

## 명령

```sh
.venv/bin/python -m migration.aws_target_check plan --config /secure/aws-target.yaml
.venv/bin/python -m migration.aws_target_check check --config /secure/aws-target.yaml
```

`plan`은 AWS에 접속하지 않습니다. 설정을 검증하고 점검 항목별로 호출할 IAM 권한 목록을 돌려줍니다. 실행 역할의 권한을 준비할 때 사용합니다. `check`는 실제 조회를 수행합니다.

명령은 표준 출력에 JSON 한 건을 출력합니다. `plan`의 `planned`와 `check`의 `ready`는 종료 코드 0, 그 밖의 상태는 3입니다. 잘못된 명령 인자는 2입니다.

| 상태 | 의미 |
| --- | --- |
| `ready` | 필수 항목이 모두 통과 |
| `not_ready` | 필수 항목 중 하나 이상이 실패(`fail`) |
| `unverified` | 필수 항목에 실패는 없지만 권한 부족·통신 오류(`error`)로 판정하지 못한 항목이 있음 |
| `blocked` | 설정 오류, 자격증명 없음, 계정 불일치로 점검을 시작하지 않음 |
| `failed` | 예상하지 못한 실행 오류. 원문 예외는 출력하지 않음 |

`groups`의 각 항목은 `pass`·`fail`·`error` 중 하나이고, 세부 `items`는 `name`, `status`, 짧은 `code`, 필요할 때 비밀이 아닌 `detail`을 가집니다. 한 항목에 실패와 오류가 섞이면 `fail`로 판정합니다. 필수 항목의 미통과 코드는 `reason_codes`, 선택 항목의 코드는 `optional_reason_codes`에 모읍니다. ECR 인증 토큰, 비밀 목록, 키·추적·자원 ARN은 결과에 넣지 않습니다.

## 점검 항목

**레지스트리(`registry`)**: `DescribeRegistry`로 ECR에 접속되고 레지스트리 ID가 `account_id`와 같은지 확인합니다. `GetAuthorizationToken`으로 이미지 이관에 필요한 인증 토큰이 발급되는지 확인하고, 토큰 값은 버립니다. `repositories`를 지정하면 저장소마다 존재 여부를 확인합니다. `scan_on_push: true`이면 저장소의 푸시 시 검사 설정이나 레지스트리 수준 검사 규칙(`SCAN_ON_PUSH`, `CONTINUOUS_SCAN`과 와일드카드 필터)이 해당 저장소에 적용되는지 확인합니다. 고급 검사(`ENHANCED`)에서는 레지스트리 규칙만 인정합니다. `encryption`을 지정하면 저장소 암호화 방식(`AES256`, `KMS`, `KMS_DSSE`)이 같은지 비교합니다.

**비밀 저장소(`secret_store`)**: `ListSecrets`를 한 건만 요청해 Secrets Manager 접근을 확인합니다. 비밀 이름이나 값은 읽지 않습니다. `kms_key`(키 ID·별칭·ARN)를 `DescribeKey`로 조회해 키가 있고 `Enabled` 상태이며, Secrets Manager가 사용할 수 있는 대칭 암호화 키(`SYMMETRIC_DEFAULT`, `ENCRYPT_DECRYPT`)인지 확인합니다. 비활성·삭제 대기 키는 실패입니다.

**인증(`identity`)**: `ListInstances`로 IAM Identity Center 인스턴스를 확인합니다. 인스턴스가 여러 개면 `instance_arn`을 지정해야 하며, 상태가 `ACTIVE`가 아니면 실패입니다. `permission_sets`를 지정하면 `ListPermissionSets`와 `DescribePermissionSet`으로 이름을 대조해 이름마다 결과를 냅니다. Identity Center는 활성화한 리전에서만 조회되므로 대상 리전과 다르면 `identity.region`을 지정합니다.

**감사로그(`audit_log`)**: `DescribeTrails`(다른 리전에서 복제된 추적 포함)와 추적마다 `GetTrailStatus`를 호출합니다. 기록 중인 추적이 하나 이상이면 통과입니다. `multi_region: true`(기본값)이면 그 추적이 다중 리전 추적이어야 합니다. 추적이 없으면 `CLOUDTRAIL_TRAIL_MISSING`, 기록 중인 추적이 없으면 `CLOUDTRAIL_NOT_LOGGING`, 단일 리전 추적만 기록 중이면 `CLOUDTRAIL_NOT_MULTI_REGION`입니다.

**데이터 대상(`data_target`, 선택)**: `rds_instance`를 `DescribeDBInstances`로 조회해 `available` 상태인지, 공개 접근이 꺼져 있는지 확인합니다. `approved_regions`를 지정하면 인스턴스 리전이 목록에 있는지 확인합니다. `eks_cluster`는 `DescribeCluster`로 `ACTIVE` 상태인지 확인합니다.

## 필요한 IAM 권한

모두 조회 권한입니다. `plan` 결과의 `actions`는 설정에서 실제로 쓰는 권한만 나열합니다. `sts:GetCallerIdentity`는 별도 권한 없이 호출됩니다.

| 항목 | 권한 |
| --- | --- |
| 계정 확인 | `sts:GetCallerIdentity` |
| 레지스트리 | `ecr:DescribeRegistry`, `ecr:GetAuthorizationToken`, `ecr:DescribeRepositories`, `ecr:GetRegistryScanningConfiguration` |
| 비밀 저장소 | `secretsmanager:ListSecrets`, `kms:DescribeKey` |
| 인증 | `sso:ListInstances`, `sso:ListPermissionSets`, `sso:DescribePermissionSet` |
| 감사로그 | `cloudtrail:DescribeTrails`, `cloudtrail:GetTrailStatus` |
| 데이터 대상 | `rds:DescribeDBInstances`, `eks:DescribeCluster` |

`kms:DescribeKey`는 키 정책이 IAM 정책 위임을 허용하거나 실행 역할을 직접 허용해야 합니다. 권한이 부족하면 해당 항목은 `AWS_ACCESS_DENIED` 오류가 되고 전체 상태는 `unverified`입니다.

## 한계

- 존재·상태·설정 일치만 확인합니다. 실제 이미지 푸시, 비밀 생성, 권한 세트 할당, 감사 이벤트 도착은 시험하지 않습니다. 해당 작업은 이후 이관 단계에서 확인합니다.
- CloudTrail은 추적만 봅니다. 이벤트 선택기, 저장 버킷 정책, CloudTrail Lake 이벤트 데이터 저장소는 확인하지 않습니다.
- RDS는 단일 DB 인스턴스만 확인합니다. Aurora 클러스터, 저장 암호화, 보안 그룹·서브넷 경로는 확인하지 않습니다.
- 권한 세트는 이름만 대조합니다. 포함된 정책과 계정 할당은 확인하지 않습니다.
- 실제 AWS 계정에서의 시험은 아직 수행하지 않았습니다. 단위 시험은 가짜 AWS 클라이언트로 응답 처리와 판정 규칙을 검증합니다.
- 플랫폼의 단계 계획(`task_plan`)에 온프레미스 → AWS 방향 도구로 연결하는 작업은 별도로 진행합니다.
