from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from controlplane.cost.collect import (
    collect_inventory,
    quantity,
    role_of,
    summarize,
    workload_name,
)
from controlplane.cost.config import CostConfig, MigrationCostConfig, OnPremSpec
from controlplane.cost.estimator import (
    cheapest_fit,
    estimate_added,
    price_cloud,
    price_equivalent,
    price_onprem,
    released_monthly,
)
from controlplane.cost.models import CostInventory, ManagedService, Node, Volume, WorkloadDemand
from controlplane.cost.prices import PriceSheet, available, load_sheet
from controlplane.cost.service import CostService
from controlplane.domain.fleet import FleetConfig, MigrationConfig
from controlplane.domain.migration import Direction
from controlplane.integrations.defaults import AWS, GCP, ONPREM
from controlplane.main import create_app

NOW = datetime(2026, 11, 6, tzinfo=UTC)

# 단가는 계산 확인용 가짜 값이다. 실제 가격표는 test_bundled_aws_sheet 에서만 읽는다.
SHEET = PriceSheet(
    provider="aws",
    region="test-1",
    currency="USD",
    source="test",
    fetched_at=NOW,
    hours_per_month=730,
    labels={"compute": "EC2"},
    compute={
        "t3.small": {"hourly": 0.02, "vcpu": 2, "memory_gib": 2},
        "t3.medium": {"hourly": 0.04, "vcpu": 2, "memory_gib": 4},
        "t3.large": {"hourly": 0.08, "vcpu": 2, "memory_gib": 8},
        "m6i.large": {"hourly": 0.1, "vcpu": 2, "memory_gib": 8},
    },
    managed={
        "postgres": {
            "db.t4g.micro": {"hourly": 0.01, "vcpu": 2, "memory_gib": 1},
            "db.t4g.small": {"hourly": 0.02, "vcpu": 2, "memory_gib": 2},
        },
        "kafka_broker": {"kafka.t3.small": {"hourly": 0.05}},
    },
    storage_gib_month={"block": 0.1, "database": 0.2, "kafka": 0.1, "object": 0.02},
    hourly={"kubernetes_control_plane": 0.1, "load_balancer": 0.02, "nat_gateway": 0.05},
)


def monthly(hourly: float) -> float:
    return round(hourly * 730, 2)


def inventory(provider="aws", **extra) -> CostInventory:
    return CostInventory(
        env_id=provider, provider=provider, region="test-1", observed_at=NOW, **extra
    )


def test_quantity_and_workload_names():
    assert quantity("250m") == pytest.approx(0.25)
    assert quantity("2") == 2
    assert quantity("512Mi") == 512 * 2**20
    assert quantity("1G") == 1e9
    assert quantity(None) == 0
    pod = {
        "metadata": {
            "name": "review-api-6d4f9-abcde",
            "labels": {"pod-template-hash": "6d4f9"},
            "ownerReferences": [{"kind": "ReplicaSet", "name": "review-api-6d4f9"}],
        }
    }
    assert workload_name(pod) == "review-api"
    rollout = {
        "metadata": {
            "labels": {"rollouts-pod-template-hash": "77c"},
            "ownerReferences": [{"kind": "ReplicaSet", "name": "sample-app-77c"}],
        }
    }
    assert workload_name(rollout) == "sample-app"
    assert role_of("postgres", ["postgres:16"]) == "postgres"
    assert role_of("migration-connect", ["quay.io/debezium/connect:3"]) == "app"
    assert role_of("kafka-broker", ["apache/kafka:3.9"]) == "kafka"
    assert role_of("object-storage", ["chrislusf/seaweedfs"]) == "object_storage"
    assert role_of("qdrant", ["qdrant/qdrant"]) == "self_hosted_data"
    assert role_of("review-api", ["ghcr.io/x/review"]) == "app"


