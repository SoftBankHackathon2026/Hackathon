# 공개 서버의 GCP·온프렘 연결

2026-10-03: 플랫폼 코드 CD와 Provider 운영 설정 설치는 별개입니다. 공개 서버에서 AWS 환경만 등록되어 있고 GCP·온프렘이 `unregistered`이면 인프라 장애를 의미하지 않습니다. 해당 서버의 `ONEPORT_CONFIG`에 Provider가 없습니다. 설정 등록 후에도 Worker 실행과 실제 배포를 확인해야 합니다.

## 설치 전 확인

- 기존 EC2 운영자 접속 권한으로 작업합니다. GitHub CD role은 정해진 배포 문서만 실행하므로 임의 명령이나 Secret 설치에 사용하지 않습니다.
- 기존 `/etc/oneport/platform.json`·runtime 설정과 Worker 상태를 제한된 경로에 백업합니다. 설정이나 Secret 내용을 명령 출력에 남기지 않습니다.
- 서버의 플랫폼 PostgreSQL 연결을 사용합니다. 개발 PC의 localhost DB 주소를 복사하지 않습니다.
- 같은 앱을 관리하는 로컬 Worker는 진행 중 작업을 완료한 뒤 중지합니다. 서로 다른 플랫폼 DB의 잠금은 서로를 보호하지 않습니다.
- GCP는 실제로 존재하는 Cloud Run 서비스의 project/region/name/UID/URL을 확인합니다. 정리한 테스트 서비스 설정을 재사용하지 않습니다.
- 온프렘은 기존 직접 Argo Provider를 연결합니다. 새 Helm SDK는 아직 Worker에 연결되지 않았으며 동일 Rollout을 두 방식으로 관리하지 않습니다.

## 비밀 파일과 runtime 설정

파일은 서버에서 `root:oneport`, 디렉터리는 `0750`, 파일은 `0640`으로 제한합니다. Git이나 웹 요청, 앱 컨테이너에 넣지 않습니다.

| 경로 | 내용 |
| --- | --- |
| `/etc/oneport/secrets/database-url` | 기존 서버 플랫폼 DB 연결 문자열, 마지막 줄바꿈 허용 |
| `/etc/oneport/secrets/gcp-credentials.json` | 승인된 GCP ADC 자격증명 JSON. 사용자 access token 문자열만으로 대체하지 않음 |
| `/etc/oneport/secrets/oneport-platform.kubeconfig` | 제한된 oneport-deployer 계정. admin kubeconfig 사용하지 않음 |
| `/etc/oneport/gcp-runtime.json` | `platform/examples/gcp/runtime.json`의 실제 서비스 설정 |
| `/etc/oneport/onprem-runtime.json` | `platform/examples/onprem/runtime.json`의 실제 앱·환경 설정 |

각 신규 runtime의 `database_url`을 제거하고 다음을 사용합니다. 기존 AWS runtime의 실제 DB 연결 문자열과 같아야 합니다.

```json
{"database_url_file": "/etc/oneport/secrets/database-url"}
```

GCP에는 실제 서비스 UID·접속 방식(`public` 또는 `authenticated`)·배포 서비스 계정·허용 이미지 digest를 지정합니다. 이 Worker는 등록된 서비스의 릴리즈를 관리하며, 새 서비스 생성은 별도 준비가 필요합니다. 자격증명은 GCP Worker의 `GOOGLE_APPLICATION_CREDENTIALS`로 지정합니다.

온프렘에는 kubeconfig 경로·context·server와 실제 앱 hostname을 지정합니다. EC2는 클러스터 내부 DNS에 직접 접근할 수 없으므로 기존 지원 기능을 사용합니다.

```json
{
  "kubeconfig": "/etc/oneport/secrets/oneport-platform.kubeconfig",
  "context": "oneport-onprem",
  "server": "https://kube-api.yubin.website",
  "preview_probe_mode": "job",
  "preview_probe_image": "<클러스터에서 pull 가능한 Python 검증 이미지의 고정 digest>"
}
```

probe 이미지는 Python 실행 기능을 제공해야 하며 private registry이면 클러스터의 pull 인증도 필요합니다. 위 placeholder는 실제 이미지가 아닙니다. 앱 이미지도 별도로 `allowed_images`에 등록합니다. kubeconfig 토큰 유효기간을 확인합니다.

