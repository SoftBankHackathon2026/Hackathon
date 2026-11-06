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

필요 권한은 `pricing:GetProducts` 하나다. 파일에는 받은 시각과 출처가 남고, 화면 아래에 표시된다. GCP 가격표(Cloud Billing Catalog API)는 아직 없다.

## 설정 (`ONEPORT_COST_CONFIG`)

fleet.json 과 따로 둔다. 비우면 기본값(온프렘 환산 기준 AWS 서울, 태그 범위 없음, 이전 여유율 30%)을 쓴다.

```json
{
  "environments": {
    "aws": {"tags": {"Project": "oneaction"}},
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

## 수집과 권한

수집기(`fleet_collector`)가 약 5분마다 환경별 자원 목록을 `cost_inventory` 테이블에 덮어쓴다.

- Kubernetes: `nodes`·`persistentvolumeclaims`·`pods` list. `deploy/aws/rbac.yaml`, `deploy/reader/reader.yaml` 에 들어 있다.
  온프렘 읽기 계정이 노드·PVC 를 못 읽으면 그 환경은 "클러스터 자원: 읽지 못함"으로 남는다.
- AWS 관리형 서비스: `tag:GetResources`, `rds:DescribeDBInstances`, `kafka:ListClustersV2`, `elasticloadbalancing:DescribeLoadBalancers`,
  `ec2:DescribeInstances`, `ec2:DescribeNatGateways`, `cloudwatch:GetMetricStatistics`(S3 크기). 수집기 파드에 AWS 자격 증명이 없으면
  "관리형 서비스: AWS 읽기 실패"로 남고 노드·볼륨 비용만 계산한다. EKS 에서는 Pod Identity 로 읽기 전용 역할을 붙인다.
- GCP 관리형 서비스 수집기는 아직 없다.
