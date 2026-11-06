"""클라우드 공식 가격 API 에서 비용 추산용 가격표(JSON)를 만든다.

가격을 손으로 적지 않고 매번 이 스크립트로 다시 받는다. 결과 파일에는 받은 시각과 출처가 남는다.

  uv run --project platform/backend --extra aws python platform/scripts/fetch_cloud_prices.py aws ap-northeast-2

  uv run --project platform/backend python platform/scripts/fetch_cloud_prices.py gcp asia-northeast3

AWS: Price List Query API(pricing:GetProducts, us-east-1 엔드포인트). 온디맨드·Linux·공유 테넌시 기준.
GCP: Cloud Billing Catalog API(services.skus.list). 애플리케이션 기본 자격 증명으로 읽는다
     (gcloud auth application-default login). 온디맨드 SKU 기준.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "backend/src/controlplane/cost/prices"

EC2_TYPES = [
    *(f"t3.{s}" for s in ("micro", "small", "medium", "large", "xlarge", "2xlarge")),
    *(f"t4g.{s}" for s in ("micro", "small", "medium", "large", "xlarge", "2xlarge")),
    *(f"m6i.{s}" for s in ("large", "xlarge", "2xlarge")),
    *(f"m7g.{s}" for s in ("large", "xlarge", "2xlarge")),
    *(f"r6i.{s}" for s in ("large", "xlarge")),
]
RDS_CLASSES = [
    *(f"db.t4g.{s}" for s in ("micro", "small", "medium", "large")),
    *(f"db.t3.{s}" for s in ("micro", "small", "medium", "large")),
    *(f"db.m7g.{s}" for s in ("large", "xlarge")),
]
MSK_BROKERS = ["kafka.t3.small", "kafka.m5.large", "kafka.m7g.large"]


def _usd(product: dict) -> tuple[float, str]:
    """온디맨드 단가 하나. 구간 가격이면 첫 구간(가장 작은 사용량)을 쓴다."""
    terms = product["terms"]["OnDemand"]
    dims = [d for term in terms.values() for d in term["priceDimensions"].values()]
    dims.sort(key=lambda d: float(d.get("beginRange") or 0))
    dim = dims[0]
    return float(dim["pricePerUnit"]["USD"]), dim["unit"]


def _query(pricing, service: str, **filters: str) -> list[dict]:
    flt = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
    items, token = [], None
    while True:
        kwargs = {"ServiceCode": service, "Filters": flt, "MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        page = pricing.get_products(**kwargs)
        items += [json.loads(p) for p in page["PriceList"]]
        token = page.get("NextToken")
        if not token:
            return items


def _one(
    pricing, service: str, usage: str = "", **filters: str
) -> tuple[dict, float, str]:
    """조건에 맞는 상품 중 가장 싼 온디맨드 단가.

    usage 는 usagetype 에서 리전 접두어(APN2- 등)를 뗀 값의 시작 부분이다. 접두어가 다른 변형
    (Outposts-·TS- 등)을 섞지 않도록 부분 일치가 아니라 앞부분 일치로 거른다.
    """
    found = []
    for product in _query(pricing, service, **filters):
        usagetype = product["product"]["attributes"].get("usagetype", "")
        if not usagetype.split("-", 1)[-1].startswith(usage):
            continue
        try:
            price, unit = _usd(product)
        except (KeyError, IndexError):
            continue
        if price > 0:
            found.append((product, price, unit))
    if not found:
        raise LookupError(f"{service} {filters}")
    found.sort(key=lambda x: x[1])
    return found[0]


def _gib(text: str) -> float:
    value, unit = text.split()
    return float(value.replace(",", "")) * (1024 if unit.lower().startswith("t") else 1)


def fetch_aws(region: str) -> dict:
    import boto3

    pricing = boto3.client("pricing", region_name="us-east-1")
    compute = {}
    for itype in EC2_TYPES:
        product, price, _ = _one(
            pricing,
            "AmazonEC2",
            usage="BoxUsage:",
            instanceType=itype,
            regionCode=region,
            operatingSystem="Linux",
            tenancy="Shared",
            preInstalledSw="NA",
            capacitystatus="Used",
            licenseModel="No License required",
        )
        attrs = product["product"]["attributes"]
        compute[itype] = {
            "hourly": price,
            "vcpu": int(attrs["vcpu"]),
            "memory_gib": _gib(attrs["memory"]),
        }
    managed = {"postgres": {}, "kafka_broker": {}}
    for cls in RDS_CLASSES:
        product, price, _ = _one(
            pricing,
            "AmazonRDS",
            usage="InstanceUsage:",
            instanceType=cls,
            regionCode=region,
            databaseEngine="PostgreSQL",
            deploymentOption="Single-AZ",
        )
        attrs = product["product"]["attributes"]
        managed["postgres"][cls] = {
            "hourly": price,
            "vcpu": int(attrs["vcpu"]),
            "memory_gib": _gib(attrs["memory"]),
        }
    for broker in MSK_BROKERS:
        family = broker.removeprefix("kafka.")
        _, price, _ = _one(
            pricing,
            "AmazonMSK",
            usage=f"Kafka.{family}",
            regionCode=region,
            computeFamily=family,
        )
        managed["kafka_broker"][broker] = {"hourly": price}

    def unit(service: str, usage: str, **filters: str) -> float:
        return _one(pricing, service, usage=usage, regionCode=region, **filters)[1]

    storage = {
        "block": unit("AmazonEC2", "EBS:VolumeUsage.gp3", productFamily="Storage"),
        "database": unit(
            "AmazonRDS", "RDS:GP3-Storage", productFamily="Database Storage"
        ),
        "kafka": unit("AmazonMSK", "Kafka.Storage.GP2", group="Storage"),
        "object": unit(
            "AmazonS3", "TimedStorage-ByteHrs", storageClass="General Purpose"
        ),
    }
    fixed = {
        "kubernetes_control_plane": unit("AmazonEKS", "AmazonEKS-Hours:perCluster"),
        "load_balancer": unit(
            "AWSELB", "LoadBalancerUsage", productFamily="Load Balancer-Application"
        ),
        "nat_gateway": unit(
            "AmazonEC2", "NatGateway-Hours", productFamily="NAT Gateway"
        ),
        "load_balancer_capacity_unit": unit(
            "AWSELB", "LCUUsage", productFamily="Load Balancer-Application"
        ),
    }
    return {
        "provider": "aws",
        "region": region,
        "currency": "USD",
        "source": "AWS Price List Query API (pricing:GetProducts), On-Demand, Linux, Shared tenancy",
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "hours_per_month": 730,
        # 공통 키를 화면에 보일 때 쓰는 이 클라우드의 상품 이름.
        "labels": {
            "compute": "EC2 (온디맨드)",
            "kubernetes_control_plane": "EKS 컨트롤 플레인",
            "load_balancer": "Application Load Balancer",
            "load_balancer_capacity_unit": "ALB LCU",
            "nat_gateway": "NAT Gateway",
            "postgres": "RDS for PostgreSQL (Single-AZ)",
            "kafka_broker": "MSK 브로커",
            "block": "EBS gp3",
            "database": "RDS gp3 스토리지",
            "kafka": "MSK 스토리지",
            "object": "S3 Standard",
        },
        "compute": compute,
        "managed": managed,
        "storage_gib_month": storage,
        "hourly": fixed,
    }


GCP_SERVICES = {
    "compute": "6F81-5844-456A",  # Compute Engine
    "sql": "9662-B51E-5089",  # Cloud SQL
    "gke": "CCD8-9BF1-090E",  # Kubernetes Engine
    "storage": "95FF-2EF5-5EA1",  # Cloud Storage
    "kafka": "9544-7B1C-811D",  # Managed Service for Apache Kafka
    "network": "E505-1604-58F8",  # Networking
}
GCP_REGION_NAMES = {"asia-northeast3": "Seoul"}
GCP_FAMILIES = {"e2": "E2", "n2": "N2", "n2d": "N2D AMD"}
GCP_STANDARD = [("e2", (2, 4, 8)), ("n2", (2, 4, 8)), ("n2d", (2, 4, 8))]
GCP_SQL_SIZES = [(1, 3840), (2, 7680), (4, 15360)]


def _gcp_skus(service: str) -> list[dict]:
    import google.auth
    import google.auth.transport.requests
    import httpx

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-billing.readonly"])
    credentials.refresh(google.auth.transport.requests.Request())
    skus, token = [], ""
    with httpx.Client(timeout=60) as client:
        while True:
            for attempt in range(6):
                response = client.get(
                    f"https://cloudbilling.googleapis.com/v1/services/{service}/skus",
                    params={"pageSize": 5000, "currencyCode": "USD", "pageToken": token},
                    headers={"Authorization": f"Bearer {credentials.token}"},
                )
                if response.status_code != 429:
                    break
                time.sleep(5 * 2**attempt)  # 분당 호출 한도
            page = response.raise_for_status().json()
            skus += page.get("skus", [])
            token = page.get("nextPageToken")
            if not token:
                return skus


def _gcp_unit(sku: dict) -> float:
    rates = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"]
    for rate in rates:
        price = int(rate["unitPrice"].get("units") or 0) + rate["unitPrice"].get("nanos", 0) / 1e9
        if price > 0:
            return price
    return 0.0


def _gcp_find(skus: list[dict], description: str, region: str) -> float:
    for sku in skus:
        if (
            sku["description"].strip() == description
            and sku["category"].get("usageType") == "OnDemand"
            and (region in sku["serviceRegions"] or "global" in sku["serviceRegions"])
        ):
            return _gcp_unit(sku)
    raise LookupError(description)


def fetch_gcp(region: str) -> dict:
    name = GCP_REGION_NAMES[region]
    skus = {key: _gcp_skus(sid) for key, sid in GCP_SERVICES.items()}
    rates = {
        family: {
            "vcpu_hourly": _gcp_find(skus["compute"], f"{label} Instance Core running in {name}", region),
            "memory_gib_hourly": _gcp_find(skus["compute"], f"{label} Instance Ram running in {name}", region),
        }
        for family, label in GCP_FAMILIES.items()
    }
    compute = {
        f"{family}-standard-{vcpu}": {
            "hourly": vcpu * rates[family]["vcpu_hourly"] + vcpu * 4 * rates[family]["memory_gib_hourly"],
            "vcpu": vcpu,
            "memory_gib": vcpu * 4,
        }
        for family, sizes in GCP_STANDARD
        for vcpu in sizes
    }
    sql = {
        "vcpu_hourly": _gcp_find(skus["sql"], f"Cloud SQL for PostgreSQL: Zonal - vCPU in {name}", region),
        "memory_gib_hourly": _gcp_find(skus["sql"], f"Cloud SQL for PostgreSQL: Zonal - RAM in {name}", region),
    }
    postgres = {
        f"db-custom-{vcpu}-{mib}": {
            "hourly": vcpu * sql["vcpu_hourly"] + mib / 1024 * sql["memory_gib_hourly"],
            "vcpu": vcpu,
            "memory_gib": mib / 1024,
        }
        for vcpu, mib in GCP_SQL_SIZES
    }
    return {
        "provider": "gcp",
        "region": region,
        "currency": "USD",
        "source": "GCP Cloud Billing Catalog API (services.skus.list), On-Demand",
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "hours_per_month": 730,
        "labels": {
            "compute": "Compute Engine",
            "kubernetes_control_plane": "GKE 클러스터 관리 요금",
            "load_balancer": "Cloud Load Balancing 전달 규칙",
            "nat_gateway": "Cloud NAT IP",
            "postgres": "Cloud SQL for PostgreSQL (Zonal)",
            "kafka_broker": "Managed Kafka",
            "block": "Persistent Disk Balanced",
            "database": "Cloud SQL 스토리지",
            "kafka": "Managed Kafka 로컬 스토리지",
            "object": "Cloud Storage Standard",
        },
        "notes": {
            "kafka_broker": "Managed Kafka 는 DCU 단위 과금이라 vCPU·메모리 → DCU 환산을 확인하기 전까지 단가 없음",
            "nat_gateway": "Cloud NAT 는 IP 사용료만. 게이트웨이 VM 당 요금·처리 GB 제외",
        },
        "compute": compute,
        "compute_rates": rates,
        "managed": {"postgres": postgres},
        "managed_rates": {"postgres": sql},
        "storage_gib_month": {
            "block": _gcp_find(skus["compute"], f"Balanced PD Capacity in {name}", region),
            "database": _gcp_find(skus["sql"], f"Cloud SQL for PostgreSQL: Zonal - Standard storage in {name}", region),
            "kafka": _gcp_find(skus["kafka"], f"Local Storage in {region}", region),
            "object": _gcp_find(skus["storage"], f"Standard Storage {name}", region),
        },
        "hourly": {
            "kubernetes_control_plane": _gcp_find(skus["gke"], "Regional Kubernetes Clusters", region),
            "load_balancer": _gcp_find(
                skus["network"], f"Cloud Load Balancer Forwarding Rule Minimum for {name} ({region})", region
            ),
            "nat_gateway": _gcp_find(skus["network"], "Networking Cloud NAT IP Usage", region),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["aws", "gcp"])
    parser.add_argument("region")
    args = parser.parse_args(argv)
    sheet = fetch_aws(args.region) if args.provider == "aws" else fetch_gcp(args.region)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.provider}-{args.region}.json"
    out.write_text(json.dumps(sheet, indent=2, ensure_ascii=False) + "\n")
    print(f"{out} ({len(sheet['compute'])} 인스턴스 유형)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
