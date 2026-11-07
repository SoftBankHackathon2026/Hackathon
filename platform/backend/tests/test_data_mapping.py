from datetime import UTC, datetime

import pytest

from controlplane.core.errors import InvalidRequest, OperationNotFound, RequestConflict
from controlplane.cost.models import CostInventory, ManagedService, WorkloadDemand
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.repositories.cutover_commands import MemoryCutoverCommands
from controlplane.repositories.data_mappings import DataMappingJournal, MemoryDataMappings
from controlplane.schemas.accounts import Identity
from controlplane.schemas.data_mapping import DataBinding, DataMappingUpdate, SavedMapping
from controlplane.schemas.migrations import CommandCreate
from controlplane.services.cutover_commands import CutoverCommandService
from controlplane.services.data_mapping import (
    DataMappingService,
    data_resources,
    inventory_hash,
    similarity,
    suggest,
)

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
ORIGIN = {"Origin": "http://127.0.0.1:4320"}
ONPREM_PG = "cluster:hackathon/postgres"
ONPREM_ORDERS = "cluster:orders/orders-db-1"
ONPREM_KAFKA = "cluster:hackathon/kafka"
AWS_REVIEW = "managed:postgres:oneaction-review-db"
AWS_ORDERS = "managed:postgres:oneaction-orders-db-1"
AWS_MSK = "managed:kafka:oneaction-review"


def inventory(env_id, *, managed=(), workloads=()):
    return CostInventory(
        env_id=env_id,
        provider="onprem" if env_id == "onprem" else "aws",
        region="test-1",
        observed_at=NOW,
        managed=list(managed),
        workloads=list(workloads),
    )


def onprem(*extra):
    return inventory(
        "onprem",
        workloads=[
            WorkloadDemand(namespace="hackathon", name="postgres", role="postgres"),
            WorkloadDemand(namespace="orders", name="orders-db-1", role="postgres", replicas=2),
            WorkloadDemand(namespace="hackathon", name="kafka", role="kafka"),
            WorkloadDemand(namespace="hackathon", name="api", role="app"),
            *extra,
        ],
        managed=[ManagedService(kind="object_storage", name="seaweedfs", storage_gib=12)],
    )


def aws(*extra):
    return inventory(
        "aws",
        managed=[
            ManagedService(kind="postgres", name="oneaction-review-db", size="db.t4g.micro"),
            ManagedService(kind="postgres", name="oneaction-orders-db-1"),
            ManagedService(kind="kafka", name="oneaction-review"),
            ManagedService(kind="load_balancer", name="alb"),
            *extra,
        ],
    )


def service(envs, store=None, now=lambda: NOW):
    return DataMappingService(DEFAULT_FLEET, envs.get, store or MemoryDataMappings(), now=now)


def confirm(bindings):
    return [DataBinding(**b) for b in bindings]


def test_resources_keep_only_data_kinds_from_managed_and_cluster():
    found = data_resources("onprem", onprem())
    assert [(r.id, r.kind, r.origin, r.detail) for r in found] == [
        (ONPREM_PG, "postgres", "cluster", "파드 1개"),
        (ONPREM_ORDERS, "postgres", "cluster", "파드 2개"),
        (ONPREM_KAFKA, "kafka", "cluster", "파드 1개"),
        ("managed:object_storage:seaweedfs", "object_storage", "managed", "12 GiB"),
    ]
    details = {r.id: r.detail for r in data_resources("aws", aws())}
    assert (details[AWS_REVIEW], details[AWS_ORDERS]) == ("db.t4g.micro", None)
    assert [r.name for r in data_resources("onprem", onprem())][:2] == [
        "hackathon/postgres",
        "orders/orders-db-1",
    ]


def test_inventory_hash_changes_with_either_side_only():
    s, t = data_resources("onprem", onprem()), data_resources("aws", aws())
    assert inventory_hash(s, t) == inventory_hash(list(reversed(s)), t)
    assert inventory_hash(s, t) != inventory_hash(s, t[:-1])
    assert inventory_hash(s, []) != inventory_hash([], s)


def test_name_similarity_ignores_cloud_words_but_keeps_numbers():
    assert similarity("orders/orders-db-1", "oneaction-orders-db-1") == 1.0
    assert similarity("orders-db-1", "orders-db-2") == pytest.approx(1 / 3)
    assert similarity("hackathon/postgres", "oneaction-review-db") == 0.0
    assert similarity("db", "rds") == 0.0


