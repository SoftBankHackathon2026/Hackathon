# Keycloak 환경 탐색·점검·호스트 설치

플랫폼이 사용자에게 기존 Keycloak 목록과 점검 결과를 보여주고, 신규 설치 시 Kubernetes 소속 노드를 선택하게 하기 위한 실행 도구입니다. UI와 선택은 플랫폼이 담당하며 이 도구는 입력 파일을 받아 **한 개의 JSON 결과**를 반환합니다. AWS 우선·GCP 후속 계정 이관에서 공통으로 사용하는 기반이며 클라우드 자격증명은 사용하지 않습니다.

기존 `migration`은 PostgreSQL 데이터 이관 도구입니다. 설정·상태·서비스 계정을 공유하지 않고 `migration.keycloak_infra`와 그 안의 Ansible 실행 자료로 분리했습니다. `infra/install/onprem-k8s/apply.sh`는 호출하지 않습니다. 계정·그룹·IAM 정책의 수집·변환·적용, Kubernetes 로그인 설정, 앱 변경은 이 도구의 범위 밖입니다. IAM은 신원 및 접근 권한 관리입니다.

**실제 클러스터·서버에 대한 설치 검증은 아직 수행하지 않았습니다.** 로컬 검사와 실제 Ansible을 사용하는 격리 HTTPS 시험은 제공하며, 현장 검증 항목은 아래에 따로 적었습니다. 허용 운영체제 목록은 구현 범위이며 전체 조합에서 설치를 검증했다는 뜻이 아닙니다.

## 설치와 실행

