import io
import json
import subprocess
import tarfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderRejected,
    ReleaseKey,
    SubmissionUncertain,
)
from controlplane.providers.onprem.helm import (
    ChartArtifact,
    HelmClient,
    HelmEnvironment,
    app_plan,
    release_name,
)
from controlplane.providers.onprem.helm.observation import observe


class FakeHelm:
    def __init__(self):
        self.current = None
        self.values = None
        self.commands = []
        self.history = {}
        self.failure = None

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        args = command[7:]
        op = args[0]
        output = ""
        if op == "version":
            output = "v3.22.0"
        elif op == "list":
            output = json.dumps([self.current] if self.current else [])
        elif op == "get":
            revision = int(args[args.index("--revision") + 1])
            output = json.dumps(
                self.history[revision] if revision != int(self.current["revision"]) else self.values
            )
        elif op == "upgrade":
            if self.failure:
                raise self.failure
            name = args[2]
            self.values = json.loads(Path(args[args.index("--values") + 1]).read_text())
            self.current = {
                "name": name,
                "namespace": "oneport-apps",
                "status": "deployed",
                "revision": str(int(self.current["revision"]) + 1 if self.current else 1),
            }
            self.history[int(self.current["revision"])] = deepcopy(self.values)
        elif op == "rollback":
            self.values = deepcopy(self.history[int(args[2])])
            self.current["revision"] = str(int(self.current["revision"]) + 1)
            self.history[int(self.current["revision"])] = deepcopy(self.values)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    import hashlib

    archive = tmp_path / "app.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = b"apiVersion: v2\nname: oneport-app\nversion: 0.1.0\n"
        member = tarfile.TarInfo("oneport-app/Chart.yaml")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    artifact = ChartArtifact(path=archive, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    request = DeploymentRequest(
        application="sdk-demo",
        environment_id="helm-demo",
        source={"kind": "image", "image": "docker.io/team/demo@sha256:" + "a" * 64},
        cpu="0.5",
        memory_mib=512,
    )
    env = HelmEnvironment(
        application=request.application,
        environment_id=request.environment_id,
        hostname="sdk-demo.yubin.website",
    )
    fake = FakeHelm()
    client = HelmClient(
        binary=tmp_path / "helm",
        kubeconfig=tmp_path / "kubeconfig",
        context="test",
        expected_server="https://cluster.example",
        artifact=artifact,
        runner=fake,
    )
    monkeypatch.setattr(client, "_check_cluster", lambda plan: None)
    plan = app_plan(request, env, ReleaseKey(value="b" * 24), artifact)
    return SimpleNamespace(
        request=request, env=env, artifact=artifact, client=client, fake=fake, plan=plan
    )


def test_release_names_isolate_environments_and_long_prefixes():
    a = release_name("a" * 30, "development")
    assert len(a) <= 40
    assert a != release_name("a" * 30, "production")
    assert a != release_name("a" * 29 + "b", "development")
    assert release_name("demo", "dev", data=True).endswith("-data")


@pytest.mark.parametrize("cpu,memory", [("0.1", 128), ("0.25", 256), ("0.5", 768)])
def test_small_resources_reach_helm_values_without_changing_requests(sdk, cpu, memory):
    request = DeploymentRequest.model_validate(
        sdk.request.model_dump() | {"cpu": cpu, "memory_mib": memory}
    )
    resources = app_plan(request, sdk.env, ReleaseKey(value="b" * 24), sdk.artifact).values[
        "resources"
    ]
    assert resources == {
        "cpuRequestMilli": 100,
        "cpuLimitMilli": int(float(cpu) * 1000),
        "memoryRequestMi": 128,
        "memoryLimitMi": memory,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"port": 80},
        {"replicas": 2},
        {"memory_mib": 2048},
        {"cpu": "2"},
        {"architecture": "ARM64"},
        {"application": "foreign"},
        {"strategy": "rolling"},
        {"resources": {"database": "postgresql"}},
        {"resources": {"redis": True}},
        {"resources": {"database_mode": "dedicated"}},
    ],
)
def test_unsupported_requests_fail_before_helm(sdk, change):
    request = DeploymentRequest.model_validate(sdk.request.model_dump() | change)
    with pytest.raises(ValueError):
        app_plan(request, sdk.env, ReleaseKey(value="b" * 24), sdk.artifact)
    assert not sdk.fake.commands


def test_plan_resolves_registered_secret_references_only(sdk):
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    env = HelmEnvironment.model_validate(
        sdk.env.model_dump()
        | {"secret_refs": {"db": {"name": "test", "key": "DATABASE_URL", "uid": uid}}}
    )
    request = DeploymentRequest.model_validate(
        sdk.request.model_dump()
        | {
            "resources": {"database": "postgresql"},
            "secret_bindings": [{"name": "DATABASE_URL", "binding": "db"}],
        }
    )
    plan = app_plan(request, env, ReleaseKey(value="c" * 24), sdk.artifact)
    assert plan.values["secretBindings"] == [
        {"env": "DATABASE_URL", "name": "test", "key": "DATABASE_URL", "uid": uid}
    ]
    assert "DATABASE_URL" not in plan.values["env"]


