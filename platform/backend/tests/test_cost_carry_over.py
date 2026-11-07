from datetime import UTC, datetime, timedelta

import psycopg
from kubernetes.client.exceptions import ApiException

from controlplane.cost.collect import (
    KUBERNETES_CARRY_OVER,
    carry_over_kubernetes,
    collect_inventory,
)
from controlplane.cost.models import CostInventory, Node, Volume, WorkloadDemand
from controlplane.domain.fleet import FleetConfig, MigrationConfig
from controlplane.domain.migration import Direction
from controlplane.execution.fleet_collector import FleetCollector
from controlplane.integrations.defaults import AWS, ONPREM

THEN = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
NODE = Node(name="n1", instance_type="t3.large", vcpu=2, memory_gib=8)
VOLUME = Volume(owner="hackathon/data", gib=10, storage_class="gp3")
WORKLOAD = WorkloadDemand(namespace="hackathon", name="api", replicas=1, vcpu=0.5, memory_gib=1)


def inventory(env, at, *, kube_at=None, read=True, **extra):
    return CostInventory(
        env_id=env.id,
        provider=env.provider,
        region=env.region,
        observed_at=at,
        kubernetes_observed_at=kube_at,
        nodes=[NODE] if read else [],
        volumes=[VOLUME] if read else [],
        workloads=[WORKLOAD] if read else [],
        kubernetes_control_plane=read and env.provider != "onprem",
        sources=["kubernetes"] if read else [],
        gaps=[] if read else ["클러스터 자원: 읽지 못함 (MaxRetryError)"],
        **extra,
    )


def failed(env, at):
    return inventory(env, at, read=False)


def test_failed_read_keeps_the_last_cluster_resources():
    previous = inventory(AWS, THEN, kube_at=THEN)
    current = failed(AWS, THEN + timedelta(minutes=5))
    merged = carry_over_kubernetes(current, previous)
    assert merged.nodes == [NODE]
    assert merged.volumes == [VOLUME]
    assert merged.workloads == [WORKLOAD]
    assert merged.kubernetes_control_plane is True
    assert merged.kubernetes_observed_at == THEN
    assert merged.observed_at == current.observed_at
    assert merged.gaps == current.gaps
    assert merged.sources == ["kubernetes 마지막으로 읽은 값 (11-07 00:00 UTC)"]


def test_onprem_carry_over_has_no_control_plane():
    previous = inventory(ONPREM, THEN, kube_at=THEN)
    merged = carry_over_kubernetes(failed(ONPREM, THEN + timedelta(minutes=5)), previous)
    assert merged.nodes == [NODE]
    assert merged.kubernetes_control_plane is False


def test_carry_over_stops_after_the_limit():
    previous = inventory(AWS, THEN, kube_at=THEN)
    edge = failed(AWS, THEN + KUBERNETES_CARRY_OVER)
    assert carry_over_kubernetes(edge, previous).nodes == [NODE]
    late = failed(AWS, THEN + KUBERNETES_CARRY_OVER + timedelta(seconds=1))
    assert carry_over_kubernetes(late, previous) is late


def test_repeated_failures_keep_the_original_read_time():
    first = inventory(AWS, THEN, kube_at=THEN)
    second = carry_over_kubernetes(failed(AWS, THEN + timedelta(hours=3)), first)
    third = failed(AWS, THEN + timedelta(hours=7))
    assert second.kubernetes_observed_at == THEN
    assert carry_over_kubernetes(third, second) is third


def test_rows_saved_before_the_field_use_their_observed_time():
    legacy = inventory(AWS, THEN)
    merged = carry_over_kubernetes(failed(AWS, THEN + timedelta(minutes=5)), legacy)
    assert merged.kubernetes_observed_at == THEN
    unread = failed(AWS, THEN)
    current = failed(AWS, THEN + timedelta(minutes=5))
    assert carry_over_kubernetes(current, unread) is current


def test_cluster_without_nodes_has_no_control_plane_after_carry_over():
    previous = inventory(AWS, THEN, kube_at=THEN).model_copy(update={"nodes": []})
    merged = carry_over_kubernetes(failed(AWS, THEN + timedelta(minutes=5)), previous)
    assert merged.volumes == [VOLUME]
    assert merged.kubernetes_control_plane is False


def test_successful_read_is_not_replaced():
    current = inventory(AWS, THEN + timedelta(minutes=5), kube_at=THEN + timedelta(minutes=5))
    assert carry_over_kubernetes(current, inventory(AWS, THEN, kube_at=THEN)) is current
    lone = failed(AWS, THEN)
    assert carry_over_kubernetes(lone, None) is lone


def test_collect_inventory_records_when_the_cluster_was_read():
    ok = collect_inventory(ONPREM, {}, kube=lambda e: ([NODE], [], []))
    assert ok.kubernetes_observed_at == ok.observed_at

    def broken(env):
        raise ApiException(status=503)

    assert collect_inventory(ONPREM, {}, kube=broken).kubernetes_observed_at is None


class Store:
    def __init__(self, previous=None, fail_put=(), fail_get=False):
        self.previous = previous or {}
        self.fail_put = set(fail_put)
        self.fail_get = fail_get
        self.saved = []

    def get(self, env_id):
        if self.fail_get:
            raise psycopg.OperationalError("down")
        return self.previous.get(env_id)

    def put(self, inventory):
        if inventory.env_id in self.fail_put:
            raise psycopg.OperationalError("down")
        self.saved.append(inventory)


def collector(store, environments, reader, migrations=()):
    return FleetCollector(
        FleetConfig(mode="live", environments=environments, migrations=list(migrations)),
        journal=None,
        costs=store,
        cost_reader=reader,
    )


def test_collector_saves_the_carried_over_inventory():
    store = Store(previous={"aws": inventory(AWS, THEN, kube_at=THEN)})
    collector(
        store, [AWS], lambda env, tags, meter, project: failed(env, THEN + timedelta(minutes=5))
    ).collect_costs(AWS)
    assert store.saved[0].nodes == [NODE]


def test_collector_saves_even_when_the_previous_row_cannot_be_read():
    store = Store(fail_get=True)
    current = failed(AWS, THEN)
    collector(store, [AWS], lambda env, tags, meter, project: current).collect_costs(AWS)
    assert store.saved == [current]


def test_database_errors_do_not_stop_the_tick():
    store = Store(fail_put={"onprem"})
    c = collector(store, [ONPREM, AWS], lambda env, tags, meter, project: inventory(env, THEN))
    c._reader = lambda env: (_ for _ in ()).throw(ValueError("bad facts"))
    c.tick()
    assert [i.env_id for i in store.saved] == ["aws"]
    assert c._ticks == 1


def test_worker_status_errors_do_not_stop_the_tick():
    job = MigrationConfig(
        job_id="forward_001",
        direction=Direction.FORWARD,
        source_env="onprem",
        target_env="aws",
        data_source="onprem",
        data_target="aws",
        worker_url="http://worker",
    )
    store = Store()
    c = collector(store, [AWS], lambda env, tags, meter, project: inventory(env, THEN), [job])

    def broken(job):
        raise ValueError("bad status")

    c.collect_worker = broken
    c.collect_connectors = broken
    c._reader = lambda env: (_ for _ in ()).throw(ValueError("bad facts"))
    c.tick()
    assert [i.env_id for i in store.saved] == ["aws"]
