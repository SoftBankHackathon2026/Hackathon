# AWS 보안로그 이관

선택한 S3 경로와 CloudWatch Logs 그룹의 과거 로그를 온프레미스에 보관합니다. 필요할 때 같은 명령을 다시 실행하면 진행 중인 범위를 재개하거나 새 범위를 처리합니다. 검색용 적재는 Wazuh의 기존 AWS 수집기가 담당합니다.

## 준비와 실행

1. 대상 서버에서 [Wazuh 설치·검증 절차](README.md)를 실행하고, `verify` 결과에서 원본 보관공간 준비를 확인합니다.
2. 해당 서버의 별도 Python 3.12 이상 가상환경에 `python -m pip install './migration[security-logs]'`로 설치합니다. AWS 접속 라이브러리와 인프라 도구의 Kubernetes 접속 라이브러리는 별도 환경에서 사용합니다.
3. [설정 예시](../../config.security-logs.example.yaml)를 복사합니다. Wazuh 검증 결과와 설정 파일을 `oneport-archive` 소유로 전달하고 권한을 0600으로 제한합니다. 여유 공간 기준은 설치 설정의 `storage.reserve_bytes`를 사용합니다. 지정된 실행 계정에 AWS 읽기 자격증명을 제공하고, 검색을 대조할 때는 검색 저장소의 신뢰 인증서와 `oneport_reader` 비밀번호 파일도 제공합니다. 비밀번호 파일은 동일하게 0600으로 제한합니다.
4. 다음 명령을 대상 서버에서 실행합니다. 가상환경 실행 파일과 설정 파일 위치를 환경에 맞춰 지정합니다.

```sh
sudo -u oneport-archive /opt/security-logs/bin/python -m migration.security_logs plan --config /secure/security-logs.yaml
sudo -u oneport-archive /opt/security-logs/bin/python -m migration.security_logs run --config /secure/security-logs.yaml
sudo -u oneport-archive /opt/security-logs/bin/python -m migration.security_logs status --config /secure/security-logs.yaml
sudo -u oneport-archive /opt/security-logs/bin/python -m migration.security_logs verify --config /secure/security-logs.yaml
```

| 명령 | 반환 정보 |
| --- | --- |
| `plan` | S3 목록과 읽기 표본, CloudWatch 조회 결과, 원본 경로 사용량·남은 저장 예산·파일시스템 여유. 부분 S3 목록이나 CloudWatch 조회 표본에서는 전체 용량을 미확인으로 표시합니다. 전체 S3 목록 크기는 기존 보관분을 빼기 전의 최초 복사 추정치입니다. |
| `run` | 원본별 완료한 파일·항목·바이트, 미완료 범위와 사유, S3 조회 이후에 변경되거나 조회 종료 시각보다 늦은 객체의 상태. |
| `status` | 실행 중에도 로컬 작업 기록에서 원본별 완료 수와 진행 중인 조회 종료 시각을 읽습니다. |
| `verify` | 로컬 파일과 [보관 결과 기록](archive-manifest.schema.json)의 크기·SHA-256 파일 요약값을 확인하고, 검색 결과를 일치·누락·중복·대조 불가로 집계합니다. |

각 명령은 표준 출력에 JSON 구조의 결과 하나를 반환합니다. 종료 코드 0은 요청 작업 완료, 3은 부분 완료·차단·실패, 2는 명령 인자 오류입니다. `archive_complete`는 선택한 조회 범위의 원본 보관 상태이고, `search_verified`와 `full_migration_verified`는 `verify`의 대조 결과입니다. 로그 메시지, AWS 키, 조회 비밀번호는 결과에 포함하지 않습니다.

## 조회 범위와 재개