def test_suggestions_pair_single_kinds_create_missing_and_rank_names():
    views = {
        v.source: v
        for v in suggest(data_resources("onprem", onprem()), data_resources("aws", aws()))
    }
    kafka = views[ONPREM_KAFKA]
    assert (kafka.action, kafka.target, kafka.basis) == ("map", AWS_MSK, "only_pair")
    bucket = views["managed:object_storage:seaweedfs"]
    assert (bucket.action, bucket.target, bucket.basis, bucket.candidates) == (
        "create",
        None,
        "no_target",
        [],
    )
    orders = views[ONPREM_ORDERS]
    assert (orders.target, orders.basis) == (AWS_ORDERS, "name")
    assert orders.candidates == [AWS_ORDERS, AWS_REVIEW]
    plain = views[ONPREM_PG]
    assert (plain.action, plain.target, plain.basis) == ("map", None, "none")


def test_tied_names_are_not_guessed():
    targets = data_resources(
        "aws",
        inventory(
            "aws",
            managed=[
                ManagedService(kind="postgres", name="orders-a"),
                ManagedService(kind="postgres", name="orders-b"),
            ],
        ),
    )
    sources = data_resources(
        "onprem",
        inventory(
            "onprem", workloads=[WorkloadDemand(namespace="x", name="orders", role="postgres")]
        ),
    )
    [view] = suggest(sources, targets)
    assert (view.target, view.basis) == (None, "none")


def test_ambiguous_mapping_needs_review_and_blocks_only_run():
    mappings = service({"onprem": onprem(), "aws": aws()})
    view = mappings.get("forward_001")
    assert (view.status, view.required) == ("needs_review", True)
    assert view.blockers == [
        "같은 종류의 데이터 자원이 여러 개예요. 시작 전에 데이터 자원 매핑을 확정하세요: postgres"
    ]
    commands = CutoverCommandService(
        DEFAULT_FLEET, MemoryCutoverCommands(), blocker=mappings.blocker
    )
    with pytest.raises(RequestConflict, match="데이터 자원 매핑"):
        commands.request("forward_001", CommandCreate(action="run"), None)
    assert commands.request("forward_001", CommandCreate(action="abort"), None).action == "abort"


def test_single_pairs_are_ready_without_confirmation():
    envs = {
        "onprem": inventory(
            "onprem", workloads=[WorkloadDemand(namespace="a", name="pg", role="postgres")]
        ),
        "aws": inventory("aws", managed=[ManagedService(kind="postgres", name="rds")]),
    }
    view = service(envs).get("forward_001")
    assert (view.status, view.required, view.blockers) == ("ready", False, [])
    assert service(envs).blocker("forward_001") is None


def test_missing_inventory_is_reported_but_does_not_block():
    mappings = service({"onprem": onprem()})
    view = mappings.get("forward_001")
    assert (view.status, view.required, view.targets) == ("unavailable", False, [])
    assert mappings.blocker("forward_001") is None
    with pytest.raises(InvalidRequest, match="자원 목록"):
        mappings.update(
            "forward_001", DataMappingUpdate(inventory_hash=view.inventory_hash, bindings=[]), None
        )


def test_unknown_job_is_not_found():
    with pytest.raises(OperationNotFound):
        service({}).get("nope")


def valid_bindings():
    return confirm(
        [
            {"source": ONPREM_ORDERS, "action": "map", "target": AWS_ORDERS},
            {"source": ONPREM_PG, "action": "map", "target": AWS_REVIEW},
            {"source": ONPREM_KAFKA, "action": "map", "target": AWS_MSK},
            {
                "source": "managed:object_storage:seaweedfs",
                "action": "exclude",
                "reason": "  임시 파일  ",
            },
        ]
    )


def test_confirming_saves_who_and_unblocks():
    envs = {"onprem": onprem(), "aws": aws()}
    store = MemoryDataMappings()
    mappings = service(envs, store)
    digest = mappings.get("forward_001").inventory_hash
    view = mappings.update(
        "forward_001",
        DataMappingUpdate(inventory_hash=digest, bindings=valid_bindings()),
        Identity(email="ops@example.com", team="t"),
    )
    assert (view.status, view.confirmed_by, view.confirmed_at) == ("ready", "ops@example.com", NOW)
    assert {b.basis for b in view.bindings} == {"confirmed"}
    excluded = next(b for b in view.bindings if b.action == "exclude")
    assert (excluded.reason, excluded.kind, excluded.candidates) == (
        "임시 파일",
        "object_storage",
        [],
    )
    assert next(b for b in view.bindings if b.source == ONPREM_PG).candidates == [
        AWS_ORDERS,
        AWS_REVIEW,
    ]
    assert mappings.blocker("forward_001") is None
    assert store.get("forward_001").confirmed_by == "ops@example.com"
    assert service(envs, store).get("return_001").status == "needs_review"