def _pod(ns, name, node, cpu, memory, claim=None, phase="Running"):
    spec = {
        "nodeName": node,
        "containers": [
            {"image": f"{name}:1", "resources": {"requests": {"cpu": cpu, "memory": memory}}}
        ],
    }
    if claim:
        spec["volumes"] = [{"persistentVolumeClaim": {"claimName": claim}}]
    return {
        "metadata": {
            "namespace": ns,
            "name": f"{name}-0",
            "ownerReferences": [{"kind": "StatefulSet", "name": name}],
        },
        "spec": spec,
        "status": {"phase": phase},
    }


def test_summarize_sums_requests_per_workload_and_node():
    nodes = [
        {
            "metadata": {
                "name": "n1",
                "labels": {"node.kubernetes.io/instance-type": "t3.medium"},
            },
            "status": {"allocatable": {"cpu": "1930m", "memory": "3Gi"}},
        }
    ]
    pods = [
        _pod("app", "postgres", "n1", "500m", "1Gi", claim="data"),
        _pod("app", "postgres", "n1", "500m", "1Gi", claim="data"),
        _pod("app", "web", "n1", "100m", "128Mi", phase="Pending"),
    ]
    claims = [
        {
            "metadata": {"namespace": "app", "name": "data"},
            "spec": {"storageClassName": "gp3", "resources": {"requests": {"storage": "5Gi"}}},
            "status": {"capacity": {"storage": "8Gi"}},
        }
    ]
    node_list, volumes, demands = summarize(nodes, pods, claims)
    assert node_list[0].instance_type == "t3.medium"
    assert node_list[0].vcpu == pytest.approx(1.93)
    assert node_list[0].requested_vcpu == pytest.approx(1.0)
    assert node_list[0].requested_memory_gib == pytest.approx(2.0)
    assert volumes == [Volume(owner="app/data", gib=8.0, storage_class="gp3")]
    assert demands == [
        WorkloadDemand(
            namespace="app",
            name="postgres",
            role="postgres",
            replicas=2,
            vcpu=1.0,
            memory_gib=2.0,
            storage_gib=16.0,
        )
    ]


def test_cloud_price_counts_nodes_control_plane_volumes_and_managed():
    inv = inventory(
        nodes=[
            Node(name="a", instance_type="t3.medium", vcpu=2, memory_gib=4),
            Node(name="b", instance_type="t3.medium", vcpu=2, memory_gib=4),
            Node(name="c", instance_type="x9.huge", vcpu=2, memory_gib=4),
        ],
        volumes=[Volume(owner="a/b", gib=10)],
        kubernetes_control_plane=True,
        managed=[
            ManagedService(kind="postgres", name="db", size="db.t4g.micro", storage_gib=20),
            ManagedService(kind="kafka", name="mq", size="kafka.t3.small", count=2, storage_gib=40),
            ManagedService(kind="load_balancer", name="alb"),
            ManagedService(kind="virtual_machine", name="router", size="t3.small"),
        ],
    )
    cost = price_cloud(inv, SHEET)
    by_item = {line.item: line.monthly for line in cost.lines}
    assert by_item["EC2 t3.medium"] == monthly(0.04) * 2
    assert by_item["EC2 x9.huge"] is None
    assert cost.unpriced == 1
    assert by_item["kubernetes_control_plane"] == monthly(0.1)
    assert by_item["block"] == 1.0
    assert by_item["postgres db.t4g.micro · db"] == monthly(0.01)
    assert by_item["database"] == 4.0
    assert by_item["kafka_broker kafka.t3.small · mq"] == round(monthly(0.05) * 2, 2)
    assert by_item["load_balancer · alb"] == monthly(0.02)
    assert by_item["EC2 t3.small · router"] == monthly(0.02)
    assert cost.monthly == round(sum(v for v in by_item.values() if v), 2)


