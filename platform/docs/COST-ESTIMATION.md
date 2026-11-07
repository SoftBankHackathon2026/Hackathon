# 비용 추산

화면: 왼쪽 메뉴 **비용** (`#costs`). API: `GET /api/v1/costs`.

## 무엇을 보여 주나

| 구분 | 계산 |
|---|---|
| 클라우드 환경의 지금 월 비용 | 클러스터 노드(인스턴스 유형) + 컨트롤 플레인 + PVC 용량 + 태그 범위 안 관리형 서비스(RDS·MSK·ALB·NAT Gateway·S3·클러스터 밖 EC2) × 공식 온디맨드 단가 |
| 온프렘 지금 월 비용 | 운영자가 입력한 장비비(감가상각)·전기료·기타. 입력이 없으면 "입력 필요" |
| 온프렘 클라우드 동급 환산 | 노드마다 같은 vCPU·메모리 이상인 가장 싼 인스턴스 + 볼륨 |
| 이전 추산 | 원본 네임스페이스 워크로드의 requests 합계(여유율 포함)를 대상 노드의 남는 용량에 먼저 넣고, 모자라면 대상이 쓰는 노드 유형으로 늘린다. Postgres·Kafka·오브젝트 스토리지는 관리형으로 바꾼다(설정으로 그대로 띄우기 선택 가능) |
| 월 증감 | 대상 증가분 − 원본 감소분. 원본 감소분은 옮기는 워크로드의 요청량 비중만큼의 노드 비용 + 그 네임스페이스 볼륨이다. 온프렘 원본은 장비가 남으므로 0 |
| 이중 운영 비용 | 대상 증가분 × 전환 기간(기본 14일) / 30 |

| 데이터 전송(지금) | AWS: 지난 7일 CloudWatch 측정값을 월로 환산. NAT 처리량·NAT 경유 인터넷 송신·VM 송신(Tailscale 라우터, 상한)·ALB 처리량(1 GB = 1 LCU-시간으로 근사). GCP 는 아직 측정하지 않는다 |
| 공인 IPv4·디스크 | 태그 범위 안 EIP·ALB 공인 IP·공인 IP 가 붙은 VM 수 × 시간당 단가, 노드·VM 루트 볼륨(PVC 볼륨은 Kubernetes 쪽에서 셈) |
| 이전 1회성 비용 | 데이터 첫 복사(옮기는 PVC 용량, 상한) + 전환 기간 CDC 변경분(하루 `cdc_gib_per_day` GB 가정) × 방향별 단가 + 이중 운영. 클라우드로 들어오는 전송은 무료, 클라우드에서 나가는 전송은 인터넷 송신 단가 |
| 무엇이 바뀌나 | 원본 자원 → 대상 자원 행(관리형으로 바뀜·옮겨감·새로 생김·줄어듦)과 행별 월 비용 변화. 행 합계 = 월 증감 |

모든 금액은 USD, 월 730시간 기준이다. 약정 할인·크레딧·세금·데이터 전송·요청 단위 요금(LCU, NAT 처리 GB)은 넣지 않는다.
대상이 온프렘이면 단가 대신 남는 용량 안에 들어가는지만 본다.

## 청구서가 아니라 자원 × 가격표인 이유

- 팀 계정은 Cost Explorer(`ce:*`)가 명시적으로 막혀 있어 청구액을 읽을 수 없다. 청구 데이터는 반영까지 하루쯤 걸리기도 한다.
- 아직 없는 이전 대상 환경은 청구서가 없다. 같은 방식(자원 × 단가)이어야 원본·대상을 나란히 비교할 수 있다.
- 태그는 금액이 아니라 **범위**로 쓴다. 이 서비스의 관리형 자원을 `Project=oneaction` 같은 태그로 찾는다.

청구 권한이 생기면 같은 화면에 청구액을 대조용으로 붙일 수 있다(AWS Cost Explorer 태그별 조회, GCP BigQuery 청구 내보내기).

## 가격표

`backend/src/controlplane/cost/prices/<provider>-<region>.json`. 손으로 적지 않고 공식 가격 API 에서 받는다.

```sh
AWS_PROFILE=<읽기 프로필> uv run --project platform/backend --extra aws python platform/scripts/fetch_cloud_prices.py aws ap-northeast-2
```

```sh
gcloud auth application-default login
uv run --project platform/backend python platform/scripts/fetch_cloud_prices.py gcp asia-northeast3
```

AWS 는 `pricing:GetProducts`, GCP 는 Cloud Billing Catalog API 읽기 권한이면 된다. 파일에는 받은 시각과 출처가 남고, 화면 아래에 표시된다.
GCP 는 계열별 vCPU·메모리 단가(`compute_rates`, `managed_rates`)도 저장해 `e2-custom-2-4096`, `db-custom-1-3840` 같은 이름을 그대로 계산한다.
GCP Managed Kafka 는 DCU 단위로 과금하는데 vCPU·메모리 → DCU 환산을 공식 자료로 확인하지 못해 "단가 없음"으로 둔다.

## 설정 (`ONEPORT_COST_CONFIG`)

