"""비용 계산에 쓰는 자원 목록을 읽는다.

- Kubernetes: 노드(할당 가능 자원·인스턴스 유형), PVC 용량, 실행 중 파드의 requests 를 워크로드별로 합친다.
- AWS: 태그(예: Project=oneaction)로 범위를 정해 RDS·MSK·ALB·NAT Gateway·S3 를 찾는다.
  읽기 권한이 없으면 빈 목록과 함께 gaps 에 이유를 남긴다.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from pydantic import BaseModel, Field

from controlplane.cost.models import (
    HOURS_PER_MONTH,
    CostInventory,
    ManagedService,
    Node,
    TransferUsage,
    Volume,
    WorkloadDemand,
    WorkloadRole,
)
from controlplane.cost.onprem import OnPremMeter
from controlplane.domain.fleet import EnvironmentConfig
from controlplane.integrations.kube import api_client

QUANTITY = re.compile(r"^([0-9.]+)([a-zA-Z]*)$")
BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
DECIMAL = {"": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "m": 1e-3}
GIB = 2**30

# (토큰, 역할). 워크로드 이름·이미지에서 앞에서부터 찾는다.
ROLES: tuple[tuple[tuple[str, ...], WorkloadRole], ...] = (
    (("debezium", "kafka-connect", "-connect"), "app"),
    (("postgres", "postgresql"), "postgres"),
    (("kafka",), "kafka"),
    (("seaweedfs", "minio", "object-storage"), "object_storage"),
    (
        ("qdrant", "redis", "valkey", "opensearch", "elasticsearch", "mongo", "mysql"),
        "self_hosted_data",
    ),
)


def quantity(text: str | None) -> float:
    """Kubernetes 수량 문자열을 기본 단위 수로 바꾼다. cpu 는 코어, 메모리·저장소는 바이트."""
    if not text:
        return 0.0
    match = QUANTITY.match(str(text))
    if not match:
        return 0.0
    value, suffix = float(match.group(1)), match.group(2)
    return value * BINARY.get(suffix, DECIMAL.get(suffix, 1))


def role_of(name: str, images: list[str]) -> WorkloadRole:
    haystack = " ".join([name, *(image.rsplit("/", 1)[-1] for image in images)]).lower()
    for tokens, role in ROLES:
        if any(token in haystack for token in tokens):
            return role
    return "app"


def workload_name(pod: dict[str, Any]) -> str:
    """파드가 속한 워크로드 이름. ReplicaSet 이면 해시 접미사를 뗀다."""
    meta = pod.get("metadata") or {}
    owners = meta.get("ownerReferences") or []
    if not owners:
        return meta.get("name") or ""
    owner = owners[0]
    name = owner.get("name") or ""
    labels = meta.get("labels") or {}
    if owner.get("kind") == "ReplicaSet":
        for key in ("pod-template-hash", "rollouts-pod-template-hash"):
            suffix = labels.get(key)
            if suffix and name.endswith(f"-{suffix}"):
                return name.removesuffix(f"-{suffix}")
    return name


def _requests(pod: dict[str, Any]) -> tuple[float, float]:
    containers = (pod.get("spec") or {}).get("containers") or []
    cpu = memory = 0.0
    for container in containers:
        requests = (container.get("resources") or {}).get("requests") or {}
        cpu += quantity(requests.get("cpu"))
        memory += quantity(requests.get("memory"))
    return cpu, memory / GIB


def summarize(
    nodes: list[dict[str, Any]], pods: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> tuple[list[Node], list[Volume], list[WorkloadDemand]]:
    """직렬화한 노드·파드·PVC 목록에서 노드·볼륨·워크로드 요구량을 만든다."""
    claim_gib = {}
    volumes = []
    for claim in claims:
        meta = claim.get("metadata") or {}
        spec = claim.get("spec") or {}
        capacity = ((claim.get("status") or {}).get("capacity") or {}).get("storage")
        requested = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
        gib = quantity(capacity or requested) / GIB
        key = (meta.get("namespace"), meta.get("name"))
        claim_gib[key] = gib
        volumes.append(
            Volume(
                owner=f"{key[0]}/{key[1]}",
                gib=round(gib, 2),
                storage_class=spec.get("storageClassName"),
            )
        )
    per_node: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for pod in pods:
        if (pod.get("status") or {}).get("phase") != "Running":
            continue
        cpu, memory = _requests(pod)
        spec = pod.get("spec") or {}
        node_usage = per_node[spec.get("nodeName") or ""]
        node_usage[0] += cpu
        node_usage[1] += memory
        namespace = (pod.get("metadata") or {}).get("namespace") or ""
        name = workload_name(pod)
        group = groups.setdefault(
            (namespace, name),
            {"replicas": 0, "cpu": 0.0, "memory": 0.0, "storage": 0.0, "images": set()},
        )
        group["replicas"] += 1
        group["cpu"] += cpu
        group["memory"] += memory
        group["images"].update(c.get("image") or "" for c in spec.get("containers") or [])
        for volume in spec.get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                group["storage"] += claim_gib.get((namespace, claim), 0.0)
    node_list = []
    for node in nodes:
        meta = node.get("metadata") or {}
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        used = per_node.get(meta.get("name") or "", [0.0, 0.0])
        node_list.append(
            Node(
                name=meta.get("name") or "",
                instance_type=(meta.get("labels") or {}).get("node.kubernetes.io/instance-type"),
                vcpu=round(quantity(allocatable.get("cpu")), 3),
                memory_gib=round(quantity(allocatable.get("memory")) / GIB, 2),
                requested_vcpu=round(used[0], 3),
                requested_memory_gib=round(used[1], 2),
            )
        )
    demands = [
        WorkloadDemand(
            namespace=namespace,
            name=name,
            role=role_of(name, sorted(group["images"])),
            replicas=group["replicas"],
            vcpu=round(group["cpu"], 3),
            memory_gib=round(group["memory"], 3),
            storage_gib=round(group["storage"], 2),
        )
        for (namespace, name), group in sorted(groups.items())
    ]
    return node_list, volumes, demands


def read_kubernetes(
    env: EnvironmentConfig,
) -> tuple[list[Node], list[Volume], list[WorkloadDemand]]:
    api = api_client(env)
    core = client.CoreV1Api(api)
    serialize = api.sanitize_for_serialization
    nodes = [serialize(n) for n in core.list_node().items]
    pods = [
        serialize(p)
        for p in core.list_pod_for_all_namespaces(field_selector="status.phase=Running").items
    ]
    claims = [serialize(c) for c in core.list_persistent_volume_claim_for_all_namespaces().items]
    return summarize(nodes, pods, claims)


def _tag_filters(tags: dict[str, str]) -> list[dict[str, Any]]:
    return [{"Key": key, "Values": [value]} for key, value in tags.items()]


class ManagedScan(BaseModel):
    """클라우드 API 로 읽은 클러스터 밖 자원."""

    managed: list[ManagedService] = Field(default_factory=list)
    transfers: list[TransferUsage] = Field(default_factory=list)
    public_ipv4: int = 0


# 지난 7일 합계를 월(730시간)로 환산한다.
TRANSFER_DAYS = 7
MONTH_FACTOR = HOURS_PER_MONTH / (TRANSFER_DAYS * 24)


def _week_sum(cloudwatch: Any, namespace: str, metric: str, dimensions: list[dict]) -> float:
    now = datetime.now(UTC)
    points = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=now - timedelta(days=TRANSFER_DAYS),
        EndTime=now,
        Period=86400,
        Statistics=["Sum"],
    )["Datapoints"]
    return sum(p["Sum"] for p in points)


def _monthly_gib(total_bytes: float) -> float:
    return round(total_bytes / GIB * MONTH_FACTOR, 3)


def read_aws_managed(region: str, tags: dict[str, str], session: Any = None) -> ManagedScan:
    """태그 범위 안의 AWS 자원과 지난 7일 데이터 전송량. boto3 가 필요하다(aws 추가 의존성)."""
    import boto3

    session = session or boto3.session.Session(region_name=region)
    tagging = session.client("resourcegroupstaggingapi", region_name=region)
    arns: set[str] = set()
    paginator = tagging.get_paginator("get_resources")
    for page in paginator.paginate(TagFilters=_tag_filters(tags)):
        arns.update(item["ResourceARN"] for item in page["ResourceTagMappingList"])
    services: list[ManagedService] = []
    transfers: list[TransferUsage] = []
    cloudwatch = session.client("cloudwatch", region_name=region)

    def scoped(tag_list: list[dict] | None) -> bool:
        tagged = {t["Key"]: t["Value"] for t in tag_list or []}
        return all(tagged.get(k) == v for k, v in tags.items())

    rds = session.client("rds", region_name=region)
    for db in rds.describe_db_instances()["DBInstances"]:
        if db["DBInstanceArn"] in arns and db.get("Engine", "").startswith("postgres"):
            services.append(
                ManagedService(
                    kind="postgres",
                    name=db["DBInstanceIdentifier"],
                    size=db["DBInstanceClass"],
                    count=2 if db.get("MultiAZ") else 1,
                    storage_gib=float(db.get("AllocatedStorage") or 0),
                )
            )
    kafka = session.client("kafka", region_name=region)
    for cluster in kafka.list_clusters_v2().get("ClusterInfoList", []):
        if cluster["ClusterArn"] not in arns or "Provisioned" not in cluster:
            continue
        provisioned = cluster["Provisioned"]
        info = provisioned["BrokerNodeGroupInfo"]
        brokers = provisioned.get("NumberOfBrokerNodes", 0)
        volume = ((info.get("StorageInfo") or {}).get("EbsStorageInfo") or {}).get("VolumeSize", 0)
        services.append(
            ManagedService(
                kind="kafka",
                name=cluster["ClusterName"],
                size=info.get("InstanceType"),
                count=brokers,
                storage_gib=float(volume) * brokers,
            )
        )
    ec2 = session.client("ec2", region_name=region)
    names: dict[str, str] = {}
    public_ips: set[str] = set()
    # 클러스터 노드가 아닌 VM(예: Tailscale 라우터). 노드 비용은 Kubernetes 쪽에서 센다.
    for reservation in ec2.describe_instances(
        Filters=[
            {"Name": "instance-state-name", "Values": ["running"]},
            *({"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()),
        ]
    )["Reservations"]:
        for instance in reservation["Instances"]:
            tagged = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
            name = tagged.get("Name") or instance["InstanceId"]
            names[instance["InstanceId"]] = name
            if instance.get("PublicIpAddress"):
                public_ips.add(instance["PublicIpAddress"])
            if "eks:nodegroup-name" in tagged or "aws:eks:cluster-name" in tagged:
                continue
            services.append(
                ManagedService(kind="virtual_machine", name=name, size=instance["InstanceType"])
            )
            sent = _week_sum(
                cloudwatch,
                "AWS/EC2",
                "NetworkOut",
                [{"Name": "InstanceId", "Value": instance["InstanceId"]}],
            )
            transfers.append(
                TransferUsage(
                    kind="internet_egress",
                    name=name,
                    gib_per_month=_monthly_gib(sent),
                    note="VM 송신 전체(상한). VPC 안으로 돌려주는 트래픽도 섞여 있음",
                )
            )
    # 노드·VM 디스크. PVC 로 만든 볼륨은 Kubernetes 쪽에서 센다.
    for volume in ec2.describe_volumes(
        Filters=[{"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()]
    )["Volumes"]:
        tagged = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
        if "kubernetes.io/created-for/pvc/name" in tagged:
            continue
        attached = [a["InstanceId"] for a in volume.get("Attachments", [])]
        services.append(
            ManagedService(
                kind="block_volume",
                name=names.get(attached[0], attached[0]) if attached else volume["VolumeId"],
                storage_gib=float(volume["Size"]),
            )
        )
    elb = session.client("elbv2", region_name=region)
    lb_names = set()
    for lb in elb.describe_load_balancers()["LoadBalancers"]:
        if lb["LoadBalancerArn"] not in arns or lb.get("Type") != "application":
            continue
        lb_names.add(lb["LoadBalancerName"])
        services.append(ManagedService(kind="load_balancer", name=lb["LoadBalancerName"]))
        processed = _week_sum(
            cloudwatch,
            "AWS/ApplicationELB",
            "ProcessedBytes",
            [
                {
                    "Name": "LoadBalancer",
                    "Value": lb["LoadBalancerArn"].split(":loadbalancer/", 1)[1],
                }
            ],
        )
        transfers.append(
            TransferUsage(
                kind="lb_processing",
                name=lb["LoadBalancerName"],
                gib_per_month=_monthly_gib(processed),
                note="처리 GB 를 LCU-시간으로 봄(연결·규칙 차원 제외)",
            )
        )
    for nat in ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}])[
        "NatGateways"
    ]:
        if not scoped(nat.get("Tags")):
            continue
        nat_id = nat["NatGatewayId"]
        services.append(ManagedService(kind="nat_gateway", name=nat_id))
        dims = [{"Name": "NatGatewayId", "Value": nat_id}]
        processed = _week_sum(cloudwatch, "AWS/NATGateway", "BytesInFromSource", dims) + _week_sum(
            cloudwatch, "AWS/NATGateway", "BytesInFromDestination", dims
        )
        egress = _week_sum(cloudwatch, "AWS/NATGateway", "BytesOutToDestination", dims)
        transfers.append(
            TransferUsage(kind="nat_processing", name=nat_id, gib_per_month=_monthly_gib(processed))
        )
        transfers.append(
            TransferUsage(
                kind="internet_egress",
                name=f"{nat_id} 경유",
                gib_per_month=_monthly_gib(egress),
                note="NAT 를 거쳐 밖으로 나간 양",
            )
        )
    for address in ec2.describe_addresses()["Addresses"]:
        eni = address.get("NetworkInterfaceId")
        description = ""
        if eni and not scoped(address.get("Tags")):
            interfaces = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])
            description = (interfaces["NetworkInterfaces"] or [{}])[0].get("Description", "")
        lb_owned = any(description.startswith(f"ELB app/{name}/") for name in lb_names)
        if scoped(address.get("Tags")) or lb_owned:
            public_ips.add(address["PublicIp"])
    now = datetime.now(UTC)
    for arn in sorted(a for a in arns if a.startswith("arn:aws:s3:::")):
        bucket = arn.removeprefix("arn:aws:s3:::")
        points = cloudwatch.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="BucketSizeBytes",
            Dimensions=[
                {"Name": "BucketName", "Value": bucket},
                {"Name": "StorageType", "Value": "StandardStorage"},
            ],
            StartTime=now - timedelta(days=3),
            EndTime=now,
            Period=86400,
            Statistics=["Average"],
        )["Datapoints"]
        latest = max(points, key=lambda p: p["Timestamp"], default=None)
        services.append(
            ManagedService(
                kind="object_storage",
                name=bucket,
                storage_gib=round((latest["Average"] if latest else 0) / GIB, 3),
            )
        )
    return ManagedScan(managed=services, transfers=transfers, public_ipv4=len(public_ips))


GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform.read-only"


def _gcp_client() -> Any:
    import google.auth
    import google.auth.transport.requests
    import httpx

    credentials, _ = google.auth.default(scopes=[GCP_SCOPE])
    credentials.refresh(google.auth.transport.requests.Request())
    return httpx.Client(timeout=20, headers={"Authorization": f"Bearer {credentials.token}"})


def _labelled(labels: dict[str, str] | None, tags: dict[str, str]) -> bool:
    labels = labels or {}
    return all(labels.get(k) == v for k, v in tags.items())


def read_gcp_managed(
    project: str, region: str, tags: dict[str, str], client: Any = None
) -> list[ManagedService]:
    """프로젝트 안 GCP 관리형 서비스. 라벨을 붙일 수 있는 자원은 tags 를 라벨 조건으로 거른다."""
    http = client or _gcp_client()

    def get(url: str, **params: Any) -> dict[str, Any]:
        return http.get(url, params=params or None).raise_for_status().json()

    services: list[ManagedService] = []
    sql = get(f"https://sqladmin.googleapis.com/v1/projects/{project}/instances")
    for db in sql.get("items", []):
        settings = db.get("settings") or {}
        if (
            db.get("region") != region
            or not str(db.get("databaseVersion", "")).startswith("POSTGRES")
            or not _labelled(settings.get("userLabels"), tags)
        ):
            continue
        services.append(
            ManagedService(
                kind="postgres",
                name=db["name"],
                size=settings.get("tier"),
                count=2 if settings.get("availabilityType") == "REGIONAL" else 1,
                storage_gib=float(settings.get("dataDiskSizeGb") or 0),
            )
        )
    kafka = get(
        f"https://managedkafka.googleapis.com/v1/projects/{project}/locations/{region}/clusters"
    )
    for cluster in kafka.get("clusters", []):
        if not _labelled(cluster.get("labels"), tags):
            continue
        capacity = cluster.get("capacityConfig") or {}
        services.append(
            ManagedService(
                kind="kafka",
                name=cluster["name"].rsplit("/", 1)[-1],
                vcpu=float(capacity.get("vcpuCount") or 0),
                memory_gib=int(capacity.get("memoryBytes") or 0) / GIB,
            )
        )
    compute = f"https://compute.googleapis.com/compute/v1/projects/{project}"
    rules = get(f"{compute}/aggregated/forwardingRules")
    for scope, listed in (rules.get("items") or {}).items():
        if scope not in ("global", f"regions/{region}"):
            continue
        for rule in listed.get("forwardingRules", []):
            # PSC(관리형 Kafka 엔드포인트 등)는 전달 규칙 요금이 아니라 서비스 요금에 들어간다.
            if rule.get("loadBalancingScheme") in (
                "EXTERNAL",
                "EXTERNAL_MANAGED",
                "INTERNAL_MANAGED",
            ):
                services.append(ManagedService(kind="load_balancer", name=rule["name"]))
    routers = get(f"{compute}/regions/{region}/routers")
    for router in routers.get("items", []):
        for nat in router.get("nats", []):
            services.append(ManagedService(kind="nat_gateway", name=nat["name"]))
    instances = get(f"{compute}/aggregated/instances", filter='status = "RUNNING"')
    for scope, listed in (instances.get("items") or {}).items():
        if not scope.startswith(f"zones/{region}-"):
            continue
        for vm in listed.get("instances", []):
            labels = vm.get("labels") or {}
            if "goog-gke-node" in labels or not _labelled(labels, tags):
                continue
            services.append(
                ManagedService(
                    kind="virtual_machine",
                    name=vm["name"],
                    size=vm["machineType"].rsplit("/", 1)[-1],
                )
            )
    sizes: dict[str, float] = {}
    now = datetime.now(UTC)
    try:
        series = get(
            f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
            filter='metric.type="storage.googleapis.com/storage/total_bytes"',
            **{
                "interval.startTime": (now - timedelta(days=2)).isoformat(),
                "interval.endTime": now.isoformat(),
            },
        )
        for item in series.get("timeSeries", []):
            bucket = item["resource"]["labels"].get("bucket_name", "")
            points = item.get("points") or []
            if points:
                value = points[0]["value"]
                sizes[bucket] = sizes.get(bucket, 0.0) + float(
                    value.get("doubleValue") or value.get("int64Value") or 0
                )
    except Exception:  # noqa: BLE001 - 크기 지표가 없으면 0 으로 둔다
        pass
    buckets = get("https://storage.googleapis.com/storage/v1/b", project=project)
    for bucket in buckets.get("items", []):
        if bucket.get("location", "").lower() != region or not _labelled(
            bucket.get("labels"), tags
        ):
            continue
        services.append(
            ManagedService(
                kind="object_storage",
                name=bucket["name"],
                storage_gib=round(sizes.get(bucket["name"], 0.0) / GIB, 3),
            )
        )
    return services


def _scan(result: ManagedScan | list[ManagedService]) -> ManagedScan:
    return result if isinstance(result, ManagedScan) else ManagedScan(managed=result)


def collect_inventory(
    env: EnvironmentConfig,
    tags: dict[str, str],
    *,
    kube: Callable[
        [EnvironmentConfig], tuple[list[Node], list[Volume], list[WorkloadDemand]]
    ] = read_kubernetes,
    aws: Callable[[str, dict[str, str]], ManagedScan | list[ManagedService]] = read_aws_managed,
    gcp: Callable[
        [str, str, dict[str, str]], ManagedScan | list[ManagedService]
    ] = read_gcp_managed,
    meter: OnPremMeter | None = None,
    project: str | None = None,
) -> CostInventory:
    sources, gaps = [], []
    nodes: list[Node] = []
    volumes: list[Volume] = []
    workloads: list[WorkloadDemand] = []
    try:
        nodes, volumes, workloads = kube(env)
        sources.append("kubernetes")
    except (ApiException, ConfigException, OSError) as exc:
        gaps.append(f"클러스터 자원: 읽지 못함 ({type(exc).__name__})")
    scan = ManagedScan()
    if env.provider == "aws":
        if not tags:
            gaps.append("관리형 서비스: 비용 설정에 태그 범위가 없음")
        else:
            scope = ", ".join(f"{k}={v}" for k, v in tags.items())
            try:
                scan = _scan(aws(env.region, tags))
                sources.append(f"AWS 태그 {scope}")
            except Exception as exc:  # noqa: BLE001 - 권한·자격 증명·네트워크 오류 모두 수집 공백으로 남긴다
                gaps.append(f"관리형 서비스: AWS 읽기 실패 ({type(exc).__name__})")
    elif env.provider == "gcp":
        if not project:
            gaps.append("관리형 서비스: 비용 설정에 GCP 프로젝트가 없음")
        else:
            try:
                scan = _scan(gcp(project, env.region, tags))
                scope = ", ".join(f"{k}={v}" for k, v in tags.items())
                sources.append("GCP 프로젝트" + (f" 라벨 {scope}" if scope else ""))
            except Exception as exc:  # noqa: BLE001 - 권한·자격 증명·네트워크 오류 모두 수집 공백으로 남긴다
                gaps.append(f"관리형 서비스: GCP 읽기 실패 ({type(exc).__name__})")
    reading = None
    if meter is not None:
        try:
            reading = meter.read(env)
            if reading is not None:
                sources.append(f"온프렘 측정기 {reading.source}")
        except Exception as exc:  # noqa: BLE001 - 측정기 오류는 수집 공백으로 남기고 설정값을 쓴다
            gaps.append(f"온프렘 측정기: 읽기 실패 ({type(exc).__name__})")
    return CostInventory(
        env_id=env.id,
        provider=env.provider,
        region=env.region,
        observed_at=datetime.now(UTC),
        nodes=nodes,
        volumes=volumes,
        workloads=workloads,
        managed=scan.managed,
        transfers=scan.transfers,
        public_ipv4=scan.public_ipv4,
        kubernetes_control_plane=env.provider in ("aws", "gcp") and bool(nodes),
        sources=sources,
        gaps=gaps,
        onprem_reading=reading,
    )