def test_onprem_tco_needs_owner_input_and_equivalent_uses_cheapest_fit():
    empty = price_onprem(OnPremSpec())
    assert empty.unpriced == 2 and empty.monthly == 0
    filled = price_onprem(
        OnPremSpec(
            hardware_usd=720,
            amortization_months=36,
            power_watts=50,
            electricity_usd_per_kwh=0.1,
            other_monthly_usd=5,
        )
    )
    assert filled.unpriced == 0
    assert filled.monthly == round(20 + 50 / 1000 * 730 * 0.1 + 5, 2)
    assert cheapest_fit(SHEET, 2, 5, ["t3", "m6i"])[0] == "t3.large"
    equivalent = price_equivalent(
        inventory("onprem", nodes=[Node(name="k3s", vcpu=2, memory_gib=3.8)]),
        SHEET,
        ["t3"],
    )
    assert equivalent.lines[0].item == "k3s → t3.medium"
    assert equivalent.monthly == monthly(0.04)


SOURCE = inventory(
    "onprem",
    nodes=[Node(name="k3s", vcpu=4, memory_gib=8, requested_vcpu=2, requested_memory_gib=4)],
    volumes=[Volume(owner="hackathon/data", gib=5)],
    workloads=[
        WorkloadDemand(namespace="hackathon", name="api", vcpu=1, memory_gib=2),
        WorkloadDemand(
            namespace="hackathon",
            name="postgres",
            role="postgres",
            vcpu=0.5,
            memory_gib=1,
            storage_gib=5,
        ),
        WorkloadDemand(
            namespace="hackathon",
            name="kafka-broker",
            role="kafka",
            replicas=3,
            vcpu=0.3,
            memory_gib=1.5,
            storage_gib=15,
        ),
        WorkloadDemand(
            namespace="hackathon", name="object-storage", role="object_storage", storage_gib=3
        ),
        WorkloadDemand(namespace="harbor", name="harbor-core", vcpu=1, memory_gib=2),
    ],
)


def test_migration_to_existing_cluster_grows_same_node_type_and_maps_data_to_managed():
    target = inventory(
        nodes=[
            Node(
                name=f"n{i}",
                instance_type="t3.medium",
                vcpu=2,
                memory_gib=4,
                requested_vcpu=1.6,
                requested_memory_gib=3.2,
            )
            for i in range(3)
        ],
    )
    added, assumptions = estimate_added(
        SOURCE, target, SHEET, ["hackathon"], MigrationCostConfig(headroom=0.0)
    )
    items = {line.item: line for line in added.lines}
    # 앱 1 vCPU·2 GiB 만 노드에 올라간다. 남는 메모리 0 이라 t3.medium 을 더한다.
    assert items["EC2 t3.medium 추가"].quantity == 1
    assert "kubernetes_control_plane" not in items
    # 메모리 1 GiB(여유율 0) → 1 GiB 클래스 중 가장 싼 db.t4g.micro
    assert items["postgres db.t4g.micro · hackathon/postgres"].monthly == monthly(0.01)
    assert items["database"].quantity == 20
    kafka = items["kafka_broker kafka.t3.small · hackathon/kafka-broker"]
    assert kafka.quantity == 3
    assert items["object · hackathon/object-storage"].quantity == 3
    assert any("관리형" in a for a in assumptions)


def test_migration_to_new_cluster_adds_control_plane_and_minimum_nodes():
    added, _ = estimate_added(
        SOURCE,
        None,
        SHEET,
        ["hackathon"],
        MigrationCostConfig(headroom=0.0, data_strategy="self_hosted", min_nodes=2),
    )
    items = {line.item: line for line in added.lines}
    assert items["kubernetes_control_plane"].monthly == monthly(0.1)
    compute = next(line for line in added.lines if line.category == "compute")
    assert compute.quantity >= 2
    assert not any(line.category in ("database", "messaging") for line in added.lines)
    assert items["block"].quantity == 5 + 15 + 3


