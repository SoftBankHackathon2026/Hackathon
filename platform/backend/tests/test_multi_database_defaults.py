"""Multi-deploy defaults reach the real adapters without making cloud calls."""

import pytest
from aws.conftest import SECRET_REF
from aws.conftest import env_file as env_file
from pydantic import SecretStr
from test_demo_deployments import TEAM, built, preview
from test_demo_deployments import helm_setup as helm_setup
from test_demo_deployments import settings as settings
from test_demo_deployments import setup as setup
from test_gcp import sdk as sdk

from controlplane.builds.store import BuildStore
from controlplane.core.errors import RequestConflict
from controlplane.demo_deployments.models import StartRun
from controlplane.demo_deployments.worker import RunWorker
from controlplane.execution.gcp.settings import GcpCacheSettings, GcpDataSettings
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.interface import DeploymentSpec
from oneport_aws.adapter import AwsAdapter
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings


@pytest.fixture
def cloud_setup(helm_setup, sdk, monkeypatch, env_file):
    service, _ = helm_setup
    sdk.settings.database_url = SecretStr(service.store.dsn)
    sdk.settings.environment.environment_id = "gcp"
    sdk.settings.allowed_images = [
        service.deployments.registry.get("helm").settings.allowed_images[0]
    ]
    sdk.settings.data_provisioning = GcpDataSettings(
        app_id="demo",
        network="projects/test-project/global/networks/oneport",
        subnetwork="projects/test-project/regions/asia-northeast3/subnetworks/oneport-cloud-run",
        allocated_ip_range="oneport-private-services",
    )
    sdk.settings.cache_provisioning = GcpCacheSettings.model_validate(
        sdk.settings.data_provisioning.model_dump(exclude={"allocated_ip_range"})
    )
    gcp = GcpAdapter(sdk.settings, store=service.deployments.store)
    aws = AwsAdapter(
        OneportEcsSettings(
            database_url=service.store.dsn,
            env_file=env_file,
            application="demo",
            environment_id="aws",
            allowed_images=sdk.settings.allowed_images,
        ),
        store=service.deployments.store,
    )
    aws.initialize()
    # Existing registrations keep their choice; individual defaults are unchanged.
    aws.register_application(port=8080, health_path="/health")
    monkeypatch.setitem(service.deployments.registry._bindings, "aws", aws)
    monkeypatch.setitem(service.deployments.registry._bindings, "gcp", gcp)
    return helm_setup


@pytest.mark.parametrize("history", [[], ["failed"], ["succeeded"], ["succeeded", "failed"]])
def test_gcp_defaults_only_before_first_success(cloud_setup, history):
    service, _ = cloud_setup
    gcp = service.deployments.registry.get("gcp")
    for index, state in enumerate(history):
        spec = DeploymentSpec.model_validate(gcp.public_environment()["request_template"])
        release = service.deployments.submit(spec, f"previous-gcp-release-{index}", TEAM)
        with service.store.connect() as conn:
            conn.execute("UPDATE releases SET state=%s WHERE id=%s", (state, release.id))
    p = preview(cloud_setup)
    assert p["ready"], p
    specs = {t["runtime"]: t["spec"] for t in p["targets"]}
    assert specs["gcp_cloud_run"]["resources"]["database"] == (
        "none" if "succeeded" in history else "postgresql"
    )
    assert specs["aws_ecs"]["resources"]["database"] == "none"
    assert specs["gcp_cloud_run"]["resources"]["redis"] == ("succeeded" not in history)


@pytest.mark.parametrize("state", ["requested", "failed", "uncertain"])
@pytest.mark.parametrize("resource", ["db_state", "cache_state"])
def test_aws_data_preparation_blocks_build_with_specific_reason(cloud_setup, state, resource):
    service, _ = cloud_setup
    aws = service.deployments.registry.get("aws")
    aws.register_application(
        port=8080,
        health_path="/health",
        database="postgresql",
        database_mode="dedicated",
        redis=True,
    )
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE oneport_apps SET db_state=%s,cache_state=%s",
            (
                state if resource == "db_state" else "ready",
                state if resource == "cache_state" else "ready",
            ),
        )
    p = preview(cloud_setup)
    assert not p["ready"]
    target = next(t for t in p["targets"] if t["runtime"] == "aws_ecs")
    assert target["blockers"] == [
        "target_data_preparing" if state == "requested" else "target_data_failed"
    ]
    assert target["spec"]["resources"]["database"] == "postgresql"
    assert target["spec"]["resources"]["redis"] is True
    with pytest.raises(RequestConflict):
        service.start(StartRun(preview_id=p["id"], idempotency_key="waiting-for-aws-db"), TEAM)
    assert not BuildStore(service.store.dsn).list()
    assert not service.deployments.store.list()


def test_all_three_real_adapters_receive_postgresql_and_redis_after_aws_data_ready(
    cloud_setup, monkeypatch
):
    service, _ = cloud_setup
    aws = service.deployments.registry.get("aws")
    aws.register_application(
        port=8080,
        health_path="/health",
        database="postgresql",
        database_mode="dedicated",
        redis=True,
    )
    # Simulate the existing AWS worker completing provisioning, with a pinned Secret ref.
    with service.store.connect() as conn:
        conn.execute("UPDATE oneport_apps SET db_state='ready',cache_state='ready'")
        aws.apps.put_ref(conn, "demo", "aws", "db", SECRET_REF.replace("/shop/", "/demo/"))
        aws.apps.put_ref(
            conn,
            "demo",
            "aws",
            "cache",
            SECRET_REF.replace("/shop/DATABASE_URL", "/demo/REDIS_URL"),
        )
    run_id, _ = built(cloud_setup, monkeypatch)
    RunWorker(service).tick()
    run = service.store.get(run_id)
    assert len(run["targets"]) == 3
    for target in run["targets"]:
        release = service.deployments.store.get(target["release_id"])
        assert release["request"]["resources"]["database"] == "postgresql"
        assert release["request"]["resources"]["redis"] is True
        assert release["request"]["resources"]["database_mode"] == (
            "container" if target["runtime"] == "onprem_helm" else "dedicated"
        )


@pytest.mark.parametrize("policy", ["data_provisioning", "cache_provisioning"])
def test_gcp_without_provisioning_policy_cannot_silently_disable_data(cloud_setup, policy):
    service, _ = cloud_setup
    setattr(service.deployments.registry.get("gcp").settings, policy, None)
    p = preview(cloud_setup)
    assert not p["ready"]
    target = next(t for t in p["targets"] if t["runtime"] == "gcp_cloud_run")
    assert target["blockers"] == ["deployment_defaults_rejected"]


@pytest.mark.parametrize("database", [False, True])
def test_gcp_preserves_existing_data_and_cache(cloud_setup, monkeypatch, database):
    service, _ = cloud_setup
    gcp = service.deployments.registry.get("gcp")
    gcp.settings.cache_provisioning = GcpCacheSettings.model_validate(
        gcp.settings.data_provisioning.model_dump(exclude={"allocated_ip_range"})
    )
    monkeypatch.setattr(
        "controlplane.providers.gcp.adapter.cache_status", lambda *args: {"phase": "ready"}
    )
    monkeypatch.setattr(
        "controlplane.providers.gcp.adapter.data_status",
        lambda *args: {"phase": "ready"} if database else None,
    )
    p = preview(cloud_setup)
    assert p["ready"], p
    target = next(t for t in p["targets"] if t["runtime"] == "gcp_cloud_run")
    assert target["spec"]["resources"] == {
        "database": "postgresql" if database else "none",
        "database_mode": "dedicated",
        "redis": True,
    }