## Worker 설치와 API 등록

새 systemd unit 두 개를 저장소의 `platform/ops/systemd/`에서 서버로 전달해 설치합니다. 코드를 묶는 CD artifact에는 infra 파일이 포함되지 않습니다.

```bash
sudo install -m 644 oneport-gcp-worker.service oneport-onprem-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/oneport-gcp-worker.service /etc/systemd/system/oneport-onprem-worker.service
```

먼저 서버의 Python 환경으로 신규 runtime 파일이 각 `GcpSettings`·`ArgoSettings` 모델을 통과하는지 확인합니다. 출력에는 성공 여부만 남깁니다. 온프렘은 해당 앱의 실제 요청 JSON으로 다음 읽기 전용 사전 점검을 실행합니다.

```bash
sudo -u oneport /opt/oneport/current/platform/backend/.venv/bin/python \
  -m controlplane.execution.onprem.doctor \
  --config /etc/oneport/onprem-runtime.json --request /path/to/actual-request.json
```

기존 `/etc/oneport/platform.json`의 AWS 항목과 DB 설정을 유지하면서 `providers` 배열에 두 항목을 추가합니다.

```json
[
  {"runtime": "gcp_cloud_run", "config_file": "gcp-runtime.json"},
  {"runtime": "onprem_argo", "config_file": "onprem-runtime.json"}
]
```

API를 재시작한 뒤 health를 확인하고 Worker를 활성화합니다. API가 시작되지 않으면 백업한 bundle을 복원하고 API를 재시작합니다. 설정 파일이 존재한다는 이유만으로 등록 성공으로 처리하지 않습니다.

```bash
sudo systemctl restart oneport-api
curl --fail http://127.0.0.1:8320/healthz
sudo systemctl enable --now oneport-gcp-worker oneport-onprem-worker
sudo systemctl is-active oneport-api oneport-gcp-worker oneport-onprem-worker
```

신규 Worker의 `PartOf=oneport-api.service`는 이후 CD가 API를 재시작할 때 실행 중인 두 Worker도 새 코드로 재시작하게 합니다. 최초 설치·설정 등록·enable은 운영자가 수행해야 합니다. API가 단독으로 꺼지면 두 Worker도 정지합니다.

## 완료 기준

1. 공개 `/api/v1/environments`에 두 실제 환경이 등록됩니다. 이것만으로 연결 검증 완료는 아닙니다.
2. GCP Worker가 권한으로 실제 서비스 조회에 성공하고, 온프렘 doctor의 권한·DB·네트워크 검사를 통과합니다.
3. 공개 UI에서 앱·환경·허용 digest·port·health를 지정해 각각 새 릴리즈를 요청합니다.
4. Worker가 서버 플랫폼 DB의 요청을 소비하고 새 릴리즈로 전환하며 공개 URL과 릴리즈 응답이 일치합니다.
5. API 재시작 후 두 Worker도 재시작되고 다음 요청을 처리하는 것을 확인합니다.

현재 문서와 unit 파일 준비는 서버 연결 완료를 뜻하지 않습니다. 실제 설치와 배포 결과를 확인한 후 완료 상태를 갱신합니다.

## GitHub Secrets에서 자동 공급

GitHub Repository Secrets `ONEPORT_GCP_CREDENTIALS_JSON`, `ONEPORT_ONPREM_KUBECONFIG`가 원본입니다. CD가 서비스 계정 JSON과 제한된 kubeconfig의 구조를 먼저 검증하고, 별도 이름 공간 `oneport-platform/runtime/`의 두 AWS Secrets Manager Secret에 기록합니다. 앱용 `oneport/<app>/*`와 겹치지 않습니다. Terraform은 이름·권한만 만들며 비밀값은 state에 넣지 않습니다.