def test_released_cost_is_request_share_on_cloud_and_zero_on_onprem():
    cloud = inventory(
        nodes=[
            Node(
                name="n",
                instance_type="t3.medium",
                vcpu=2,
                memory_gib=4,
                requested_vcpu=1,
                requested_memory_gib=2,
            )
        ],
        volumes=[Volume(owner="platform/x", gib=10), Volume(owner="other/y", gib=10)],
        workloads=[WorkloadDemand(namespace="platform", name="api", vcpu=0.5, memory_gib=1)],
    )
    released, why = released_monthly(cloud, SHEET, ["platform"])
    assert released == round(monthly(0.04) * 0.5 + 1.0, 2)
    assert "50%" in why
    assert released_monthly(SOURCE, SHEET, ["hackathon"])[0] == 0.0


FLEET = FleetConfig(
    mode="live",
    environments=[ONPREM, AWS, GCP],
    migrations=[
        MigrationConfig(
            job_id="forward_001",
            direction=Direction.FORWARD,
            source_env="onprem",
            target_env="aws",
            data_source="onprem",
            data_target="aws",
        ),
        MigrationConfig(
            job_id="return_001",
            direction=Direction.RETURN,
            source_env="aws",
            target_env="onprem",
            data_source="aws",
            data_target="onprem",
        ),
    ],
)


def test_service_overview_estimates_both_directions():
    target = inventory(
        nodes=[
            Node(
                name="n",
                instance_type="t3.medium",
                vcpu=2,
                memory_gib=4,
                requested_vcpu=0.5,
                requested_memory_gib=1,
            )
        ],
        workloads=[WorkloadDemand(namespace="platform", name="api", vcpu=0.2, memory_gib=0.5)],
        kubernetes_control_plane=True,
    )
    inventories = {"onprem": SOURCE, "aws": target}
    service = CostService(
        FLEET,
        CostConfig.model_validate({"migrations": {"forward_001": {"headroom": 0}}}),
        inventories.get,
        lambda provider, region: SHEET if provider == "aws" else None,
    )
    overview = service.overview()
    costs = {e.env_id: e for e in overview.environments}
    assert costs["onprem"].current.unpriced == 2
    assert costs["onprem"].cloud_equivalent.monthly > 0
    assert costs["aws"].current.monthly == round(monthly(0.04) + monthly(0.1), 2)
    assert "가격표 없음: gcp asia-northeast3" in costs["gcp"].gaps
    assert costs["gcp"].current is None
    forward, back = overview.migrations
    assert forward.source_released == 0.0
    assert forward.monthly_change == forward.added.monthly
    assert forward.target_monthly_after == round(
        costs["aws"].current.monthly + forward.added.monthly, 2
    )
    assert any("동급 환산" in gap for gap in forward.gaps)
    assert back.added is None and back.capacity_fits is True
    assert back.monthly_change == -back.source_released
    assert back.dual_run_cost == 0


def test_collect_inventory_records_gaps_instead_of_failing():
    def no_kube(env):
        raise ApiException(status=403)

    def no_aws(region, tags):
        raise RuntimeError("denied")

    inv = collect_inventory(AWS, {"Project": "x"}, kube=no_kube, aws=no_aws)
    assert inv.sources == []
    assert inv.gaps == [
        "클러스터 자원: 읽지 못함 (ApiException)",
        "관리형 서비스: AWS 읽기 실패 (RuntimeError)",
    ]
    untagged = collect_inventory(AWS, {}, kube=lambda env: ([], [], []), aws=no_aws)
    assert untagged.gaps == ["관리형 서비스: 비용 설정에 태그 범위가 없음"]
    assert collect_inventory(ONPREM, {}, kube=lambda env: ([], [], [])).gaps == []


def test_bundled_aws_sheet_is_complete():
    assert ("aws", "ap-northeast-2") in available()
    sheet = load_sheet("aws", "ap-northeast-2")
    assert sheet.compute["t3.medium"].vcpu == 2
    assert {"postgres", "kafka_broker"} <= set(sheet.managed)
    assert {"block", "database", "kafka", "object"} <= set(sheet.storage_gib_month)
    assert {"kubernetes_control_plane", "load_balancer", "nat_gateway"} <= set(sheet.hourly)
    assert load_sheet("aws", "nowhere-1") is None


