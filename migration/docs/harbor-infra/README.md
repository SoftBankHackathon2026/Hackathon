# Harbor 온프레미스 설치와 접근권한 준비

**`migration.harbor_infra`는 이미지 이관의 목적지인 Harbor를 탐색·설치·설정하고, 지정한 노드의 공개 인증서 신뢰 설정을 준비하는 독립 Ansible 실행 도구입니다.** 실제 AWS ECR 이미지 복사·업무 앱의 이미지 주소 전환은 별도 도구가 담당하며 GCP Artifact Registry는 후속 대상입니다.

기존 [Keycloak PR #27](https://github.com/SoftBankHackathon2026/Hackathon/pull/27)과 [OpenBao PR #28](https://github.com/SoftBankHackathon2026/Hackathon/pull/28)의 독립 명령·보호 참조·Ansible·구조화된 결과 방식을 참고했습니다. 최신 main `e5bb2e5`에서 별도 작업 폴더를 분기했으며 두 미병합 구현을 의존성으로 가져오지 않았습니다. 플랫폼 화면·작업 전달·비밀 저장 연결은 별도 연계 대상입니다.

- [입력 예제](../../config.harbor-infra.example.yaml)
- [입력 구조](input.schema.json) · 작업별 필수 조건과 파일 검사는 `config.py`가 추가 확인합니다.
- [결과 형식](result.schema.json)
- [인터뷰와 구현계획](PLAN.md)
- [검증 상태와 현장 수용 절차](VALIDATION.md)

## 설치와 호출

제어 환경은 Python 3.12 이상을 사용합니다. 대상 서버는 신규 설치 기준 Ubuntu 24.04·amd64이며 원격 접속, 관리자 권한, 인터넷 다운로드, OpenSSL·systemd·findmnt가 필요합니다. 비밀 참조는 실행 사용자 소유의 일반 파일이고 다른 사용자에게 읽기 권한이 없어야 합니다. JSON·YAML 설정에 비밀 원문을 넣지 않습니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install './migration[harbor-infra,dev]'
.venv/bin/python -m migration.harbor_infra --help
.venv/bin/python -m migration.harbor_infra discover --config /run/oneport/harbor.yaml
.venv/bin/python -m migration.harbor_infra preflight --config /run/oneport/harbor.yaml
```

예제의 문서용 주소·경로·식별자를 실제 플랫폼 입력으로 바꿉니다. `preflight`가 반환한 `plan_digest`를 설정에 넣고 변경 계획을 확인한 다음 `install`, `configure`, `trust`를 호출합니다. 설정·참조 파일 내용·재시작 허용 대상을 바꾸면 다시 사전점검해 요약값을 갱신해야 합니다. 요청 식별자와 검사 선택은 설치의 고정 설정과 분리합니다.

```sh
.venv/bin/python -m migration.harbor_infra install --config /run/oneport/harbor.yaml
.venv/bin/python -m migration.harbor_infra configure --config /run/oneport/harbor.yaml
.venv/bin/python -m migration.harbor_infra trust --config /run/oneport/harbor.yaml
.venv/bin/python -m migration.harbor_infra handoff --config /run/oneport/harbor.yaml
.venv/bin/python -m migration.harbor_infra verify --config /run/oneport/harbor.yaml
```

각 명령은 독립 호출입니다. 탐색 후보가 없다고 자동 설치하지 않습니다. Ansible은 같은 Python 환경의 실행 파일을 우선 사용하며 외부 Ansible 확장 모음은 필요하지 않습니다. 최종 표준 출력은 JSON 하나이며 완료는 종료 코드 0, 차단·실패·부분 준비는 3으로 반환합니다. 인자 자체가 잘못된 경우 인자 처리기의 종료 코드 2가 반환될 수 있습니다.

## 탐색과 재사용

`discover`는 지정 클러스터의 노드와 허용 범위의 파드·서비스·Deployment·StatefulSet·ReplicaSet·Ingress를 조사합니다. 상위 소유 관계 또는 설치 라벨로 후보를 묶으며 파드가 없는 배포 자원도 조사합니다. 접근할 수 있는 노드에서는 Docker의 컨테이너·Compose 메타데이터를 확인합니다. 컨테이너 환경변수·Kubernetes 비밀 객체·원본 설정 전체를 결과에 넣지 않습니다.

이름·이미지 기반 후보는 제품 확인 완료가 아닙니다. 직접 입력한 HTTPS 주소나 후보 주소의 제품 응답·구성요소 상태는 `inspect`에서 확인합니다. 상세 버전이 권한 부족으로 나오지 않으면 제품 미확정으로 반환합니다. 조회 누락은 `coverage`와 고정된 사유 코드로 표시하며 미설치로 해석하지 않습니다. Ingress 주소도 접속 확인 전의 후보입니다. 사용자 정의 이름이나 설치 형태는 탐색에서 누락될 수 있습니다.

`target.mode: reuse`는 기존 Harbor의 제품 설정만 준비하며 새 컨테이너·데이터 경로를 만들거나 기존 환경을 재시작하지 않습니다. 해당 주소의 점검·설정에는 원격 접속이 필요하지 않지만 `trust.targets` 노드의 신뢰 설정에는 노드 접속 정보가 필요합니다. 기존 프로젝트는 명시적 `adopt_existing: true`와 비공개·일반 프로젝트 확인이 필요합니다. 캐시 프로젝트를 쓰기 목적지로 바꾸지 않습니다.

## 신규 설치와 반복 실행

Harbor는 공식 **2.14.6 온라인 설치기**를 검증값으로 고정합니다. Docker 공식 Ubuntu 24.04 패키지 버전과 공개키 파일 검증값도 [releases.json](../../migration/harbor_infra/ansible/releases.json)에 기록했습니다. Harbor 이미지 자체는 해당 제품 버전의 태그를 사용하며 모든 컨테이너 이미지를 개별 불변 요약값으로 잠근 구성은 아닙니다. 재현 가능한 배포 목록을 갱신할 때 별도 검토해야 합니다.

새 설치는 다음 자원을 사용합니다.

| 자원 | 위치 또는 역할 |
| --- | --- |
| 설치 묶음·설정·인증서 | `/opt/oneport-harbor/<installation_id>/` |
| 설치 기록·잠금 | `/var/lib/harbor-infra/<installation_id>/` |
| 영속 데이터 | 명시한 `install.data_path` |
| 기동 서비스 | `oneport-harbor-<installation_id>.service` |
| 공개 접속 | `endpoint.url`의 HTTPS 포트. 예제는 8444 |
| 내부 PostgreSQL·Redis | Harbor 전용 컨테이너. 외부 공개하지 않음 |
| 로그 수신 | Harbor 내부 로그용 호스트의 루프백 포트 1514 |

현재 노드 고유 식별자·주소와 원격 machine-id를 대조하고 자원·포트·저장 경로·마운트·기존 데이터를 검사합니다. 데이터 경로의 지정 여유 용량 외에 Docker 저장 영역에도 최소 10 GB의 여유를 요구합니다. Harbor 로그 보관과 컨테이너 자체 로그에도 용량 한도를 둡니다. 충돌하는 `containerd`·`runc`를 삭제하거나 기존 Docker를 자동 업그레이드하지 않습니다. Docker가 없으면 고정 버전 패키지를 설치하되 패키지 제거가 필요한 경우 중단합니다. 기존 방화벽 정책이 자동 변경의 영향을 검토해야 하는 상태면 차단합니다.

공식 `install.sh`의 기존 환경 정리 명령을 실행하는 대신 공식 `prepare`로 설정을 생성하고, 관리하는 Compose 구성만 서비스로 기동합니다. 서버 시작 시 데이터용 마운트를 확인하고 컨테이너가 이 확인을 우회해 자동 기동하지 않도록 재시작 정책을 구성합니다. 완료한 설치의 재실행은 관리 파일·마운트·고정 설정을 대조합니다. 다른 설정이나 자격증명으로 기존 데이터를 재초기화하지 않습니다.

운영 서버에서의 신규 설치·정전 후 기동은 아직 별도 현장 검증이 필요합니다. 설치 상태 `installed`는 구성요소·인증·노드 다운로드·플랫폼 연계의 전체 준비 완료를 뜻하지 않습니다.

## 인증서와 Keycloak

기존 인증서는 `tls.mode: provided`에서 인증서·개인키·발급자 파일 참조로 제공합니다. 주소·유효기간·키 일치를 검사하고 서버에서 신뢰 체인을 확인합니다. `self_signed`는 전용 발급자와 서버 인증서를 생성하며 반복 실행에서 키를 유지합니다. 공개 신뢰 자료는 결과와 제어 환경의 보호된 상태 영역에 보관하고 개인키는 서버에 남깁니다.

Keycloak은 이미 설치된 관리 영역을 사용합니다. `manage_client: false`이면 사용자가 전용 client·공유 비밀·그룹 전달 설정을 준비합니다. 로그인 반환 주소는 `<Harbor 접속 주소>/c/oidc/callback`이어야 합니다. `manage_client: true`와 `admin_token_ref`를 명시하면 해당 영역의 전용 client만 생성·대조하며 소유 표식·그룹 전달·반환 주소·비밀이 다르면 중단합니다. 관리 영역·사용자·다른 client를 변경하지 않습니다.

Harbor에 일반 로컬 사용자가 있으면 OIDC 전환을 차단합니다. 기존 OIDC 제공자·client가 다르거나 관리 설정이 바뀐 경우 덮어쓰지 않습니다. 관리용 로컬 `admin`은 복구를 위해 별도로 보관합니다. 신규 초기 관리자·DB 비밀번호는 24~256자의 영문·숫자와 `_@%+=.,:/!-` 문자로 구성합니다. Harbor가 생성하는 환경 파일의 재해석을 막기 위해 공백·줄바꿈·달러 기호는 받지 않습니다. 실제 브라우저 로그인은 이 명령이 자동 수행하지 않으며 결과에서 미실행으로 표시합니다.

신규 설치에서 제공한 Keycloak 공개 발급자 자료는 Harbor 구성요소의 신뢰 디렉터리에 배치합니다. 재사용 환경에서 사용자 정의 발급자를 추가하거나 기존 Harbor의 신뢰 저장소를 바꾸는 작업은 별도 관리 절차가 필요합니다. 컨트롤러의 메타데이터 연결 성공만으로 기존 Harbor의 Keycloak 신뢰까지 확인했다고 표시하지 않습니다.

## 노드 인증서 신뢰와 재시작

`trust.targets`는 Harbor 설치 대상과 별도의 목록입니다. 해당 노드의 실제 실행 기반을 확인해 해당 Harbor 주소의 공개 인증서 설정만 준비합니다.

| 실행 기반 | 적용 위치 |
| --- | --- |
| k3s | `/etc/rancher/k3s/registries.yaml`의 해당 주소 `tls.ca_file` |
| 일반 containerd | 활성 `config_path: /etc/containerd/certs.d` 아래 해당 주소의 호스트 설정 |
| Linux Docker Engine | `/etc/docker/certs.d/<호스트와 포트>/ca.crt` |

k3s 설정을 읽는 Python YAML 도구가 없으면 사전점검에서 알리고, `trust`의 Ubuntu 24.04 대상에서는 필요한 운영체제 패키지를 설치합니다. 다른 운영체제의 자동 보조 도구 설치는 허용하지 않습니다. 다른 레지스트리·인증·미러는 유지하고 공개 발급자 자료만 배치합니다. 서버 개인키나 앱용 계정 비밀을 호스트 전역 인증으로 저장하지 않습니다.

k3s는 시작 시 설정을 읽으므로 `trust.restart_approved`에 **대상 노드 이름을 명시한 경우만** 재시작합니다. 재시작이 필요하지만 허용되지 않았으면 부분 준비 상태로 반환합니다. 적용은 노드별 순차 실행하며 실패 시 다음 노드로 진행하지 않습니다. 앱 퇴거·호스트 재부팅·전체 클러스터 재시작은 수행하지 않습니다.

`trust_configured`는 파일과 활성화 상태이며 실제 이미지 다운로드 확인은 별도입니다. 지원 방식은 코드로 제공했으나 실제 k3s·containerd·Docker 노드의 적용 수용 시험은 아직 필요합니다. 전역 containerd 설정 경로 변경이나 기존 레지스트리 설정 편입이 필요하면 중단합니다.

## 계정과 비밀 인계

지정 프로젝트마다 쓰기용과 읽기용 기계 계정을 별도로 만듭니다. 쓰기용은 해당 프로젝트의 읽기·쓰기, 앱용은 읽기만 부여합니다. 삭제·다른 프로젝트·전체 관리 권한은 부여하지 않습니다. 프로젝트 단위 권한이므로 저장소별 격리가 필요하면 프로젝트를 나눕니다.

비밀은 생성 요청 전에 제어 환경의 보호 파일에 저장하고 Harbor 요청에 전달합니다. 생성 응답이 유실돼도 같은 소유 표식과 사전 저장한 비밀로 대조하며 비밀을 자동 갱신하지 않습니다. 계정 식별자·권한·기한·활성 상태가 달라지거나 비밀을 잃으면 중단합니다.

제어 상태 영역은 `<handoff.directory>/<organization_id>/<installation_id>/`입니다. 실행 사용자가 소유하고 반복 호출·재시작 후에도 유지되는 전용 저장소를 플랫폼이 준비해야 합니다. 예제는 `/var/lib/oneport/harbor-state`이며 임시 작업 공간과 구분합니다.

| 파일 | 의미 |
| --- | --- |
| `managed.json` | 대상·관리 프로젝트·계정 식별자와 구성 기록 |
| `credentials.json` | 운영용 계정 비밀. 일반 결과에는 파일 참조만 반환 |
| `trusted-ca.pem` | 신규 설치에서 받은 공개 신뢰 자료 |
| `export.json` | 기한이 있는 다운로드용 임시 사본 |
| `export-record.json` | 인계 대상·요약값·기한·수령 상태 |

`handoff.action: prepare`로 인계 자료를 만들고, 사용자가 보관 확인한 `sha256`을 `receipt_digest`로 전달해 `ack`를 호출합니다. 수령 확인이나 만료 뒤 `cleanup`은 임시 사본만 삭제합니다. 운영용 `credentials.json`은 삭제하지 않으며 플랫폼이 기한·소유권·접근 권한과 외부 비밀 저장을 관리해야 합니다. 계정 자체의 기한과 임시 다운로드 사본의 기한은 다릅니다. 조직·사용자 인증은 플랫폼 책임이며 이 스크립트의 조직 문자열 자체가 권한 증명은 아닙니다.

OpenBao 연계는 해당 보호 참조를 플랫폼에서 전달받는 방식입니다. Harbor 모듈이 OpenBao를 설치·초기화하거나 별도 보관 연결을 이중 구현하지 않습니다. 현재 파일은 암호화 저장소가 아니므로 실행 환경의 접근 통제·암호화 보관은 플랫폼이 제공해야 합니다. 기한 갱신·비밀 교체를 자동 예약하지 않습니다.

## 검증과 상태 해석

`verify`는 구성요소 건강 상태, OIDC 활성 상태, 관리 계정의 실제 권한·기한과 인계 확인을 검사합니다. `verification.image_tests: true`이면 전용 임시 이미지로 쓰기·읽기·내용 요약값 일치, 읽기 계정의 쓰기 거부와 다른 프로젝트의 읽기 거부를 검사합니다. 단일 프로젝트에서는 프로젝트 간 거부 검사를 대상외로 표시합니다. 업무 이미지는 가져오지 않습니다.

`runtime_pull: true`는 `image_tests`와 기존 `test_namespace`를 함께 지정해야 합니다. 플랫폼이 선택한 각 노드에서 임시 Pod·이미지 읽기용 Secret으로 실제 다운로드를 확인합니다. 업무 앱의 배포 설정은 바꾸지 않습니다. 시험용 이미지는 실행 파일이 없는 이미지로, 컨테이너 실행 성공 대신 이미지 식별자 확인을 검사합니다. 시험 객체는 생성한 고유 식별자를 조건으로 삭제하고 시험 이미지도 정리합니다. 정책상 시험 자원이 허용되지 않으면 검사를 생략하고 미실행으로 기록합니다.

필수 검사가 생략되면 `partially_ready`이며 성공을 가장하지 않습니다. `ready`는 해당 요청의 인프라 검증과 인계가 완료된 뜻입니다. 브라우저 로그인·실제 업무 앱 전환·AWS ECR 복사 완료는 별도로 확인해야 합니다. 실패나 시간 초과의 `changed_unknown`은 원격에서 일부 변경이 남았을 수 있다는 뜻이며 `changed: false`만으로 미변경을 단정하면 안 됩니다.

## 중단과 복구

오류 원인을 해소한 뒤 같은 소유 식별자·설정으로 재실행합니다. 잠금은 실행 프로세스의 파일 잠금으로 관리하며 원격 프로세스가 계속 실행 중이면 다음 요청을 차단합니다. 설치·신뢰 설정의 원본 기록과 백업을 보존하며 다른 관리자의 수정은 덮어쓰지 않습니다. 제어 환경이 여러 개인 경우 플랫폼이 같은 Harbor에 대한 변경을 직렬화해야 합니다.

새 Harbor의 실행만 중단하려면 해당 `oneport-harbor-<installation_id>` 서비스만 정지합니다. 데이터·발급자 키·인증서·계정 비밀은 보존하십시오. 기존 Docker 전체를 정지하거나 `compose down -v`, 데이터 경로 삭제를 복구 절차로 사용하지 않습니다. 기존 노드 신뢰 설정 복구는 보호 백업과 현재 변경을 대조한 별도 운영 작업입니다.

용어: PR은 변경 검토 요청, SSH는 암호화 원격 접속, HTTPS·TLS는 서버 신원 확인과 통신 암호화, OIDC는 사용자 로그인 연동 표준입니다. JSON·YAML은 구조화된 자료 형식, Pod는 Kubernetes 컨테이너 실행 단위, Secret은 자격증명을 전달하는 Kubernetes 객체입니다. amd64는 64비트 x86 프로세서 형식, machine-id는 Linux 호스트 식별값, client는 로그인 연동 등록 단위, realm은 Keycloak 인증 관리 영역입니다.