1. 이 코드가 main에 반영된 뒤 `AWS Infra Deploy`의 plan을 검토하고 apply합니다. Secret 두 개, 제한된 IAM 권한, 고정 명령만 실행하는 `oneport-sync-provider-credentials` SSM 문서가 필요합니다.
2. 최초 확인은 `Platform Deploy` 수동 실행에서 `sync_provider_secrets=true`를 선택합니다.
3. 이후 매번 동기화하려면 Repository Variable `ONEPORT_SYNC_PROVIDER_SECRETS=true`를 설정합니다. 기본값은 꺼져 있어 IAM/SSM 준비 이전에 기존 CD가 실패하지 않습니다.
4. CD가 EC2에서 같은 두 Secret을 읽어 `/etc/oneport/secrets/`에 원자적으로 파일을 교체하고 두 Worker unit을 설치합니다. 이미 실행 중인 Worker만 재시작합니다. 비밀값은 SSM 인수·코드 압축파일·로그에 넣지 않습니다.
5. 위의 실제 runtime 설정 및 bundle 등록을 마친 후에만 Worker를 최초 활성화합니다. Secret 공급 성공은 앱 배포 성공 또는 Provider 등록 완료가 아닙니다.

CD는 이 두 Secret에 Put/Describe/VersionStage 권한만, EC2는 Get 권한만 갖습니다. 같은 값은 같은 버전을 재사용하고, 이전 값으로 되돌리면 해당 버전을 CURRENT로 이동합니다. 토큰 재발급 시 GitHub Secret을 교체하고 동기화를 실행합니다. 이미 만료된 Kubernetes 토큰을 저장한다고 갱신되지는 않습니다.

기존 플랫폼 DB·운영자 토큰·AWS IAM 인증은 이 동기화에서 변경하지 않습니다. 공개 시연의 토큰 없는 접속 모드는 별도 `ONEPORT_AUTH_MODE` 설정입니다.

## 검토된 시연 환경의 자동 등록

`platform/deployment/provider-environments.json`은 비밀값 없는 시연 환경 명세입니다. 기존 AWS `demo`에는 공개 Docker Hub 이미지 digest를 기본값으로 등록하고, GCP의 기존 `oneport-demo` 서비스와 별도 온프렘 `oneport-demo` 환경을 등록합니다. GCP service UID는 API로 실제 확인한 값이며 서비스가 재생성되면 다시 검토해야 합니다.

시크릿 동기화 SSM 문서는 `activate_providers.py`로 이 명세를 적용합니다. 기존 서버 플랫폼 DB와 AWS 인증 설정을 유지하고 신규 환경에는 서버 DB 파일 참조를 사용합니다. 적용 전 Docker Hub 접근/아키텍처, GCP 서비스 리소스 이름·UID, 온프렘 namespace 권한·서버 DB·공개 경로 사전 점검을 수행합니다. 그 뒤 API와 Worker를 재시작하며 시작 실패 시 이전 설정을 복원합니다. 이미 실행 중인 Worker도 재시작해 변경된 runtime 설정을 읽습니다. 새 온프렘 hostname의 사전 HTTP 404는 아직 앱이 없기 때문에 정상이고, 실제 배포 후 릴리즈 응답 확인을 별도로 수행해야 합니다.

GCP `service_url`은 사용자에게 표시하고 운영 상태 검사에 사용하는 공개 주소입니다. 시연 환경은 기존 공유 로드밸런서의 `https://oneport-demo.gcp.yubin.dev`를 사용합니다. Cloud Run API의 `service.uri`는 기본 `run.app` 주소이며 공개 도메인과 같을 필요가 없습니다. 서비스 대상은 project/region/name과 UID로 검증하고, 인증 요청의 ID token audience는 기존처럼 `service.uri`를 사용합니다. 이 설정은 DNS·인증서·로드밸런서를 생성하지 않습니다.

2026-10-03 변경 전 조회에서 커스텀 도메인과 기본 주소의 `/health`가 모두 HTTPS 200이고 같은 `X-Launchpad-Release`를 반환함을 확인했습니다. URL 변경은 runtime fingerprint도 바꾸므로 진행 중인 GCP 배포가 끝난 뒤 적용합니다. 이후 환경 API와 새 배포 결과는 커스텀 도메인을 사용하며, 이미 저장된 과거 배포 결과의 URL은 소급 변경하지 않습니다.

이 자동 등록은 앱 이미지 pull/deploy 자체를 실행하지 않습니다. UI에서 새 릴리즈를 접수해야 앱이 배포됩니다. AWS의 과거 `sha256:000…` 예시값으로 실패한 요청은 자동 재시도하지 않고, 준비된 digest로 새 요청을 제출합니다. 새 서비스/DB/Redis 리소스를 이 설치 단계에서 만들지 않습니다.