def test_costs_api_without_collected_inventory(monkeypatch):
    for name in ("ONEPORT_CONFIG", "ONEPORT_RUNTIME_CONFIG", "ONEPORT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/costs").json()
    assert [e["env_id"] for e in body["environments"]] == ["onprem", "aws", "gcp"]
    assert all("자원 목록을 아직 수집하지 않음" in e["gaps"] for e in body["environments"])


def test_cost_store_round_trip(store):
    from controlplane.cost.store import CostStore

    store.migrate()
    costs = CostStore(store)
    assert costs.get("aws") is None
    saved = inventory(nodes=[Node(name="n", instance_type="t3.medium", vcpu=2, memory_gib=4)])
    costs.put(saved)
    costs.put(saved.model_copy(update={"gaps": ["x"]}))
    assert costs.get("aws") == saved.model_copy(update={"gaps": ["x"]})


def test_meter_reading_overrides_owner_input_per_field():
    from controlplane.cost.onprem import OnPremReading

    spec = OnPremSpec(hardware_usd=720, power_watts=100, electricity_usd_per_kwh=0.1)
    reading = OnPremReading(source="plug", power_watts=40)
    cost = price_onprem(spec, reading)
    power = next(line for line in cost.lines if line.item == "전기")
    assert power.basis == "measured"
    assert power.quantity == 40
    assert power.monthly == round(40 / 1000 * 730 * 0.1, 2)
    assert power.note == "측정: plug"
    hardware = next(line for line in cost.lines if line.item.startswith("장비"))
    assert hardware.basis == "owner_input" and hardware.monthly == 20
    assert cost.label == "지금 월 비용 (측정값 포함)"
    # 측정값만 있고 전기 단가가 없으면 전기 줄은 여전히 입력 필요
    only_watts = price_onprem(OnPremSpec(), reading)
    assert next(line for line in only_watts.lines if line.item == "전기").monthly is None


def test_registered_meter_is_created_and_read_by_collector():
    from controlplane.cost.onprem import OnPremReading, create_meter, register_meter

    @register_meter("test-fixed")
    class FixedMeter:
        def __init__(self, options):
            self.watts = options["watts"]

        def read(self, env):
            return OnPremReading(source="test-fixed", power_watts=self.watts)

    meter = create_meter("test-fixed", {"watts": 55})
    inv = collect_inventory(ONPREM, {}, kube=lambda env: ([], [], []), meter=meter)
    assert inv.onprem_reading.power_watts == 55
    assert "온프렘 측정기 test-fixed" in inv.sources
    with pytest.raises(KeyError):
        create_meter("nope", {})

    class Failing:
        def read(self, env):
            raise TimeoutError

    failed = collect_inventory(ONPREM, {}, kube=lambda env: ([], [], []), meter=Failing())
    assert failed.onprem_reading is None
    assert failed.gaps == ["온프렘 측정기: 읽기 실패 (TimeoutError)"]


def test_collector_keeps_running_with_unknown_meter():
    from controlplane.cost.config import EnvironmentCostConfig, MeterConfig
    from controlplane.execution.fleet_collector import FleetCollector

    saved = []

    class Store:
        def put(self, inventory):
            saved.append(inventory)

    collector = FleetCollector(
        FleetConfig(mode="live", environments=[ONPREM], migrations=[]),
        journal=None,
        costs=Store(),
        cost_config=CostConfig(
            environments={"onprem": EnvironmentCostConfig(meter=MeterConfig(name="missing"))}
        ),
        cost_reader=lambda env, tags, meter: collect_inventory(
            env, tags, kube=lambda e: ([], [], []), meter=meter
        ),
    )
    collector.collect_costs(ONPREM)
    assert saved[0].gaps == ["온프렘 측정기: 읽기 실패 (KeyError)"]
