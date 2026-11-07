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
| 전환 명령(시작·승인·중단·재시도) | 화면이 저장(`POST /api/v1/migrations/<job_id>/commands`), 러너 Pod 의 보고기가 pull(`claim`) 후 결과 push | `fleet_cutover_commands` (DB 없으면 메모리) |

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

## 전환 제어 (시작·승인 버튼)

작업 상세 화면(`#migrations/<job_id>`)의 "전환 제어" 패널에서 시작(run)·승인(approve)·재시도(retry)·강제 재개(force_retry)·중단·원복(abort)을 보냅니다. **플랫폼은 클러스터와 DB 에 쓰지 않습니다.** 플랫폼은 명령을 저장만 하고, 온프렘 러너 Pod(gitops `apps/live-db-cutover`, Deployment `live-db-cutover`) 안에서 도는 보고기가 명령을 가져가(pull) `python -m migration.cutover ...` 를 서브프로세스로 실행한 뒤 종료 코드와 로그 끝부분을 올립니다. 플랫폼이 온프렘 클러스터에 닿지 못해도 되고, 온프렘에서 플랫폼으로 나가는 HTTPS 한 방향만 필요합니다.

버튼은 단계에 따라 보입니다: 시작은 READY·ABORTED·NEEDS_RECOVERY·COMPLETED 가 아닐 때, 승인은 READY, 재시도는 ABORTED, 강제 재개는 NEEDS_RECOVERY, 중단·원복은 ABORTED 이면서 실패한 단계가 SWITCHING 앞일 때입니다. 승인은 승인 ID 를 입력하고 "원본 쓰기를 차단하고 대상에 쓰기를 열며 SWITCHING 뒤에는 자동으로 되돌릴 수 없다"는 안내를 확인한 뒤 보냅니다. 한 작업에 대기·실행 중 명령이 있으면 모든 버튼이 잠기고 서버도 새 명령을 409 로 거절합니다(한 번에 한 명령). 응답의 `source` 가 `simulated` 이면 패널에 "시뮬레이션" 배지가 붙고, 실행기가 없어 명령은 대기 상태로 남습니다.

| 메서드·경로 | 인증 | 설명 |
|---|---|---|
| `POST /api/v1/migrations/<job_id>/commands` | 화면(다른 쓰기 경로와 같음: operator 토큰 또는 등록된 Origin, login 모드는 배포 권한) | `{action, approval_id?, note?}` → 202, `{id, status: "pending", ...}`. `approve` 는 `approval_id` 필수(422), 활성 명령이 있으면 409 |
| `GET /api/v1/migrations/<job_id>/commands` | 화면 | 최신순 목록(상태, 종료 코드, 단계, 로그 끝 4000자) |
| `POST /api/v1/ingest/migrations/<job_id>/commands/claim` | 보고용 토큰 | 가장 오래된 `pending` 하나를 원자적으로 `running` 으로 바꿔 돌려줌, 없으면 204 |
| `POST /api/v1/ingest/migrations/<job_id>/commands/<id>/result` | 보고용 토큰 | `{exit_code, stage, log_tail}` → 0 `succeeded`, 3 `waiting_approval`, 그 외 `failed` |

명령은 작업 이벤트 목록에도 "명령 요청: 시작 (by …)" / "명령 결과: …" 로 남습니다. 비밀값은 저장하지 않습니다: 보고기는 서브프로세스 출력에서 `password|token` 뒤의 값을 가린 뒤 마지막 4000자만 올리고, 환경변수는 보내지 않습니다.

### 러너 Pod 에서 보고기 실행

러너 Pod 는 cutover 도구와 `MIGRATION_*` 비밀값 env, `MIGRATION_CONFIG=/etc/live-cutover/config.yaml`, PVC `/var/lib/worker` 를 이미 갖고 있으므로 `sleep` 대신(또는 sidecar 로) 보고기를 `--execute-commands` 로 띄웁니다. 보고기는 표준 라이브러리만 씁니다.

```yaml
# gitops apps/live-db-cutover/runner.yaml (Deployment live-db-cutover) 의 컨테이너
command: ["python", "/opt/reporter/fleet_reporter.py"]
args:
  - --job-id
  - forward_001
  - --state-dir
  - /var/lib/worker
  - --interval
  - "5"
  - --execute-commands
env:
  - name: ONEPORT_PLATFORM_URL
    value: https://<platform-host>          # 온프렘 → 플랫폼 방향 HTTPS
  - name: ONEPORT_INGEST_TOKEN
    valueFrom: { secretKeyRef: { name: oneport-ingest, key: token } }
  - name: MIGRATION_CONFIG
    value: /etc/live-cutover/config.yaml   # 이미 있는 값 그대로, --config 로 넘겨짐
  - name: ONEPORT_CUTOVER_ARGV
    value: python -m migration.cutover     # 기본값. 다른 진입점이면 바꿈
  # MIGRATION_* 비밀값 env 는 기존 그대로 두면 서브프로세스가 상속한다
```

`/opt/reporter/fleet_reporter.py` 는 `deploy/onprem/fleet_reporter.py` 를 ConfigMap 으로 올린 것입니다(`reporter-sidecar.patch.yaml` 참고). 동작 흐름은 "상태 보고 → claim → 있으면 `python -m migration.cutover --config $MIGRATION_CONFIG --state-dir /var/lib/worker run|run --approve <id>|run --retry|run --force-retry|abort` 실행 → 결과 push → 상태 보고" 를 `--interval` 초마다 반복합니다. 서브프로세스가 끝날 때까지 다음 명령을 가져가지 않으므로 두 명령이 동시에 돌지 않습니다. 결과 push 가 실패하면 3번까지 다시 시도하고, 그래도 실패하면 명령이 `running` 으로 남으므로 로그를 보고 수동으로 확인합니다.

## 보안 통제

통제 정의와 판정은 `backend/src/controlplane/domain/security_controls.py`에 있습니다. 점수는 심각도 가중치(critical 10, high 5, medium 3, low 1)로 통과 비율을 계산하고 `unknown`은 분모에서 뺍니다. `parity.regressions`는 출발 환경에서 통과했지만 도착 환경에서 실패한 통제이며, 전환 전 확인 대상입니다.
