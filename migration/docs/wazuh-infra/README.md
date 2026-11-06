# Wazuh 온프레미스 감사로그 기반 준비

플랫폼이 선택한 기존 Kubernetes 노드 한 대에 Wazuh manager·indexer·Filebeat·dashboard를 운영체제 서비스로 설치합니다. 원본 보관공간과 별도 원본 이관 도구의 연결 규약, 전체 이벤트 검색, 설치 위치별 용량 정보와 Wazuh 수집 제어를 제공합니다.

**실제 원본 파일 다운로드·이관 프로그램과 플랫폼 화면은 이번 구현에 포함하지 않습니다.** AWS 로그 활성화·내보내기·저장소·접근 정책을 생성하거나 변경하지 않습니다. Keycloak 로그인 선택은 인터뷰에서 확정되지 않아 자체 계정을 사용하며, Keycloak·OpenBao의 서버·정책·기존 코드를 변경하지 않습니다. 새로운 위협 탐지 규칙과 자동 대응은 후속 범위입니다.

## 코드 위치와 기존 변경 요청

`migration/migration/wazuh_infra/`의 독립 명령으로 제공합니다. 최신 main에서 분기했으며, 열려 있는 [Keycloak 변경 요청 #27](https://github.com/SoftBankHackathon2026/Hackathon/pull/27)과 [OpenBao 변경 요청 #28](https://github.com/SoftBankHackathon2026/Hackathon/pull/28)의 입력 검증·Ansible 호출·결과 반환 방식을 참고했습니다. 두 변경을 이 브랜치에 합치거나 해당 파일에 실행 의존성을 만들지 않았습니다.

`pyproject.toml`의 선택 의존성과 하위 패키지 탐색, `MANIFEST.in`은 다른 설치 모듈과 공통으로 추가하는 위치이므로 해당 변경 요청을 합칠 때 각 모듈의 항목을 모두 보존해야 합니다. 기존 데이터베이스 이관의 기본 의존성에는 추가하지 않고 `wazuh-infra` 선택 묶음에 분리했습니다.

## 설치와 호출

제어 환경은 Python 3.12 이상입니다. 기본 이관 도구 이미지에는 Ansible 의존성을 추가하지 않으므로 플랫폼의 전용 실행 환경에서 `wazuh-infra` 선택 묶음을 설치해야 합니다. 별도 Ansible 확장 모음 설치는 필요하지 않습니다. 신규 설치 대상은 **Ubuntu 24.04·x86_64·systemd**로 제한합니다. 이는 코드의 허용 범위이며 실제 서버 설치를 검증했다는 뜻은 아닙니다. 원격 접속·설치 권한·공식 패키지 저장소 연결이 필요합니다. 전역 `vm.max_map_count`가 262144보다 작으면 필요한 변경 사유를 반환하며 임의로 운영체제 전역 설정을 바꾸지 않습니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install './migration[wazuh-infra,dev]'
.venv/bin/python -m migration.wazuh_infra --help
```

[예시 설정](../../config.wazuh-infra.example.yaml)의 문서용 주소·노드 식별자·보호 파일 참조를 실제 값으로 바꾸고 설정 파일에 0600 권한을 적용합니다. 원격 노드는 최신 Kubernetes 노드 고유 식별자·machine-id·제공되는 하드웨어 식별값·등록 주소와 대조합니다. 호스트 키 검증을 유지합니다. 컨트롤 플레인 노드는 `allow_control_plane: true`로 명시해야 합니다.

```sh
python -m migration.wazuh_infra discover --config /secure/wazuh.yaml
python -m migration.wazuh_infra assess --config /secure/wazuh.yaml
python -m migration.wazuh_infra preflight --config /secure/wazuh.yaml
python -m migration.wazuh_infra install --config /secure/wazuh.yaml
python -m migration.wazuh_infra configure --config /secure/wazuh.yaml
python -m migration.wazuh_infra verify --config /secure/wazuh.yaml
python -m migration.wazuh_infra control --config /secure/wazuh.yaml
```

| 명령 | 역할 |
| --- | --- |
| `discover` | Kubernetes 파드의 Wazuh 이미지와 제공된 노드의 서비스 후보 조사. 접속하지 못한 노드는 미확인입니다. |
| `inspect` | 직접 입력한 기존 HTTPS 주소의 인증서·접근 상태 조회. 호환 응답만으로 제품을 확정하지 않습니다. |
| `assess` | 선택 서버의 디스크·메모리와 기존 AWS 원본을 읽기 전용으로 조사합니다. |
| `preflight` | 노드 식별·운영체제·자원·포트·기존 경로·설치 소유권을 점검합니다. |
| `install` | 호스트 잠금과 재검사 후 고정 버전 패키지를 설치·설정합니다. 실패 시 데이터를 자동 삭제하지 않습니다. |
| `configure` | 동일 설치 설정을 확인하고 보호 파일에서 AWS 자격증명을 갱신하며 서비스·조회 권한을 검사합니다. 원본 목록·기간 변경은 차단합니다. |
| `verify` | 서비스·설치 버전·조회 허용·관리 거부·보관공간 접근과 선택적인 이벤트 검색을 검사합니다. |
| `control` | `control.action`의 `start`, `pause`, `status`로 이 도구가 관리하는 Wazuh AWS 입력 전체를 제어합니다. |

`inspect`에는 노드 접속 없이 `schema_version`, `request_id`, `endpoint`만 전달할 수 있습니다. 기존 Wazuh는 읽기 전용 점검만 지원하며 이 도구의 관리 표식이 없는 환경에 설정을 덮어쓰지 않습니다. 발견된 모든 사용자 정의 설치·컨테이너·실행 수가 0인 구성의 부재를 보장하지 않습니다.

## 설치 구성과 자격증명

공식 서명된 패키지 저장소에서 Wazuh 4.14.10-1과 Filebeat 7.10.2를 설치하고 보류 상태로 고정합니다. 저장소 서명키·Wazuh Filebeat 모듈 0.5·색인 템플릿의 SHA-256 검증값은 `ansible/versions.yml`에 있습니다. 일괄 설치기의 자동 제거·덮어쓰기 경로는 사용하지 않습니다.

indexer의 9200·9300 포트와 manager API의 55000 포트는 루프백 주소에 바인딩합니다. dashboard만 선택한 노드 주소와 기본 8443 포트에 바인딩합니다. 업무 서버용 에이전트 접속·등록과 자동 대응은 활성화하지 않습니다. 시스템 기본 Java·Kubernetes 설정·전역 방화벽을 변경하지 않습니다. 관리하는 서비스에는 메모리 상한을 적용하며 전체 서버 자원을 예약하는 기능은 아닙니다.

| 자료 | 보관·사용 |
| --- | --- |
| 운영자·조회자 비밀번호 | 플랫폼이 각기 다른 보호 파일 참조로 제공합니다. 길이는 UTF-8 기준 16~72바이트입니다. |
| 내부 서비스 비밀번호·관리 인증서 | 서버의 `/var/lib/oneport-wazuh-infra/`에 제한 접근으로 보관하며 일반 결과로 내보내지 않습니다. |
| AWS 자격증명 | 표준 AWS 자격증명 파일의 지정 프로필에서 읽어 서버의 보호 파일에 복사합니다. `configure`로 같은 참조의 갱신된 값을 반영할 수 있습니다. |
| dashboard 인증서 | 제공된 인증서·개인키·신뢰 체인 또는 설치 중 자체 생성한 인증서를 사용합니다. IP 주소 일치와 통신 검증을 수행합니다. |
| 공개 신뢰 인증서 | 결과의 `trusted_ca_pem`으로 제공합니다. 접속 측이 신뢰 자료로 등록해야 하며 인증서 검증을 끄지 않습니다. |

`oneport_operator`는 관리용, `oneport_reader`는 감사 색인 조회용입니다. `oneport_ingest`는 전달 도구용이며 별도 내부 비밀번호를 사용합니다. dashboard의 manager API 연결에는 `run_as`를 켜므로 조회자를 API 관리자 신원으로 일괄 실행하지 않습니다. Wazuh 앱의 별도 관리 화면 권한 매핑과 Keycloak 로그인은 이번에 구성하지 않습니다. 전체 이벤트 조회는 dashboard의 Discover 화면과 `oneport-archives` 검색 패턴을 사용합니다.

비밀번호·인증서·데이터 위치의 암묵적인 교체를 지원하지 않습니다. 운영자·조회자 비밀번호 내용이 달라지거나 원본 범위·저장 예산·설치 주소가 바뀌면 명시적인 변경 계획이 필요합니다. 비밀 참조는 실행 사용자 소유 일반 파일이어야 하며 타인에게 읽기 권한이 있거나 마지막 경로가 심볼릭 링크이면 거부합니다. AWS 장기 키 생성·회전과 단기 자격증명의 자동 발급은 구현하지 않습니다.

## 수집 원본과 전체 이벤트 검색

S3는 CloudTrail·VPC Flow·Config·GuardDuty·WAF·서버 접근·custom 유형을 입력받습니다. CloudWatch Logs는 `type: cloudwatchlogs`와 로그 그룹을 입력받습니다. 실제 파일 형식과 Wazuh 지원 범위는 현장에서 확인해야 하며 임의 형식의 파일을 검색 가능으로 간주하지 않습니다. [Wazuh 지원 서비스](https://documentation.wazuh.com/current/cloud-security/amazon/services/supported-services/index.html)

S3 조사는 원본마다 `max_pages`(기본 10) 이내의 목록과 첫 객체의 최대 1024바이트 읽기를 사용합니다. 목록·표본을 읽었다고 모든 객체의 접근 권한이나 암호 해독을 검증한 것으로 표현하지 않습니다. 중앙 로그 버킷은 `bucket_owner`와 로그의 `account_id`를 따로 전달합니다. CloudWatch는 호출 계정을 확인하고 제한된 이벤트 조회를 수행하며 원본 전체 크기는 미확인으로 둡니다. 기존 읽기 권한만 사용하고 AWS 자원을 변경하지 않습니다. 조회·네트워크 비용은 발생할 수 있습니다.

기본 `start_date: 1970-JAN-01`로 임의의 최근 기간만 수집하지 않습니다. 처음 시작할 때 Wazuh AWS 모듈의 처리 기록이 생성되므로, 이후 기간·원본을 바꿔 과거 로그를 재처리하는 작업은 자동 실행하지 않습니다. 재처리에는 중복 가능성을 검토한 별도 계획이 필요합니다. [과거 로그와 재처리](https://documentation.wazuh.com/current/cloud-security/amazon/services/prerequisites/considerations.html)

manager의 `logall_json`과 Filebeat archives 전달을 활성화하므로 경보가 없는 이벤트도 검색 대상입니다. `wazuh-archives-*`의 기본 시간 필드는 Wazuh 이벤트의 `timestamp`입니다. 원래 AWS 이벤트 시각은 로그 유형별 필드와 `full_log`에 보존되는지 실제 표본으로 확인해야 하며, 이 구현이 모든 유형의 원본 시각을 동일한 시간 필드로 변환한다고 가정하지 않습니다. [전체 이벤트 색인 설정](https://documentation.wazuh.com/current/user-manual/wazuh-indexer/wazuh-indexer-indices.html)

공식 Filebeat 모듈의 실패 시 버리는 처리는 원문과 실패 사유를 `wazuh-ingest-failures`에 격리하는 처리로 바꿉니다. 설치 중 indexer의 실제 처리 모의 실행으로 해당 처리의 문법과 원문 보존을 검사하며, 검증 명령은 격리된 이벤트 수를 반환합니다. 이는 manager 이전의 대형 이벤트 제한이나 모든 전송 장애의 무손실을 보장하지 않으므로 별도 원본 보관과 전체 범위 대조가 필요합니다.

대량 AWS 수집은 설치 직후 비활성 상태입니다. `control.action: start`를 명시하면 저장 예산·원본 접근을 확인한 뒤 수집을 시작합니다. 일시정지는 해당 manager가 관리하는 AWS 원본 전체를 대상으로 manager를 재시작하며 개별 원본 정지는 지원하지 않습니다. 별도 원본 이관 도구는 실행·정지하지 않습니다. 일시정지·상태 조회·서버 검증은 AWS 자격증명 파일이 만료되거나 제거되어도 수행할 수 있도록 해당 파일을 요구하지 않습니다.

`ingestion_enabled`는 관리하는 입력의 활성화 설정을 의미합니다. 원본별 실제 처리 지연·대기량·최종 수집 성공을 관측하는 기능은 없으며 `ingestion_health: not_observed`를 반환합니다. 이 값을 실제 로그 수집 성공이나 전체 이관 완료로 해석하지 않습니다.

## 용량 정보와 원본 보관 연결

원본 경로와 검색 경로는 플랫폼이 지정합니다. manager 중간 이벤트 경로는 `/var/ossec`로 고정하며 이를 별도 용량 항목에 포함합니다. 저장 위치는 가능하면 파일시스템 고유 식별자로 대조하며, 없으면 장치 번호를 사용합니다. 마운트가 달라지면 시작을 차단하되 일시정지·상태 조회는 허용합니다. 기존 장치 번호가 재부팅 후 바뀐 환경에는 운영자의 확인이 필요할 수 있습니다. 같은 장치는 가용 공간을 중복 합산하지 않고, 기존 관리 데이터가 이미 사용한 공간은 남은 예산에서 제외합니다. 계정별 강제 할당량은 조회하지 않으며 `quota_checked: false`로 표시합니다. 원본·검색·중간 파일의 예산은 표시·감시용이며 운영체제의 강제 할당량이나 실제 예약이 아닙니다.

원본 압축 파일 크기를 검색 색인 크기로 사용하지 않습니다. 원본 전체 크기·해제된 이벤트 크기·측정한 색인 배율이 없으면 적재 가능 여부와 검색 보관기간을 `null`로 반환합니다. 겹치는 버킷 경로와 부분 목록은 원본 전체 합계로 사용하지 않습니다. 원본의 가장 이른·늦은 관측값은 객체 최종 변경 시각이며 이벤트 발생 시각과 다릅니다.

`oneport-wazuh-storage-guard.timer`는 1분 간격으로 공간·파일 수·예산을 점검합니다. 실패하거나 임계치에 도달하면 관리하는 Wazuh 입력을 일시정지하고 상태를 기록합니다. 과거 데이터는 삭제하지 않으며 외부 원본 이관 도구에는 플랫폼이 정지 필요 상태를 전달해야 합니다. 감시 간격 동안의 증가나 전달 중인 데이터까지 강제 제한하는 기능은 아닙니다.

원본 보관 계정은 로그인할 수 없는 `oneport-archive`, 보관 결과 교환 위치는 `/var/lib/oneport-wazuh-archive-manifests`입니다. 별도 이관 도구가 해당 계정으로 원본을 기록하도록 연결해야 합니다. [보관 결과 형식](archive-manifest.schema.json)은 출처·상대 경로·크기·SHA-256·보관 시각·검증 수준을 정의합니다. `migration.wazuh_infra.archive.validate_manifest`는 형식을 검사하며 실제 AWS 원본을 다운로드하거나 해당 파일의 무결성을 직접 입증하지 않습니다.

원본 파일과 Wazuh의 `archives.json`은 서로 다릅니다. CloudWatch API 이벤트를 파일로 직렬화한 경우에도 S3 원본 파일의 바이트 복사와 구분해야 합니다. 원본 수·이벤트 수·검색 수의 전체 대조와 원본 이관 재개 시험은 별도 이관 작업의 책임입니다.

## 플랫폼 결과·복구·검증

최종 결과는 표준 출력의 단일 JSON입니다. `complete`, `partial`, `blocked`, `failed`와 검사별 `pass`, `fail`, `unknown`, `skipped`를 구분합니다. 요청 작업이 완료되면 종료 코드 0, 부분 준비·차단·실패이면 3입니다. 명령 인자 오류는 표준 인자 처리기의 종료 코드 2입니다. 입력·결과 형식은 [입력 규약](input.schema.json)과 [결과 규약](result.schema.json)에 있습니다.

`raw_storage_ready`는 보관공간 준비, `search_ingestion_ready`는 명시한 시험 이벤트의 검색 검증 여부입니다. `verification.probe: false`이면 해당 이벤트 검사는 생략되어 전체 검증은 부분 준비로 반환합니다. 설치 완료를 전체 데이터 검증으로 처리하지 않으며 `full_migration_verified`는 항상 거짓, `archive_integration_status`는 `external_dependency_pending`입니다. 별도 이관 도구 미연결만으로 이번 서버 설치 완료를 실패 처리하지 않습니다.

Ansible 2.20은 민감 작업의 결과를 일반 수집기에도 가릴 수 있으므로, 등록된 결과에서 전용 모듈의 `report`만 추출하는 제어 측 작업 플러그인을 사용합니다. 민감 작업의 출력 보호는 유지하고 실행 입력·Ansible 원문 출력은 제한된 임시 공간에서 정리합니다. 시간 초과나 강제 종료는 원격 작업 종료를 보장하지 않으므로 `REMOTE_STATE_UNKNOWN`을 반환하고 실제 상태를 재조회해야 합니다.

설치 실패 시 자동 제거·데이터 삭제·패키지 다운그레이드를 수행하지 않습니다. 보호된 설치 기록·인증서·내부 자격증명·데이터를 유지한 채 원인을 해결하고 같은 입력으로 재실행합니다. 설치 잠금 `/run/lock/oneport-wazuh-install`이 남으면 다른 작업이 실행 중인지 확인한 뒤 운영자가 해당 잠금만 정리해야 합니다. 초기 패키지 설치 이후의 부분 상태도 같은 설치 식별자로 재개합니다.

단일 서버이므로 원본과 검색 데이터가 함께 손실될 수 있습니다. 외부 백업·복원·자동 장애 전환·기존 설치 업그레이드는 이번 범위에 포함하지 않습니다. 플랫폼 화면·실제 Ubuntu 설치·실제 AWS 적재의 수행 여부와 인수 절차는 [검증 기록](VALIDATION.md)에 구분합니다.

용어: Ansible은 원격 설치·설정 자동화 도구, API는 프로그램 간 통신 규약, JSON은 구조화된 데이터 형식, SSH는 암호화 원격 접속, TLS는 통신 암호화·서버 신원 확인, SHA-256은 파일 요약값 계산 방식, S3는 AWS 객체 저장소, CloudWatch Logs는 AWS 로그 서비스입니다. PR은 변경 검토 요청이며 manager·indexer·dashboard는 이벤트 처리 서버·검색 저장소·조회 화면입니다.