- **S3**는 지정 경로의 현재 객체를 매번 목록화합니다. 객체 키·변경 시각·크기·AWS 객체 버전 비교 태그를 함께 사용해 같은 객체를 건너뛰고, 목록 이후 변경된 객체는 별도 사유를 기록합니다. 전체 파일 SHA-256을 AWS에서 받으면 비교하고, 그 외에는 로컬 검증값만 기록합니다. 이전 객체 버전 목록은 조회 범위에 포함하지 않습니다. 복원이 필요한 객체는 사유를 반환합니다. 복사에 실패했던 객체가 다음 목록에서 사라져도 원본 범위를 완료로 표시하지 않습니다. 원본 키와 사유는 보호된 로컬 작업 기록의 `s3_issues` 표에 남습니다. 조직 공용 버킷은 `bucket_owner`와 해당 계정 로그가 놓인 `prefix`를 지정합니다.
- **CloudWatch Logs**는 첫 실행에서 조회 가능한 과거분을 읽고, 후속 실행에서는 이전 완료 시각보다 기본 14일 전부터 다시 읽습니다. 늦게 도착한 이벤트가 이 기간보다 오래된 경우 `run --full-rescan`으로 전체 보존 범위를 다시 조회할 수 있습니다. 진행 중인 작업이 있으면 먼저 기존 범위를 마친 뒤 다음 실행에서 전체 재조사가 적용됩니다. 이벤트 시각과 수집 시각·스트림·이벤트 식별자·메시지를 줄 단위 JSON으로 묶어 보관합니다. 조회 서비스가 적용한 마스킹은 유지됩니다.
- **저장과 복구**는 원본 보관 경로 아래 `security-logs/`와 검증 결과에 지정된 기록 경로의 `security-logs-*.json`, `security-logs.sqlite3`을 함께 사용합니다. 이름은 원본 키와 별개로 생성합니다. 중단 후에는 같은 설정으로 재실행합니다. 일시 파일과 완료 여부가 불명확한 기록은 최종 파일의 무결성을 확인해 복구합니다. 저장 위치·원본 범위의 변경은 기존 작업 기록과 대조해 차단합니다. 파일시스템 여유와 원본 보관 예산을 다시 확인하고 부족하면 멈춥니다. 원본·기존 보관 파일의 자동 삭제 없이 공간을 확보한 뒤 재개합니다.

AWS 자격증명은 AWS 프로그램의 기본 탐색 순서 또는 지정한 프로필에서 읽습니다. 호출 계정이 원본 계정과 다르면 `credential_account_id`를 명시합니다. S3 목록·객체 읽기 및 필요한 키 복호화 권한과 CloudWatch 그룹의 이벤트 조회 권한이 필요합니다. 이관 도구는 AWS 구성과 데이터를 읽기만 합니다.

## 검색 대조와 인수 기준

검색 대상은 같은 서버의 `127.0.0.1:9200`과 `wazuh-archives-*`입니다. 검색을 시작하려면 원본별로 Wazuh에서 확인한 식별자 필드와 검색 가능한 표본 식별자를 설정합니다. CloudTrail 이벤트 식별자, CloudWatch 이벤트 식별자, CloudWatch 메시지 안의 CloudTrail 식별자를 지원합니다. 원본 식별자가 기록되지 않는 형식, 읽을 수 없는 파일, 조회 일부 실패는 대조 불가로 표시합니다. 압축된 CloudTrail 파일의 대조에는 압축 해제 후 64MiB 상한이 적용됩니다. 대조 건수는 보관한 원본 이벤트의 출현 건수를 기준으로 합니다.

실제 인수에서는 작은 S3 경로와 CloudWatch 로그 그룹의 보관부터 실행하고, 파일과 이벤트의 원본 대조 및 검색 결과를 확인합니다. 같은 설정으로 재실행해 보관 건수가 유지되는지 확인하고 새 이벤트를 추가해 추가분만 늘어나는지 확인합니다. 검색용 과거 로그가 Wazuh에 없다면 누락으로 보고하며 재처리는 운영자가 Wazuh 수집 설정과 중복 가능성을 확인한 뒤 별도로 진행합니다.
