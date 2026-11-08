import json
from copy import deepcopy

import pytest
from migration_connection.control.errors import ConnectionError
from migration_connection.inventory import InventoryError, Kubectl
from migration_connection.inventory.observation import OBSERVE_INTERVAL_MS, observe
from test_control import OTHER, OWNER, envelope, ready, report
from test_control import svc as service_fixture
from test_control_integration import pg_store as postgres_fixture
from test_inventory import SECRET, FakeKubectl

svc = service_fixture
pg_store = postgres_fixture


def register(svc):
    request_id = ready(svc)
    task, value = report(svc, request_id)
    result = call(
        svc, request_id, "inventory", {"job_id": task["job_id"], "report": value}
    )
    svc.browser(
        request_id,
        OWNER,
        "register",
        {
            "collectedId": value["target_id"],
            "inventoryDigest": result["inventory_digest"],
            "target": {
                "organization": OWNER.team,
                "project": "demo",
                "name": "home",
                "kind": "kubernetes",
                "region": "",
            },
        },
    )
    return request_id


def call(svc, request_id, action, data):
    return svc.device(
        request_id, envelope(svc, request_id, action, data), through_tunnel=True
    )


def poll(svc, request_id):
    return call(svc, request_id, "poll", {"observation_version": 1})


def tunnel(svc, request_id, epoch, *, new_key):
    svc.tunnel(
        request_id,
        {
            "epoch": epoch,
            "connection_id": f"connection_{epoch}234567890",
            "state": "READY",
            "checks": 2,
            "key_expires_at_ms": new_key,
            "lease_expires_at_ms": svc.clock() + 60_000,
        },
    )


class ObservationKubectl(FakeKubectl):
    def __call__(self, command):
        if "get" in command:
            resource = command[command.index("get") + 1]
            if resource in ("replicasets.apps", "pods"):
                self.commands.append(command)
                assert (
                    "--context=prod" in command
                    and command[command.index("-n") + 1] == "customer"
                )
                return json.dumps({"items": []}).encode()
        return super().__call__(command)


@pytest.mark.parametrize("storage", ["memory", "postgres"])
def test_registered_observation_is_bound_and_preserves_the_last_success(
    svc, request, storage
):
    if storage == "postgres":
        svc.store = request.getfixturevalue("pg_store")
    request_id = register(svc)
    original = svc.get(request_id, OWNER)
    assert call(svc, request_id, "poll", {})["task"] is None
    first = poll(svc, request_id)
    task = first["task"]["payload"]
    assert task["operation"] == "environment.observe"
    assert task["namespaces"] == ["customer"]
    assert poll(svc, request_id)["task"] == first["task"]

    runner = ObservationKubectl()
    runner.denied.add("--raw=/apis/metrics.k8s.io/v1beta1/nodes")
    client = Kubectl(runner=runner, executable="/usr/local/bin/kubectl")
    with pytest.raises(InventoryError, match="CLUSTER_IDENTITY_UNVERIFIED"):
        observe(
            client,
            target_id=task["target_id"],
            namespace_uid="wrong-cluster",
            namespaces=task["namespaces"],
        )
    value = observe(
        client,
        target_id=task["target_id"],
        namespace_uid=task["namespace_uid"],
        namespaces=task["namespaces"],
    )
    assert SECRET not in json.dumps(value)
    assert value["nodes"][0]["metadata"] == {"name": "node-1"}
    assert value["metrics"] is None
    assert value["namespaces"][0]["workloads"][0]["metadata"]["name"] == "actual-app"
    assert not any("--all-namespaces" in command for command in runner.commands)

    submission = {"job_id": task["job_id"], "report": value}
    with pytest.raises(ConnectionError, match="TUNNEL_TRANSPORT_REQUIRED"):
        svc.device(request_id, envelope(svc, request_id, "observation", submission))
    tampered = deepcopy(value)
    tampered["nodes"][0]["metadata"]["annotations"] = {"secret": SECRET}
    with pytest.raises(ConnectionError, match="INVALID_OBSERVATION"):
        call(svc, request_id, "observation", {**submission, "report": tampered})
    call(svc, request_id, "observation", submission)
    [fleet] = svc.list(OWNER, include_observations=True)
    assert fleet["observation"]["fresh"] is True
    assert fleet["observation"]["latest"]["report"] == value
    assert svc.get(request_id, OWNER)["collected"] == original["collected"]
    assert "observation" not in svc.list(OWNER)[0]
    assert "observation" not in svc.store.get(request_id)
    assert svc.list(OTHER, include_observations=True) == []
    assert poll(svc, request_id)["task"] is None

    svc.test_now[0] += OBSERVE_INTERVAL_MS
    tunnel(svc, request_id, 1, new_key=original["keyExpiresAt"])
    pending = poll(svc, request_id)["task"]["payload"]
    assert pending["job_id"] != task["job_id"]

    tunnel(svc, request_id, 2, new_key=svc.clock() + 1_800_000)
    with pytest.raises(ConnectionError, match="STALE_OBSERVATION"):
        call(
            svc, request_id, "observation", {**submission, "job_id": pending["job_id"]}
        )
    retry = poll(svc, request_id)["task"]["payload"]
    assert retry["epoch"] == 2
    call(svc, request_id, "observation", {"job_id": retry["job_id"], "report": None})
    [fleet] = svc.list(OWNER, include_observations=True)
    assert fleet["observation"]["latest"]["report"] == value
    assert fleet["observation"]["fresh"] is False
    assert fleet["observation"]["failed"] is True

    svc.browser(request_id, OWNER, "revoke", {})
    assert "observation" not in svc.list(OWNER, include_observations=True)[0]