fleet.json 과 따로 둔다. 비우면 기본값(온프렘 환산 기준 AWS 서울, 태그 범위 없음, 이전 여유율 30%)을 쓴다.

```json
{
  "environments": {
    "aws": {"tags": {"Project": "oneaction"}},
    "gcp": {"project": "<GCP 프로젝트 ID>"},
    "onprem": {
      "onprem": {
        "hardware_usd": null,
        "amortization_months": 36,
        "power_watts": null,
        "electricity_usd_per_kwh": null,
        "other_monthly_usd": 0
      },
      "equivalent": {"provider": "aws", "region": "ap-northeast-2"}
    }
  },
  "migrations": {
    "forward_001": {
      "namespaces": ["hackathon"],
      "data_strategy": "managed",
      "headroom": 0.3,
      "dual_run_days": 14
    }
  }
}
```

`namespaces` 를 비우면 원본 환경의 `app_namespace` 하나를 옮긴다고 본다.

## 온프렘 측정기 붙이기

온프렘에는 청구서도 가격 API 도 없다. 장비비·전기 단가는 설정(`onprem`)에 적고, 측정할 수 있는 값은
측정기가 읽어 그 필드만 덮어쓴다. 화면에는 근거가 "측정값"으로 표시된다.

- 인터페이스: `controlplane/cost/onprem.py` 의 `OnPremMeter.read(env) -> OnPremReading | None`.
  `OnPremReading` 은 `power_watts`·`hardware_usd`·`amortization_months`·`electricity_usd_per_kwh`·`other_monthly_usd` 중
  읽은 것만 채운다(나머지는 설정값).
- 등록: `@register_meter("이름")` 을 붙인 클래스(생성자는 설정의 `options` dict 하나)를
  `controlplane/cost/meters/` 아래 모듈에 둔다. 이 패키지의 모듈은 자동으로 불러온다.
- 선택: 비용 설정 `{"environments": {"onprem": {"meter": {"name": "이름", "options": {...}}}}}`.
- 실패: 등록되지 않은 이름, 생성 오류, `read()` 예외는 수집기를 멈추지 않고 "온프렘 측정기: 읽기 실패"로 남는다.
  `None` 을 돌려주면 설정값만 쓴다.
- 기본 제공: `cost-exporter`. 온프렘 노드마다 DaemonSet 이 전력을 구해 ConfigMap `oneport-cost/cost-reading-<노드>` 에 남기고,
  측정기가 온프렘 읽기 계정으로 그 값을 모아 `power_watts` 로 쓴다. 노드가 VM 이면 CPU 사용률 기반 추정, 물리 서버에서 RAPL 을 읽을 수 있으면 측정.
  배포 설정 `deploy/aws/cost.json` 이 이 측정기를 고른다. 전기 단가는 같은 파일 `onprem.electricity_usd_per_kwh` 0.132 다
  (한전 2026년 7월 평균 판매단가 kWh 당 179.7원 ÷ 2026-11-06 원·달러 1,356.6원. 일반용 요금표 단가로 바꾸려면 이 값만 고친다). 자세한 것은 `infra/install/onprem-k8s/admin/cost-exporter/README.md`.
- 다른 예: 스마트 플러그 API 를 읽는 측정기.
- 테스트 예: `tests/test_costs.py` 의 `test_registered_meter_is_created_and_read_by_collector`.

## 수집과 권한

수집기(`fleet_collector`)가 5분마다(tick 횟수가 아니라 실제 경과 시간 기준) 환경별 자원 목록을 `cost_inventory` 테이블에 덮어쓴다.

- Kubernetes: `nodes`·`persistentvolumeclaims`·`pods` list. `deploy/aws/rbac.yaml`, `deploy/reader/reader.yaml` 에 들어 있다.
  온프렘 읽기 계정이 노드·PVC 를 못 읽으면 그 환경은 "클러스터 자원: 읽지 못함"으로 남는다.
- AWS 관리형 서비스: `tag:GetResources`, `rds:DescribeDBInstances`, `kafka:ListClustersV2`, `elasticloadbalancing:DescribeLoadBalancers`,
  `ec2:DescribeInstances`, `ec2:DescribeNatGateways`, `cloudwatch:GetMetricStatistics`(S3 크기). 수집기 파드에 AWS 자격 증명이 없으면
  "관리형 서비스: AWS 읽기 실패"로 남고 노드·볼륨 비용만 계산한다. EKS 에서는 Pod Identity 로 읽기 전용 역할을 붙인다.
- GCP 관리형 서비스: 비용 설정의 `project` 안에서 Cloud SQL(PostgreSQL)·Managed Kafka·전달 규칙(LB)·Cloud NAT·
  GKE 노드가 아닌 VM·Cloud Storage(크기는 Cloud Monitoring `storage/total_bytes`)를 읽는다. `tags` 는 라벨 조건이다.
  필요한 역할: `roles/cloudsql.viewer`, `roles/managedkafka.viewer`, `roles/compute.viewer`, `roles/storage.bucketViewer`,
  `roles/monitoring.viewer`. 클러스터 안에서는 Workload Identity 로 붙인다.