def test_inventory_change_after_confirming_makes_mapping_stale():
    envs = {"onprem": onprem(), "aws": aws()}
    store = MemoryDataMappings()
    mappings = service(envs, store)
    digest = mappings.get("forward_001").inventory_hash
    mappings.update(
        "forward_001", DataMappingUpdate(inventory_hash=digest, bindings=valid_bindings()), None
    )
    envs["aws"] = aws(ManagedService(kind="postgres", name="new-db"))
    view = mappings.get("forward_001")
    assert (view.status, view.required, view.confirmed_by) == ("stale", True, None)
    assert view.confirmed_at == NOW
    assert {b.basis for b in view.bindings} != {"confirmed"}
    assert mappings.blocker("forward_001") == view.blockers[0]
    assert "다시 확인" in view.blockers[0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: b[:-1], "한 번씩"),
        (lambda b: [*b, b[0]], "한 번씩"),
        (lambda b: [b[0].model_copy(update={"target": AWS_MSK}), *b[1:]], "같은 종류"),
        (lambda b: [b[0].model_copy(update={"target": None}), *b[1:]], "같은 종류"),
        (lambda b: [b[0].model_copy(update={"action": "create"}), *b[1:]], "고를 수 없어요"),
        (lambda b: [*b[:-1], b[-1].model_copy(update={"reason": "   "})], "이유"),
        (lambda b: [*b[:-1], b[-1].model_copy(update={"reason": None})], "이유"),
    ],
)
def test_invalid_bindings_are_rejected(change, message):
    mappings = service({"onprem": onprem(), "aws": aws()})
    digest = mappings.get("forward_001").inventory_hash
    with pytest.raises(InvalidRequest, match=message):
        mappings.update(
            "forward_001",
            DataMappingUpdate(inventory_hash=digest, bindings=change(valid_bindings())),
            None,
        )


def test_create_without_target_is_accepted():
    mappings = service({"onprem": onprem(), "aws": aws()})
    digest = mappings.get("forward_001").inventory_hash
    bindings = [
        *valid_bindings()[:-1],
        DataBinding(source="managed:object_storage:seaweedfs", action="create"),
    ]
    view = mappings.update(
        "forward_001", DataMappingUpdate(inventory_hash=digest, bindings=bindings), None
    )
    assert next(b for b in view.bindings if b.kind == "object_storage").action == "create"


def test_outdated_hash_is_a_conflict():
    mappings = service({"onprem": onprem(), "aws": aws()})
    with pytest.raises(RequestConflict, match="새로 고친"):
        mappings.update(
            "forward_001", DataMappingUpdate(inventory_hash="old", bindings=valid_bindings()), None
        )


def test_api_reads_and_rejects_without_inventory(monkeypatch):
    from fastapi.testclient import TestClient

    from controlplane.main import create_app

    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    with TestClient(create_app(auth_mode="demo")) as client:
        path = "/api/v1/migrations/forward_001/data-mapping"
        body = client.get(path).json()
        assert (body["status"], body["source_env"], body["target_env"]) == (
            "unavailable",
            "onprem",
            "aws",
        )
        update = {"inventory_hash": body["inventory_hash"], "bindings": []}
        assert client.post(path, json=update).status_code == 403
        assert client.post(path, json=update, headers=ORIGIN).status_code == 422
        assert client.get("/api/v1/migrations/missing/data-mapping").status_code == 404
        run = client.post(
            "/api/v1/migrations/forward_001/commands", json={"action": "run"}, headers=ORIGIN
        )
        assert run.status_code == 202


def test_journal_round_trip_and_overwrite(store):
    journal = DataMappingJournal(store)
    assert journal.get("forward_001") is None
    first = SavedMapping(
        job_id="forward_001",
        inventory_hash="h1",
        bindings=valid_bindings(),
        confirmed_by=None,
        confirmed_at=NOW,
    )
    journal.put(first)
    assert journal.get("forward_001") == first
    second = first.model_copy(
        update={"inventory_hash": "h2", "bindings": valid_bindings()[:1], "confirmed_by": "a@b"}
    )
    journal.put(second)
    assert journal.get("forward_001") == second
    assert journal.get("return_001") is None


