# 마이그레이션·보안 대시보드

플랫폼은 온프렘 k3s(레거시)에서 AWS EKS(신규)로 옮기는 앱과 DB가 끊김 없이 이전되고 되돌릴 수 있다는 것을 숫자로 보여 줍니다. 실제 전환은 `SoftBankHackathon2026/Hackathon`의 `migration` 도구(Debezium CDC, cutover 상태 기계, OPA 게이트, verify)가 수행하고, 플랫폼은 그 결과를 읽어 시각화합니다. 플랫폼은 클러스터와 DB에 쓰지 않습니다.

## 화면

| 경로 | 내용 |
|---|---|
| `#migrations` | 환경 지도, 이전 증거 KPI(실패한 쓰기, 최대 쓰기 중단, 최대 지연, 데이터 불일치), 작업 표, 환경별 Argo CD 앱 |
| `#migrations/<job_id>` | 10단계 진행(승인 지점 READY, 되돌릴 수 없는 지점 SWITCHING), 원본·대상 쓰기 흐름 차트, 대기열·지연 차트, OPA 게이트, verify(B−T, T−B, 필드 불일치, S−T), 커넥터·워커, 이벤트 |
| `#security` | 환경별 점수, 출발 환경에서는 통과했지만 도착 환경에서 실패한 통제, 통제 매트릭스(CIS Kubernetes, AWS FSBP, NSA/CISA, Oneport 정책) |

응답의 `source`가 `simulated`이면 화면에 "시뮬레이션 데이터" 배지가 붙습니다.

## 데이터 출처

| 데이터 | 수집 방식 | 저장 |
|---|---|---|
| 쓰기 워커 상태 (`db-insert-worker` `/status`) | 수집기가 5초마다 pull | `fleet_worker_samples`, `fleet_worker_status` |
| Kafka Connect 커넥터 상태 | 수집기가 pull (`/connectors/<job>-source|sink/status`) | `fleet_connectors` |
| 클러스터 사실(네임스페이스 라벨, NetworkPolicy, 실행 중 Pod securityContext, Role, argocd-cm, ExternalSecret, ResourceQuota, Argo CD Application) | 수집기가 1분마다 읽기 전용 kube API (`hackathon-user`: `view` + argoproj 읽기) | `fleet_environment_facts` |
| cutover 단계·체크포인트, OPA 판정, verify 보고서 | 워커 Pod 안의 보고기가 push (`POST /api/v1/ingest/migrations/<job_id>`) | `fleet_cutover`, `fleet_cutover_stages`, `fleet_gate_decisions`, `fleet_verify_reports` |
| 클라우드 보안 서비스 여부(GuardDuty, Security Hub, 감사 로그, WAF 등) | 설정 파일의 `declared` (Terraform 기준) | 설정 |

cutover 상태는 워커 PVC의 SQLite(`cutover.sqlite3`, `policy.sqlite3`)에 있고 플랫폼의 읽기 전용 권한으로는 읽을 수 없어 push 방식을 씁니다.

## 실행

시뮬레이션(기본): 설정 없이 API를 실행하면 `integrations/defaults.py`의 두 환경과 두 작업(`forward_001` 완료, `return_001` 승인 대기)을 시간 함수로 계산해 보여 줍니다. 단계 의미와 수치는 cutover 도구 동작(초당 1건 쓰기, FENCING부터 SWITCHING까지 쓰기 일시정지 후 대기열 재처리)을 따르고, 레포에서 근거를 찾을 수 없는 클러스터 사실은 `unknown`으로 둡니다.

실측:

```sh
export ONEPORT_DATABASE_URL_FILE=/safe/path/database-url  # 플랫폼 DB (배포 Provider 설정 불필요)
export ONEPORT_FLEET_CONFIG=/safe/path/fleet.live.json  # examples/fleet/fleet.live.json 참고
export ONEPORT_INGEST_TOKEN_FILE=/safe/path/ingest-token # 32자 이상
uv run --project platform/backend uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
uv run --project platform/backend python -m controlplane.execution.fleet_collector
```

수집기는 워커·Connect 주소에 닿는 곳에서 실행합니다. 기본 배치는 AWS EKS `oneport` 네임스페이스이고(`deploy/aws`), EKS는 `in_cluster: true`로 Pod ServiceAccount를 써서 access entry가 필요 없습니다. 온프렘은 `Hackathon/infra/install/onprem-k8s/generate-kubeconfig.sh`의 읽기 전용 사용자, GKE는 `deploy/reader`의 읽기 전용 계정 kubeconfig를 씁니다. 자세한 순서는 [platform/README.md](../README.md) 2장을 따릅니다.

보고기는 워커 Pod에 sidecar로 붙여 같은 PVC를 읽습니다(표준 라이브러리만 사용).

```sh
python deploy/onprem/fleet_reporter.py --job-id forward_001 --state-dir /var/lib/worker --interval 5
# 환경변수: ONEPORT_PLATFORM_URL, ONEPORT_INGEST_TOKEN
```

## 보안 통제

통제 정의와 판정은 `backend/src/controlplane/domain/security_controls.py`에 있습니다. 점수는 심각도 가중치(critical 10, high 5, medium 3, low 1)로 통과 비율을 계산하고 `unknown`은 분모에서 뺍니다. `parity.regressions`는 출발 환경에서 통과했지만 도착 환경에서 실패한 통제이며, 전환 전 확인 대상입니다.
