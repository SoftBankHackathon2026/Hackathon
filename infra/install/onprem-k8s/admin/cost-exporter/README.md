# 온프렘 비용 exporter

온프렘 노드마다 소비 전력을 구해서 플랫폼 비용 화면의 "전기" 줄에 넣어요. 노드마다 DaemonSet 파드가 하나씩 떠요.

## 무엇을 내나

| 어디로 | 내용 |
|---|---|
| ConfigMap `oneport-cost/cost-reading-<노드>` | `reading.json` 한 개. 평균 전력, 지금 전력, 누적 에너지, CPU 사용률, vCPU, 메모리, 계산 방식, 계수. 60초마다 갱신 |
| `/metrics` (포트 9877) | `oneport_cost_node_power_watts`, `oneport_cost_node_average_power_watts`, `oneport_cost_node_energy_joules_total`, `oneport_cost_node_cpu_utilization_ratio`, `oneport_cost_node_vcpus`, `oneport_cost_node_memory_bytes`, `oneport_cost_node_info`. ServiceMonitor 로 온프렘 Prometheus 가 30초마다 긁어요 |

플랫폼은 이미 쓰는 온프렘 읽기 계정(`hackathon-user`, view)으로 ConfigMap 을 읽어요. 새 포트를 밖으로 열지 않고, 플랫폼 쪽 권한도 늘리지 않아요.

## 전력을 구하는 법

지금 온프렘 노드 두 대는 KVM 가상머신이라 전력 센서(RAPL, hwmon)가 없어요. 그래서 기본은 추정이에요.

- **호스트 모델(`host`, 지금 매니페스트)**: 두 VM 이 물리 서버 한 대(Intel N100, 4스레드, 16GB)를 나눠 쓰므로, 서버 전체 전력을 VM 몫으로 나눠요.
  - 노드 전력 = `유휴 W × (노드 vCPU ÷ HOST_VCPUS) + (최대 W − 유휴 W) × CPU 사용률 × (노드 vCPU ÷ HOST_THREADS)`, 여기에 PUE 를 곱해요.
  - 두 노드 값을 더하면 `유휴 W + (최대 W − 유휴 W) × 호스트 사용률` 이 돼요. 유휴 값에 디스크·팬·전원 손실이 들어가요.
  - 기본값은 N100 미니 PC 의 콘센트 전력 리뷰 측정치 대략값이에요(유휴 7 W, 전부하 25 W). 전력계로 재면 `HOST_IDLE_WATTS`·`HOST_MAX_WATTS` 만 바꾸면 돼요.
  - `HOST_*` 넷 중 하나라도 비우면 아래 클라우드 평균 계수(`model`)로 돌아가요.

- **추정(`model`)**: `vCPU × (최소 W + (최대 W − 최소 W) × CPU 사용률) + 메모리 GiB × W/GiB`, 여기에 PUE 를 곱해요.
  - 기본 계수는 Cloud Carbon Footprint 가 클라우드 평균으로 쓰는 값이에요. vCPU 당 0.74 W(유휴)~3.5 W(최대), 메모리 GiB 당 0.392 W, PUE 1.0.
  - CPU 사용률은 `/proc/stat` 에서 15초 간격으로 구해요. steal(하이퍼바이저가 가져간 시간)은 바쁜 시간에 넣지 않아요.
  - 가상머신이 쓰는 몫만 세요. 물리 서버 전체 전력(디스크, 팬, 다른 VM)은 들어가지 않아요.
- **측정(`rapl`)**: 물리 서버에서 `/sys/class/powercap/intel-rapl:*` 를 읽을 수 있으면 CPU 패키지 전력을 실제로 재요. 메모리는 위 계수로 더해요. 커널이 `energy_uj` 를 root 만 읽게 하므로, 지금 매니페스트(비 root)에서는 읽기에 실패하고 추정으로 돌아가요(`rapl_error` 에 남아요).

평균 전력은 누적 에너지 ÷ 지난 시간이에요. 파드가 다시 뜨면 자기 ConfigMap 의 누적 에너지와 시간을 읽어 이어서 쌓아요. 계수가 바뀌었으면 처음부터 다시 쌓아요. 파드가 내려가 있던 시간은 세지 않아요.

## 설정

DaemonSet 환경 변수로 바꿔요.

| 변수 | 기본 | 뜻 |
|---|---|---|
| `MIN_WATTS_PER_VCPU` | 0.74 | CPU 유휴일 때 vCPU 당 전력 |
| `MAX_WATTS_PER_VCPU` | 3.5 | CPU 100% 일 때 vCPU 당 전력 |
| `WATTS_PER_MEMORY_GIB` | 0.392 | 메모리 GiB 당 전력 |
| `PUE` | 1.0 | 냉방 등 시설 부하 배수 |
| `HOST_IDLE_WATTS` | 7 (매니페스트) | 물리 서버 유휴 전력 |
| `HOST_MAX_WATTS` | 25 (매니페스트) | 물리 서버 전부하 전력 |
| `HOST_THREADS` | 4 (매니페스트) | 물리 서버 CPU 스레드 수 |
| `HOST_VCPUS` | 4 (매니페스트) | 이 서버의 VM 들에 준 vCPU 합계 |
| `SAMPLE_SECONDS` | 15 | 샘플 간격 |
| `PUBLISH_SECONDS` | 60 | ConfigMap 갱신 간격 |

플랫폼 쪽은 비용 설정에 측정기를 고르면 돼요(`platform/deploy/aws/cost.json`).

```json
{"environments": {"onprem": {"meter": {"name": "cost-exporter", "options": {"namespace": "oneport-cost"}}}}}
```

옵션 `max_age_seconds`(기본 600)보다 오래된 ConfigMap 은 빼고, `use: "current"` 이면 평균 대신 지금 전력을 써요. 전기 단가(`electricity_usd_per_kwh`)는 비용 설정 `onprem` 에 넣어야 금액이 나와요.

## 적용과 확인

`apply.sh` 4a 단계가 적용해요. 따로 할 때는 이렇게 해요.

```sh
kubectl apply -k infra/install/onprem-k8s/admin/cost-exporter
kubectl -n oneport-cost rollout status ds/cost-exporter
kubectl -n oneport-cost get configmap -l oneport.io/cost-reading=node -o jsonpath='{range .items[*]}{.data.reading\.json}{"\n"}{end}'
```

이미지를 따로 만들지 않아요. `python:3.12-alpine`(Harbor Docker Hub 프록시 경유)에 `exporter.py` 를 ConfigMap 으로 붙여 돌려요. 파드 메모리는 17 MiB 안팎이에요.

## 테스트

```sh
uv run --no-project --with pytest python -m pytest -q infra/install/onprem-k8s/admin/cost-exporter
```
