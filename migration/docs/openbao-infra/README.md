# OpenBao 탐색과 Ubuntu 서버 설치

기존 Kubernetes에서 OpenBao 후보를 조사하고 선택한 Ubuntu 노드 **한 대**에 운영체제 서비스로 설치하는 Ansible 기반 모듈입니다. AWS Secrets Manager에서 비밀을 받아올 목적지의 저장·인증 환경을 준비합니다. 실제 AWS 비밀 조회·복사와 GCP 지원, 업무 앱 배포 변경은 수행하지 않습니다.

구현 경로는 `migration/migration/openbao_infra/`입니다. 기존 데이터베이스 이관 모듈과 Keycloak 설치 파일을 수정하지 않고 독립 명령으로 연결합니다. Python은 플랫폼 입력·결과와 Ansible 호출을 담당하고, 실제 서버 작업은 패키지에 포함한 Ansible 작업과 전용 모듈이 수행합니다.

## 설치와 입력

제어 환경에는 Python 3.12 이상이 필요합니다. 새 가상환경에서 다음과 같이 설치합니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install './migration[openbao-infra,dev]'
.venv/bin/python -m migration.openbao_infra --help
```

외부 Ansible 컬렉션은 사용하지 않습니다. 대상 서버에는 Python 3, systemd, 관리자 권한이 필요하며 설치 단계에서 Ubuntu 패키지 관리자를 통해 `ca-certificates`, `tar`, `python3-cryptography`를 준비합니다. 운영체제 검사에는 Ubuntu 22.04·24.04·26.04를 허용하지만 **이 PR에서 실제 Ubuntu 설치 시험을 수행한 것은 아닙니다.** 지원 확정에는 아래 실제 인수 절차가 필요합니다.

OpenBao는 공식 2.7.2 배포물과 프로세서별 SHA-256 검증값을 고정합니다. 설치 시 최신 버전을 조회하거나 기존 서비스를 업그레이드하지 않습니다. [공식 배포](https://github.com/openbao/openbao/releases/tag/v2.7.2), [고정 값](../../migration/openbao_infra/ansible/releases.json)

[예시 설정](../../config.openbao-infra.example.yaml)을 보호된 설정 파일로 복사하고 실제 주소·노드 식별자·접속 파일을 입력합니다. 예시는 문서용 주소와 존재하지 않는 파일 경로를 포함하므로 그대로 실행하는 설정이 아닙니다. 일반 설정에는 비밀 원문을 넣지 않습니다. 토큰·개인키·비밀번호 참조 파일은 실행 사용자 소유와 0600 이하 권한이 필요합니다. 파일 참조에서 심볼릭 링크는 허용하지 않습니다.

Kubernetes의 노드 이름·불변 식별자·등록 주소와 원격 접속 호스트의 machine-id를 함께 대조합니다. `ssh_host`는 해당 노드에 등록된 주소여야 합니다. 원격 호스트 키 검증을 끄지 않습니다. 제어 노드 선택은 `allow_control_plane: true`가 필요하며 Kubernetes 자체 설정이나 서비스는 변경하지 않습니다. 신규 설치에는 cgroup v2가 필요하고, OpenBao의 1 GiB 메모리 상한을 예약한 뒤에도 `min_memory_mb`만큼 가용 메모리가 남아야 합니다.

## 명령과 진행 순서

아래 명령은 설치한 실행 환경에서 사용하며 설정 경로를 각 환경에 맞게 지정합니다.

```sh
python -m migration.openbao_infra discover --config /secure/openbao.yaml
python -m migration.openbao_infra inspect --config /secure/openbao.yaml
python -m migration.openbao_infra preflight --config /secure/openbao.yaml
python -m migration.openbao_infra install --config /secure/openbao.yaml
python -m migration.openbao_infra configure --config /secure/openbao.yaml
python -m migration.openbao_infra handoff --config /secure/openbao.yaml
python -m migration.openbao_infra verify --config /secure/openbao.yaml
python -m migration.openbao_infra finalize --config /secure/openbao.yaml
python -m migration.openbao_infra issue-migration-token --config /secure/openbao.yaml
```

`inspect`는 명시한 HTTPS 주소를 읽기만 합니다. 처음 자체 서명 인증서를 설치하기 전이라면 아직 접속할 서버가 없으므로 설치 선행 조건으로 실행하지 않아도 됩니다. `preflight`는 설치 가능 여부를 읽기만 검사하며, `install`에서도 다시 검사합니다.

`configure`는 저장 엔진·전용 인증·접근 정책을 준비합니다. Keycloak과 업무 앱 입력이 없으면 `partially_ready`를 반환합니다. 운영자 그룹이나 업무 계정을 임의로 생성하지 않습니다. 기존 Keycloak client 정보를 입력받는 방식과, 명시적으로 `manage_client: true`를 선택하여 **기존 영역에 OpenBao 전용 client만 등록**하는 방식을 제공합니다. 다른 설치기가 소유한 client는 이 방식으로 변경하지 않습니다.

`verify`는 전용 임시 경로에 시험 값을 기록하고 읽어본 뒤 관리자 인증으로 해당 시험 값만 정리합니다. `verification.restart: true`이면 이번 설치의 OpenBao 서비스만 재시작하여 자동 해제·값 보존을 검사합니다. 운영 중인 기존 서비스에는 이 재시작 기능을 허용하지 않습니다. 전체 서버를 재부팅하지 않습니다.

초기 관리자 토큰은 대상 서버의 제한된 `bootstrap.json`에 보관하고 플랫폼 결과로 내보내지 않습니다. 사용자 키 인계와 실제 운영자 로그인 토큰이 준비된 뒤 `finalize`를 실행하여 초기 토큰을 회수합니다. `verification.operator_token_ref`에는 실제 Keycloak 로그인으로 발급받은 OpenBao 토큰을 지정합니다. 초기 토큰이나 임의 생성한 관리자 토큰을 운영자 로그인 증거로 받지 않습니다. 이후 관리 작업은 `endpoint.admin_token_ref` 또는 해당 운영자 토큰 참조로 인증합니다.

## 후보 탐색과 기존 환경 재사용

클러스터 전체의 노드·Pod·Service 목록에서 후보를 찾고, 원격 접속 정보가 제공된 노드에서는 서비스와 실행 프로세스를 추가 조사합니다. Pod 환경변수나 Kubernetes의 비밀 저장 객체는 결과로 내보내지 않습니다. 이미지·프로세스 이름과 호환 건강 상태 응답은 제품의 확정 근거가 아니므로 `product_confirmed: false`를 반환합니다. 운영자가 제품을 별도로 확인해야 합니다.

결과의 `reports`에는 후보와 조사한 노드, 누락 사유가 포함됩니다. 원격 접속 정보가 없는 노드·목록 권한 부족·통신 실패가 있으면 완전한 부재로 판정하지 않습니다. 자동 탐색되지 않은 기존 서비스는 플랫폼에서 주소와 포트를 입력받아 `inspect`로 점검합니다. 화면의 선택·입력 로직은 이 모듈이 구현하지 않습니다.

기존 환경에 권한을 준비할 때는 `target.mode: reuse`를 설정합니다. 이때 `endpoint.product_confirmed: true`, 신뢰 인증서와 관리자 토큰 참조가 필수입니다. 선택한 Ubuntu 노드는 제어 상태 파일을 보관할 노드로 사용합니다. 기존 서버의 인증서·고정 키·서비스·저장소를 설치하거나 초기화하지 않습니다. 기존 KV v2 저장 엔진은 사용하거나, 지정한 이름이 없는 경우에만 새로 생성합니다. 기존 감사 장치가 없으면 준비를 차단합니다.

새 인증·정책은 `oi-<installation_id>` 접두어로 구분합니다. 이름이 겹치는 미소유 자원은 덮어쓰지 않습니다. 신규 설치는 설치 식별자·노드 식별자·서버 machine-id와 고정 설정 요약값을 저장하며, 대상이나 설정이 달라지면 자동 적용하지 않습니다.

## 고정 키 다운로드와 폐기

고정 키는 대상 서버의 `/etc/openbao/keys/static.key`에 32바이트 난수로 한 번 생성하고, OpenBao 실행 사용자만 읽는 0400 권한으로 보관합니다. 같은 서버에 키와 암호화 데이터를 함께 두므로 서버 관리자 권한 침해에 대해 별도 키 관리 장비와 동일한 보호를 제공하지는 않습니다. [공식 고정 키 설명](https://openbao.org/docs/configuration/seal/static/)

`handoff.action`에 따라 같은 `handoff` 명령이 다음 작업을 수행합니다.

1. `prepare`: 고정 키·복구용 키가 담긴 다운로드 자료를 원격 호스트 키를 검증한 접속으로 받아 지정한 0700 디렉터리에 0600 파일로 준비합니다. 초기 관리자 토큰은 포함하지 않습니다. 자료 위치·기한·요약값만 결과에 반환합니다.
2. 플랫폼이 인증된 사용자에게 자료를 전달합니다. 플랫폼은 로그인한 사용자와 요청의 `owner_id`가 일치하는지 검증하고, 자료를 해당 작업에만 연결해야 합니다.
3. 사용자가 다운로드와 별도 보관을 확인하면 `action: ack`, `download_confirmed: true`로 호출합니다. 수령 확인 기록을 먼저 확정하고 임시 자료를 삭제한 뒤 서버에 인계 완료를 기록합니다. 서버 기록 실패 시 동일 확인을 재호출할 수 있습니다.
4. 실패·만료 자료는 `action: cleanup`으로 삭제합니다. 플랫폼은 기한에 맞춰 이 명령을 호출해야 합니다. 모듈 자체가 백그라운드 정리 서비스를 설치하지는 않습니다.

브라우저 전송 성공만으로 보관 확인을 대신하지 않습니다. 플랫폼의 다운로드 경로는 매 요청마다 수령 확인 기록·기한·사용자를 확인하고 수령 확인 이후에는 접근을 거부해야 합니다. 캐시·로그에 자료를 남기지 않고, 암호화된 저장 영역을 사용해야 합니다. 실제 웹 다운로드·사용자 인증·암호화 저장소는 플랫폼 책임입니다. 이 모듈은 로컬 파일 접근 권한을 적용하며 저장장치 암호화를 직접 구현하거나 물리적 완전 삭제를 보장하지 않습니다.

폐기하는 것은 **플랫폼·실행 작업의 임시 사본과 다운로드 권한**입니다. 서버의 운영용 고정 키와 사용자가 내려받은 사본은 유지합니다. 확인 전 만료되면 서버 키를 바꾸지 않고 새 자료를 준비할 수 있습니다. 확인 완료 자료의 재발급이 필요하면 플랫폼에서 사용자 재인증과 별도 새 수령 기록 경로를 준비해야 합니다.

## 인증서와 접근 권한

`tls.mode: provided`는 인증서·개인키·신뢰 체인을 입력받아 주소·유효기간·키 일치를 확인합니다. `self_signed`는 접속 주소에 맞는 인증서를 생성하고 재실행 시 유지합니다. 공개 신뢰 자료는 설치 결과의 `trusted_ca_pem`으로 반환합니다. 플랫폼과 접속 프로그램은 이를 신뢰 자료로 등록해야 하며, 브라우저의 신뢰 설정은 별도 안내가 필요합니다. 인증서 개인키는 다운로드 묶음에 포함하지 않습니다. 자체 서명 인증서의 갱신·주소 변경은 자동으로 수행하지 않습니다.

이관 프로그램은 전용 AppRole에서 단기 토큰을 발급받습니다. 선택한 KV v2 경로의 생성·수정·조회만 허용하고 삭제·관리 권한은 주지 않습니다. `issue-migration-token`은 단기 토큰을 보호 파일로 내보내고 결과에 참조와 만료 시각을 반환합니다. 이 파일의 전달·만료 후 삭제는 플랫폼의 책임입니다. 이 기능은 관리자 인증으로 발급하므로 일반 사용자의 무제한 발급 경로로 노출하면 안 됩니다.

운영자는 Keycloak OIDC로 로그인하고 지정 그룹만 전용 관리 정책을 받습니다. 앱은 Kubernetes의 짧은 수명 서비스 계정 JWT를 사용하여 해당 앱 경로만 조회합니다. 앱의 발급자 조회·서명키 갱신 경로가 외부 운영체제 서비스에서 접근 가능해야 합니다. JWT 인증은 Kubernetes의 토큰 검증 요청을 호출하지 않아 삭제·회수된 토큰이 만료 전까지 유효할 수 있습니다. 즉시 회수가 필요한 환경에는 별도 인증 설계가 필요합니다. 실제 앱의 토큰 주입·배포 수정은 별도 이관 작업입니다. [공식 JWT 인증 제약](https://openbao.org/docs/auth/jwt/oidc-providers/kubernetes/)

## 결과와 복구

표준 출력은 구조화된 결과 한 개만 내보냅니다. 일반 결과에는 비밀 원문이나 Ansible 원문 출력이 없습니다. 명령 성공 시 0, 입력 오류·부분 준비·실패 시 3을 반환합니다. `ready`는 실제 필수 검사에 근거하며, 인프라 준비 완료가 비밀 이관 완료를 뜻하지 않습니다. 앱·운영자·재시작·키 인계·초기 관리자 토큰 회수 검사가 없으면 신규 설치는 부분 준비 상태로 남습니다. 기존 환경 재사용에는 재시작·키 인계·초기 토큰 회수 검사를 적용하지 않습니다.

호스트 변경 작업은 `/var/lock/openbao-infra.lock`으로 직렬화합니다. 비정상 종료로 잠금이 남으면 다른 실행이 없다는 사실을 확인한 뒤 운영자가 해당 잠금 디렉터리만 제거합니다. 자동으로 남의 잠금을 뺏지 않습니다. 초기화 요청 이후 응답을 잃으면 저장소를 다시 초기화하지 않고 복구 필요 오류를 반환합니다. 키가 사라져도 새 키를 생성하여 덮어쓰지 않습니다.

운영 원본을 덮어쓰는 복원 명령은 제공하지 않습니다. 백업 운영 시에는 Raft 스냅샷과 고정 키·복구 자료를 별도 보관하고 격리 환경에서 복원을 시험해야 합니다. 실제 복원 시험은 이번 자동 검사에 포함하지 않으며 `recovery_verified: false`로 반환합니다. 이 결과를 복원 검증이 필수인 기존 이관 준비 증명으로 그대로 제출하지 않습니다.

## 검증 범위

검증 명령과 실제 수행 결과는 [검증 기록](VALIDATION.md)에 기록합니다. 로컬 모의시험·실제 OpenBao 시험과 Ubuntu 원격 설치·실제 Keycloak 로그인·플랫폼 브라우저 인계 시험을 구분합니다.

용어: SSH는 암호화 원격 접속, HTTPS·TLS는 서버 신원 확인과 통신 암호화, SHA-256은 파일 무결성을 확인하는 요약값, KV v2는 버전이 있는 키·값 저장, OIDC는 사용자 로그인 연동 표준, JWT는 서명된 신원 토큰, client는 인증 연동 등록 단위, AppRole은 프로그램용 OpenBao 인증 역할, Pod는 Kubernetes의 컨테이너 실행 단위, PR은 변경 검토 요청입니다.