저장소 루트에서 Python 3.12 이상과 OpenSSL을 갖춘 제어 환경에 설치합니다. 기존 `migration-tools` 이미지는 이 선택 의존성을 기본 설치하지 않습니다.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e './migration[keycloak-infra,dev]'
ansible-galaxy collection install -r migration/migration/keycloak_infra/ansible/requirements.yml
cp migration/keycloak-infra.example.yaml migration/keycloak-infra.local.yaml
```

플랫폼은 예제 파일의 경로와 식별자를 실제 값으로 바꾸고 작업별 필요한 부분을 제공합니다.

```bash
python -m migration.keycloak_infra discover --config migration/keycloak-infra.local.yaml --result discovery.json
python -m migration.keycloak_infra inspect --config migration/keycloak-infra.local.yaml --result inspection.json
python -m migration.keycloak_infra preflight --config migration/keycloak-infra.local.yaml --result preflight.json
python -m migration.keycloak_infra install --config migration/keycloak-infra.local.yaml --result installation.json
python -m migration.keycloak_infra verify --config migration/keycloak-infra.local.yaml --result verification.json
```

`install`은 실제 변경 명령입니다. 사용자가 신규 설치와 대상 노드를 선택한 요청에만 플랫폼이 호출해야 합니다. `discover`, `inspect`, `preflight`, `verify`는 원격 환경을 읽기 전용으로 점검합니다. Ansible 실행 로그는 임시 비공개 공간에 보관한 뒤 제거하며 표준 출력에는 최종 JSON만 출력합니다. `--result` 파일은 권한 `0600`으로 원자적으로 교체합니다.

| 작업 | 필수 입력 | 반환/플랫폼 후속 동작 |
| --- | --- | --- |
| `discover` | 클러스터 접속 설정, 조사할 노드의 접속 정보 | 후보·노드·조사 범위 표시. 후보 선택/기존 주소 직접 입력/신규 노드 선택은 플랫폼 책임 |
| `inspect` | `endpoint`의 기존 주소, 필요 시 realm·신뢰 인증서 | 접속·발급자·서명 공개키 점검 결과 표시 |
| `preflight` | 클러스터·선택 노드·설치 설정·인증서 파일 | 설치 가능 여부, 충돌과 예정 변경 표시 |
| `install` | 사전점검 입력과 비밀번호 환경변수 | 사전점검 재실행 → 전용 서비스 설치 → 로컬·외부 검증 |
| `verify` | 비밀번호를 제외한 설치 입력 | 기존 관리 표식과 대상 확인 → 서비스·데이터베이스·외부 주소 검증 |

직접 입력한 기존 주소를 점검하는 `inspect`에는 클러스터·SSH 접속 정보가 필요하지 않습니다. SSH는 서버 원격 접속 방식입니다. 탐색한 후보에 접속 주소가 충분히 확인되지 않으면 `next_actions`로 주소 보완을 요청합니다. Service 이름과 포트만으로 통신 방식이나 외부 주소를 확정하지 않습니다.

## 플랫폼 입력 규약

- `schema_version: 1`, `request_id`, 선택적으로 `timeout_seconds`(10~7200초, 기본 1800초).
- `cluster`: 플랫폼 식별자, **명시한** kubeconfig와 context, 선택적 namespace 목록. 빈 목록은 자격증명으로 볼 수 있는 전체 범위입니다. kubeconfig는 Kubernetes 접속 설정 파일입니다.
- `nodes`: 노드 이름·현재 `metadata.uid`와 SSH 주소·사용자·키 파일·known_hosts 파일. known_hosts는 접속 서버의 신원을 검증하는 호스트 키 목록입니다. 암호 없는 sudo 또는 `become_password_env` 참조로 설치 권한을 제공합니다.
- `endpoint`: `base_url`(통신 방식·주소·포트·필요 시 기본 경로), `realm`(기본 `master`), 선택적 `ca_file`, `management_url`. realm은 Keycloak 사용자와 앱의 관리 영역입니다.
- `install`: 설치 식별자, 선택 노드, 직접 HTTPS 접속 주소·포트, 데이터베이스 포트, 인증서 체인·개인 키·신뢰 인증서 파일, 두 비밀번호 환경변수 이름, 자원 하한.

알 수 없는 입력 필드, 인라인 비밀번호, 주소에 포함된 계정·비밀번호, 임의 Ansible 옵션은 허용하지 않습니다. 설치 주소의 포트와 실제 HTTPS 수신 포트는 같아야 합니다. 이 도구는 역방향 프록시나 DNS·방화벽을 자동 구성하지 않습니다. DNS는 이름을 IP 주소로 변환하는 체계입니다. 필요한 네트워크 접근은 플랫폼/운영 환경이 준비해야 합니다.

설치 시 `KEYCLOAK_DB_PASSWORD`, `KEYCLOAK_BOOTSTRAP_PASSWORD` 등의 **실제 값은 플랫폼이 프로세스 환경으로 주입**합니다. 비밀번호는 24~256자의 `A-Z a-z 0-9 _ @ % + = . , : / ! -` 조합으로 제한해 설정 파일의 재해석을 막습니다. 저장소에 비밀번호를 쓰지 않습니다. 입력 파일·kubeconfig·키 파일은 신뢰할 수 있는 플랫폼이 생성해야 합니다.

## 탐색과 점검의 의미

Kubernetes의 Pod·Deployment·ReplicaSet·StatefulSet·Service·Ingress와 서버의 systemd 서비스·실행 경로·네이티브 프로세스를 조사합니다. 꺼져 있거나 실행 수가 0인 설치도 가능한 범위에서 후보로 반환합니다. 동일 상위 실행 자원에 속한 여러 Pod는 묶고 소속 노드는 보존합니다. Keycloak Operator 자체를 서버 후보로 표시하지 않습니다.

노드 직접 조사는 SSH 대상의 machine-id와 시스템 UUID를 Kubernetes가 보고한 값과 대조합니다. 비교할 값이 없거나 일치하지 않으면 해당 대상의 결과를 사용하지 않습니다. 네이티브 탐색은 서비스 이름·실행 경로·프로세스의 특징을 이용하므로 완전히 이름을 바꾼 사용자 정의 설치의 부재를 보장하지 않습니다. `coverage`, `method`, `limitations`를 함께 표시해야 합니다. 일부 노드/namespace의 조회 실패는 ‘없음’이 아니라 `unknown`으로 반환됩니다.

기존 접속 점검은 OpenID Connect(OIDC: 인증 정보를 전달하는 표준)의 발급자·토큰 주소·서명 공개키를 확인합니다. 메타데이터가 다른 주소로 유도하면 따라가지 않으며 인증서 검증을 생략하지 않습니다. HTTP 주소는 조사할 수 있지만 암호화 미사용 검사에 실패합니다. 관리용 준비 상태 주소가 없으면 그 항목은 `unknown`입니다. 기본 realm이 비활성화되거나 외부에서 숨겨진 환경은 실제 사용 realm을 플랫폼이 입력해야 합니다.

기존 Keycloak의 관리자 자격증명 검증·정확한 제품 버전 조회는 수행하지 않습니다. 응답이 확인되어도 관리 권한이나 실제 앱 로그인이 검증되었다는 뜻이 아닙니다. 결과의 `admin_access: not_checked`, `version: null`을 유지합니다.

## 신규 설치와 재실행 보호

초기 허용 범위는 **systemd 기반 Ubuntu 24.04/26.04, Debian 12/13, x86_64/aarch64**입니다. 운영체제는 입력받지 않고 조사합니다. PostgreSQL 공식 패키지 저장소가 해당 배포판을 제공하는지도 변경 전에 확인합니다. 대상 노드는 `Ready`여야 하며 기본 하한은 사용 가능 메모리 4096MiB, 관련 파일시스템 여유 공간 10240MiB입니다. 시스템 Python 3, sudo, SSH 연결과 외부 배포 저장소 접근이 필요합니다. 폐쇄망 설치는 현재 지원하지 않습니다.

Kubernetes API 기준은 2026-11-06의 최신 안정판 1.37.2이며 Python 접속 라이브러리는 37.0.2 이상 38 미만으로 맞췄습니다. 실제 클러스터 업그레이드는 수행하지 않습니다.

2026-11-06 확인한 Keycloak 26.8.2, Temurin Java 25.0.5+8, PostgreSQL 18을 사용합니다. Keycloak·Java 설치물 URL과 SHA-256 검증값은 `ansible/versions.yml`에 고정했습니다. PostgreSQL은 서명을 확인한 공식 패키지 저장소에서 18의 제공 패치 버전을 설치하며, 기존 패키지를 자동 업그레이드하는 `state: latest`는 사용하지 않습니다. Java는 전용 경로에 풀어 시스템 기본 Java를 바꾸지 않습니다.

| 구성 | 관리 범위 |
| --- | --- |
| Keycloak | `/opt/oneport-keycloak`, `/etc/oneport-keycloak`, `/var/lib/oneport-keycloak`, `oneport-keycloak.service` |
| PostgreSQL | `/var/lib/oneport-keycloak-postgres`, `/etc/postgresql/18/oneport-keycloak`, `oneport-keycloak-postgres.service` |
| 패키지 저장소 | `/etc/apt/sources.list.d/oneport-keycloak-pgdg.list`, 전용 서명 키 파일 |
| 서비스 계정 | `oneport-keycloak`, `oneport-kc-db` |
| 접속 | Keycloak HTTPS 기본 8443, PostgreSQL `127.0.0.1:55432`, 준비 상태 `127.0.0.1:9000`, 내부 캐시 통신 `127.0.0.1:7800/57800` |

PostgreSQL은 전용 구성과 `start.conf=manual`을 먼저 준비해 배포판의 기본 `main` 클러스터 자동 생성을 방지합니다. 기존 PostgreSQL 공식 패키지 저장소가 등록되어 있으면 재사용해 중복 등록을 피합니다. 패키지 설치 시 서비스 자동 시작을 차단하며, 전용 데이터 영역만 초기화합니다. 기존 서비스·전용 경로·계정·포트 충돌은 차단합니다. 임의 데이터 삭제, 기존 Keycloak 채택·업그레이드, 기존 데이터베이스 비밀번호 변경은 하지 않습니다.

설치 식별자·노드 UID·주소·포트·버전의 관리 표식을 기록해 같은 설치를 재개할 수 있습니다. 입력 비밀번호가 기존 보호 파일과 달라지면 자동 교체하지 않고 중단합니다. 초기 관리자는 `bootstrap-admin`이며 최초 준비를 위한 **임시 관리자**입니다. 플랫폼은 안전한 영구 관리 접근을 마련한 뒤 이 임시 계정을 정리해야 합니다. 이 작업은 별도 관리 절차이고, 도구는 `temporary_admin_requires_platform_handoff`로 인계 상태를 반환합니다. 운영용 캐시는 `ispn`을 사용하고 내부 통신을 루프백 주소로 제한합니다. 단일 노드 구성으로 다중 서버 장애 전환은 제공하지 않습니다.

호스트 설치에는 `/run/lock/oneport-keycloak-install` 배타 잠금을 사용합니다. 정상 종료/실패 시 해제하지만 연결 단절·강제 종료 시 남을 수 있습니다. 플랫폼은 **진행 중인 다른 설치가 없고 원격 상태를 재확인한 뒤** 운영자가 잠금을 해제하도록 안내해야 합니다. 자동으로 오래된 잠금을 훔치지 않습니다. 실행 제한 시간 초과는 원격 작업 종료를 보장하지 않으며 `EXECUTION_TIMEOUT_REMOTE_STATE_UNKNOWN`으로 보고합니다.

## 결과와 종료 코드

| 종료 코드 | `status` | 의미 |
| --- | --- | --- |
| 0 | `complete` | 요청한 조사를 마쳤거나 설치 후 필수 검증을 통과 |
| 1 | `failed` | 실행/접속/검증 실패. 설치 명령은 일부 변경이 이미 발생했을 수 있음 |
| 2 | `blocked` | 잘못된 입력 또는 사전점검에서 확인한 설치 차단 조건 |
| 3 | `partial` | 후보를 반환했지만 일부 조사 범위가 미확인 |

일반 결과에는 `request_id`, `operation`, `candidates`, `nodes`, `coverage`, `checks`, `planned_changes`, `changes`, `reason_codes`, `events`, `connection`, `limitations`가 있습니다. `changes`는 성공적으로 실행되어 변경을 보고한 호스트 작업이며 설치 잠금 생성·해제 같은 임시 변경도 포함합니다. 연결 단절 시 마지막 작업의 결과가 불명일 수 있어 최종 전체 상태를 대신하지 않습니다. **종료 코드만으로 기존 환경의 재사용 가능 여부를 판단하지 말고 항목별 검사와 조사 범위를 함께 읽어야 합니다.** 결과 형식은 [result.schema.json](result.schema.json)을 참고합니다.

## 검사와 현장 인수

```bash
cd migration
python -m pytest --tb=short
cd migration/keycloak_infra/ansible
for playbook in playbooks/*.yml; do
  ANSIBLE_CONFIG=ansible.cfg ansible-playbook "$playbook" --syntax-check -i localhost,
done
```

자동 검사는 입력·비밀정보 보호, 여러 Pod 통합, 조사 누락, 잘못된 노드, 불완전 설치 결과, 실제 Ansible의 격리 HTTPS 점검·인증서 검증을 확인합니다. 저장소의 자동 검사도 같은 검증과 배포 패키지 자료 포함 여부를 검사합니다. 실제 PostgreSQL·OPA가 없는 환경에서는 기존 도구의 해당 통합 검사가 생략됩니다. OPA는 외부 정책 판단 도구입니다.

현장에서는 새로 준비한 시험 노드에서 신규 설치 → 동일 설정 재실행 → 실패 후 재개 → 서비스 재시작 후 영속성을 검사해야 합니다. 기존 PostgreSQL·Keycloak이 있는 노드, 인증서 오류, 포트 충돌, 노드 교체, 권한 부족도 시험해야 합니다. 제어 노드를 선택했다면 Kubernetes 기능과 자원 여유를 함께 확인하십시오. 도구는 kubelet 재시작·클러스터 업그레이드·방화벽 변경을 수행하지 않습니다.

실패 시 자동으로 데이터베이스를 되돌리거나 삭제하지 않습니다. 필요한 경우 운영자가 서비스 두 개를 정지해 추가 변경을 멈추고, 기록된 관리 범위와 실제 상태를 대조합니다. 재시도 전에는 원래 비밀번호와 인증서를 확보하고 실패 원인을 해소해야 합니다. 설치 제거와 데이터 폐기는 별도 승인된 절차로 수행합니다.

## 근거

- [Keycloak 공식 배포판](https://github.com/keycloak/keycloak/releases/tag/26.8.2)
- [운영 구성](https://www.keycloak.org/server/configuration-production), [지원 데이터베이스](https://www.keycloak.org/server/db)
- [준비 상태 검사](https://www.keycloak.org/observability/health), [임시 관리자 준비](https://www.keycloak.org/server/bootstrap-admin-recovery)
- [Ansible Kubernetes 조회](https://docs.ansible.com/projects/ansible/latest/collections/kubernetes/core/k8s_info_module.html)