def test_private_install_then_published_upgrade_have_separate_receipts(sdk):
    first = sdk.client.apply(sdk.plan, expected_revision=None)
    assert first.revision == 1 and first.application_verified is False
    public = app_plan(
        sdk.request, sdk.env, ReleaseKey(value="b" * 24), sdk.artifact, published=True
    )
    second = sdk.client.apply(public, expected_revision=1)
    assert second.revision == 2 and second.values_fingerprint != first.values_fingerprint
    writes = [c for c, _ in sdk.fake.commands if "upgrade" in c]
    assert len(writes) == 2
    for command in writes:
        assert not {"--take-ownership", "--force", "--atomic", "--reuse-values"}.intersection(
            command
        )
        assert not Path(command[command.index("--values") + 1]).exists()


def test_first_install_cannot_be_public(sdk):
    public = app_plan(
        sdk.request, sdk.env, ReleaseKey(value="b" * 24), sdk.artifact, published=True
    )
    with pytest.raises(ProviderConflict, match="First install"):
        sdk.client.apply(public, expected_revision=None)
    assert not any("upgrade" in c for c, _ in sdk.fake.commands)


@pytest.mark.parametrize("changed", ["revision", "status", "application", "hostname"])
def test_changed_release_blocks_upgrade(sdk, changed):
    sdk.client.apply(sdk.plan, expected_revision=None)
    if changed == "revision":
        sdk.fake.current["revision"] = "4"
    elif changed == "status":
        sdk.fake.current["status"] = "pending-upgrade"
    elif changed == "application":
        sdk.fake.values["application"] = "foreign"
    else:
        sdk.fake.values["route"]["hostname"] = "foreign.yubin.website"
    with pytest.raises(ProviderConflict):
        sdk.client.apply(sdk.plan, expected_revision=1)
    assert len([c for c, _ in sdk.fake.commands if "upgrade" in c]) == 1


def test_mutation_timeout_is_not_retried(sdk):
    sdk.fake.failure = subprocess.TimeoutExpired("helm", 210, output="super-secret")
    with pytest.raises(SubmissionUncertain) as error:
        sdk.client.apply(sdk.plan, expected_revision=None)
    assert "super-secret" not in str(error.value)
    assert len([c for c, _ in sdk.fake.commands if "upgrade" in c]) == 1


def test_failed_command_redacts_raw_helm_output(sdk):
    sdk.client.runner = lambda *a, **kw: SimpleNamespace(
        returncode=1, stderr="DB_PASSWORD=secret", stdout="secret"
    )
    with pytest.raises(SubmissionUncertain) as error:
        sdk.client._run(["upgrade"], write=True)
    assert "DB_PASSWORD" not in str(error.value) and "secret" not in str(error.value)


def test_checksum_tamper_blocks_all_commands(sdk):
    sdk.artifact.path.write_bytes(b"different chart")
    with pytest.raises(ProviderRejected, match="checksum"):
        sdk.client.apply(sdk.plan, expected_revision=None)
    assert not sdk.fake.commands


def test_values_changed_after_plan_blocks_all_commands(sdk):
    sdk.plan.values["replicas"] = 5
    with pytest.raises(ValueError, match="changed"):
        sdk.client.apply(sdk.plan, expected_revision=None)
    assert not sdk.fake.commands


def test_helm_debug_and_driver_are_not_inherited(sdk, monkeypatch):
    monkeypatch.setenv("HELM_DEBUG", "true")
    monkeypatch.setenv("HELM_DRIVER", "configmap")
    sdk.client.verify_version()
    assert all(not key.startswith("HELM_") for key in sdk.fake.commands[-1][1]["env"])


def rollout(plan):
    return {
        "metadata": {
            "name": plan.release,
            "namespace": plan.namespace,
            "uid": "uid-1",
            "generation": 2,
            "resourceVersion": "123",
            "labels": {
                "launchpad.io/application": plan.values["application"],
                "launchpad.io/environment": plan.values["environment"],
                "app.kubernetes.io/instance": plan.release,
                "app.kubernetes.io/managed-by": "Helm",
            },
            "annotations": {
                "meta.helm.sh/release-name": plan.release,
                "meta.helm.sh/release-namespace": plan.namespace,
            },
        },
        "spec": {
            "strategy": {plan.values["strategy"]["type"]: {}},
            "template": {
                "metadata": {"labels": {"launchpad.io/release": plan.values["releaseId"]}},
                "spec": {"containers": [{"image": plan.values["image"]}]},
            },
        },
        "status": {
            "observedGeneration": "2",
            "phase": "Healthy",
            "stableRS": "hash",
            "currentPodHash": "hash",
            "readyReplicas": 1,
        },
    }