def test_two_sources_and_one_target_are_not_paired_automatically():
    sources = data_resources(
        "onprem",
        inventory(
            "onprem",
            workloads=[
                WorkloadDemand(namespace="a", name="alpha", role="postgres"),
                WorkloadDemand(namespace="b", name="beta-orders", role="postgres"),
            ],
        ),
    )
    targets = data_resources(
        "aws", inventory("aws", managed=[ManagedService(kind="postgres", name="orders")])
    )
    views = {v.source: v for v in suggest(sources, targets)}
    assert (views["cluster:a/alpha"].target, views["cluster:a/alpha"].basis) == (None, "none")
    assert (views["cluster:b/beta-orders"].target, views["cluster:b/beta-orders"].basis) == (
        "managed:postgres:orders",
        "name",
    )


def test_name_only_suggestions_still_need_confirmation():
    envs = {
        "onprem": inventory(
            "onprem",
            workloads=[
                WorkloadDemand(namespace="a", name="orders", role="postgres"),
                WorkloadDemand(namespace="a", name="payments", role="postgres"),
            ],
        ),
        "aws": inventory(
            "aws",
            managed=[
                ManagedService(kind="postgres", name="orders-rds"),
                ManagedService(kind="postgres", name="payments-rds"),
            ],
        ),
    }
    view = service(envs).get("forward_001")
    assert {b.basis for b in view.bindings} == {"name"}
    assert (view.status, view.required) == ("needs_review", True)


def test_kafka_members_in_one_namespace_are_one_cluster():
    inv = inventory(
        "onprem",
        workloads=[
            WorkloadDemand(namespace="hackathon", name="kafka-broker", role="kafka", replicas=2),
            WorkloadDemand(
                namespace="hackathon", name="kafka-controller", role="kafka", replicas=3
            ),
            WorkloadDemand(namespace="hackathon", name="kafka-zookeeper", role="kafka"),
            WorkloadDemand(namespace="hackathon", name="kafka-broker-ui", role="kafka"),
            WorkloadDemand(namespace="other", name="kafka-broker", role="kafka"),
            WorkloadDemand(namespace="hackathon", name="pg-controller", role="postgres"),
        ],
    )
    found = [(r.id, r.kind, r.detail) for r in data_resources("onprem", inv)]
    assert found == [
        ("cluster:hackathon/pg-controller", "postgres", "파드 1개"),
        (
            "cluster:hackathon/kafka",
            "kafka",
            "파드 6개 (kafka-broker, kafka-controller, kafka-zookeeper)",
        ),
        ("cluster:hackathon/kafka-broker-ui", "kafka", "파드 1개"),
        ("cluster:other/kafka", "kafka", "파드 1개"),
    ]


def test_infrastructure_buckets_are_not_migration_targets():
    inv = inventory(
        "aws",
        managed=[
            ManagedService(kind="object_storage", name="oneaction-logs-1"),
            ManagedService(kind="object_storage", name="oneaction-tfstate-1"),
            ManagedService(kind="object_storage", name="team-cloudtrail"),
            ManagedService(kind="object_storage", name="oneaction-review-docs-1"),
            ManagedService(kind="object_storage", name="catalog"),
            ManagedService(kind="postgres", name="audit-logs-db"),
        ],
    )
    assert [r.name for r in data_resources("aws", inv)] == [
        "audit-logs-db",
        "catalog",
        "oneaction-review-docs-1",
    ]


def test_live_shaped_inventory_pairs_without_review():
    envs = {
        "onprem": inventory(
            "onprem",
            workloads=[
                WorkloadDemand(namespace="hackathon", name="postgres", role="postgres"),
                WorkloadDemand(
                    namespace="hackathon", name="kafka-broker", role="kafka", replicas=2
                ),
                WorkloadDemand(
                    namespace="hackathon", name="kafka-controller", role="kafka", replicas=3
                ),
                WorkloadDemand(namespace="hackathon", name="object-storage", role="object_storage"),
            ],
        ),
        "aws": inventory(
            "aws",
            managed=[
                ManagedService(kind="postgres", name="oneaction-review-db"),
                ManagedService(kind="kafka", name="oneaction-review"),
                ManagedService(kind="object_storage", name="oneaction-logs-1"),
                ManagedService(kind="object_storage", name="oneaction-review-docs-1"),
                ManagedService(kind="object_storage", name="oneaction-tfstate-1"),
            ],
        ),
    }
    view = service(envs).get("forward_001")
    assert (view.status, view.required) == ("ready", False)
    assert {b.basis for b in view.bindings} == {"only_pair"}
    assert {b.target for b in view.bindings} == {
        "managed:postgres:oneaction-review-db",
        "managed:kafka:oneaction-review",
        "managed:object_storage:oneaction-review-docs-1",
    }