@pytest.mark.parametrize(
    "status",
    [
        {"observedGeneration": "1"},
        {"abort": True},
        {"phase": "Progressing"},
        {"pauseConditions": [{}]},
        {"stableRS": "old"},
        {"readyReplicas": 0},
    ],
)
def test_controller_acceptance_does_not_mean_ready(sdk, status):
    obj = rollout(sdk.plan)
    obj["status"].update(status)
    assert not observe(obj, sdk.plan, expected_uid="uid-1").ready


def test_observation_checks_rollout_uid_and_exact_release(sdk):
    obj = rollout(sdk.plan)
    assert observe(obj, sdk.plan, expected_uid="uid-1").ready
    with pytest.raises(ProviderConflict, match="replaced"):
        observe(obj, sdk.plan, expected_uid="other")
    changed = deepcopy(obj)
    changed["spec"]["template"]["metadata"]["labels"]["launchpad.io/release"] = "c" * 24
    with pytest.raises(ProviderConflict, match="template"):
        observe(changed, sdk.plan)


def test_rollback_only_restores_a_recorded_revision(sdk):
    original = sdk.client.apply(sdk.plan, expected_revision=None)
    new = app_plan(sdk.request, sdk.env, ReleaseKey(value="c" * 24), sdk.artifact)
    sdk.client.apply(new, expected_revision=1)
    restored = sdk.client.rollback(sdk.plan, original, expected_revision=2)
    assert restored.revision == 3
    assert sdk.fake.values["releaseId"] == "b" * 24
    assert restored.application_verified is False


def test_rollback_refuses_modified_history(sdk):
    original = sdk.client.apply(sdk.plan, expected_revision=None)
    new = app_plan(sdk.request, sdk.env, ReleaseKey(value="c" * 24), sdk.artifact)
    sdk.client.apply(new, expected_revision=1)
    sdk.fake.history[1]["image"] = "foreign"
    with pytest.raises(ProviderConflict, match="Historical"):
        sdk.client.rollback(sdk.plan, original, expected_revision=2)
    assert not any("rollback" in c for c, _ in sdk.fake.commands)


@pytest.mark.parametrize("change", ["uid", "mutable", "foreign", "key", "missing"])
def test_secret_binding_is_verified_before_helm(sdk, monkeypatch, change):
    from controlplane.providers.onprem.helm import client as module

    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    env = HelmEnvironment.model_validate(
        sdk.env.model_dump()
        | {"secret_refs": {"db": {"name": "db-ref", "key": "DATABASE_URL", "uid": uid}}}
    )
    request = DeploymentRequest.model_validate(
        sdk.request.model_dump()
        | {
            "resources": {"database": "postgresql"},
            "secret_bindings": [{"name": "DATABASE_URL", "binding": "db"}],
        }
    )
    plan = app_plan(request, env, ReleaseKey(value="b" * 24), sdk.artifact)
    secret = SimpleNamespace(
        metadata=SimpleNamespace(
            uid=uid,
            labels={
                "launchpad.io/application": sdk.request.application,
                "launchpad.io/environment": sdk.request.environment_id,
            },
        ),
        immutable=True,
        data={"DATABASE_URL": "do-not-log-credentials"},
    )
    if change == "uid":
        secret.metadata.uid = "foreign"
    if change == "mutable":
        secret.immutable = False
    if change == "foreign":
        secret.metadata.labels["launchpad.io/application"] = "foreign"
    if change == "key":
        secret.data = {}
    closed = []

    def read(*args, **kwargs):
        if change == "missing":
            raise RuntimeError("do-not-log-credentials")
        return secret

    monkeypatch.setattr(
        module,
        "connect",
        lambda **kwargs: SimpleNamespace(
            core=SimpleNamespace(read_namespaced_secret=read), close=lambda: closed.append(True)
        ),
    )
    with pytest.raises((ProviderConflict, ProviderRejected)) as error:
        HelmClient._check_cluster(sdk.client, plan)
    assert "do-not-log-credentials" not in str(error.value)
    assert closed == [True]


def test_system_hostname_is_rejected(sdk):
    with pytest.raises(ValueError, match="reserved"):
        HelmEnvironment.model_validate(
            sdk.env.model_dump() | {"hostname": "kube-api.yubin.website"}
        )


def test_helm_test_uses_job_hooks_without_log_flag(sdk):
    receipt = sdk.client.apply(sdk.plan, expected_revision=None)
    sdk.client.test(receipt)
    command, kwargs = sdk.fake.commands[-1]
    assert "test" in command and "--logs" not in command
    assert kwargs["timeout"] == 150


def test_successful_write_with_lost_read_is_uncertain(sdk, monkeypatch):
    from controlplane.providers.contracts import ProviderUnavailable

    original = sdk.client.current

    def current(name):
        if sdk.fake.current:
            raise ProviderUnavailable("Read unavailable")
        return original(name)

    monkeypatch.setattr(sdk.client, "current", current)
    with pytest.raises(SubmissionUncertain, match="could not be observed"):
        sdk.client.apply(sdk.plan, expected_revision=None)
    assert len([c for c, _ in sdk.fake.commands if "upgrade" in c]) == 1
