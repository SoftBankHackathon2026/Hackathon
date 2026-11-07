import copy

import boto3
import pytest
from botocore import stub

from crosscloud import target_deploy

_PROFILE = target_deploy.TargetProfile(
    secret_env=(
        target_deploy.SecretEnv("DATABASE_URL", "ccx-target-profile", "3"),
    ),
    plain_env=(("PGSSLMODE", "verify-full"), ("DB_SSL_CA", "PEM")),
)
_SERVICE = "projects/p/locations/asia-northeast3/services/ccx-app"
_RUN = "https://run.googleapis.com/v2/"


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Cloud Run Admin API v2 의 GET·PATCH 를 흉내 낸다."""

    def __init__(self, operation_polls=1, op_error=None):
        self.service = {
            "name": _SERVICE,
            "template": {
                "revision": "ccx-app-pinned",
                "containers": [
                    {
                        "name": "app",
                        "env": [
                            {"name": "APP_ENV", "value": "gcp"},
                            {
                                "name": "DATABASE_URL",
                                "valueSource": {
                                    "secretKeyRef": {
                                        "secret": "ccx-source-profile",
                                        "version": "1",
                                    }
                                },
                            },
                            {"name": "PGSSLMODE", "value": "require"},
                        ],
                    }
                ],
            },
            "latestReadyRevision": f"{_SERVICE}/revisions/ccx-app-1",
        }
        self.patched = None
        self.polls_left = operation_polls
        self.op_error = op_error

    def get(self, url, timeout):
        if url == _RUN + _SERVICE:
            return _Response(copy.deepcopy(self.service))
        assert url == _RUN + "projects/p/locations/x/operations/op1"
        self.polls_left -= 1
        done = self.polls_left <= 0
        body = {"name": "projects/p/locations/x/operations/op1", "done": done}
        if done and self.op_error:
            body["error"] = self.op_error
        if done and not self.op_error:
            self.service = {
                **self.patched,
                "latestReadyRevision": f"{_SERVICE}/revisions/ccx-app-2",
            }
        return _Response(body)

    def patch(self, url, json, timeout):
        assert url == _RUN + _SERVICE
        self.patched = json
        return _Response(
            {"name": "projects/p/locations/x/operations/op1", "done": False}
        )


def _cloud_run(session):
    return target_deploy.CloudRunDeployer(
        session, _SERVICE, sleep=lambda _: None
    )


def test_cloud_run_swaps_secret_and_plain_env_into_new_revision():
    session = _FakeSession(operation_polls=2)

    revision = _cloud_run(session).deploy(_PROFILE)

    template = session.patched["template"]
    env = {entry["name"]: entry for entry in template["containers"][0]["env"]}
    assert env["DATABASE_URL"]["valueSource"]["secretKeyRef"] == {
        "secret": "ccx-target-profile",
        "version": "3",
    }
    assert env["PGSSLMODE"] == {"name": "PGSSLMODE", "value": "verify-full"}
    assert env["DB_SSL_CA"] == {"name": "DB_SSL_CA", "value": "PEM"}
    assert env["APP_ENV"] == {"name": "APP_ENV", "value": "gcp"}
    assert "revision" not in template
    assert revision.endswith("ccx-app-2")


def test_cloud_run_operation_error_is_deploy_error():
    session = _FakeSession(op_error={"code": 9, "message": "boom"})

    with pytest.raises(target_deploy.DeployError, match="boom"):
        _cloud_run(session).deploy(_PROFILE)


def test_cloud_run_gives_up_at_timeout():
    session = _FakeSession(operation_polls=10**6)
    clock = iter(range(0, 10**6, 10))
    deployer = target_deploy.CloudRunDeployer(
        session,
        _SERVICE,
        sleep=lambda _: None,
        clock=lambda: next(clock),
        timeout_seconds=30,
    )

    with pytest.raises(target_deploy.DeployError, match="시간"):
        deployer.deploy(_PROFILE)


_CLUSTER = "arn:aws:ecs:ap-northeast-2:1:cluster/ccx"
_TD_OLD = "arn:aws:ecs:ap-northeast-2:1:task-definition/ccx-app:4"
_TD_NEW = "arn:aws:ecs:ap-northeast-2:1:task-definition/ccx-app:5"
_SRC_SECRET = "arn:aws:secretsmanager:ap-northeast-2:1:secret:src-AbC"
_TGT_SECRET = "arn:aws:secretsmanager:ap-northeast-2:1:secret:tgt-DeF"
_TASK_DEFINITION = {
    "taskDefinitionArn": _TD_OLD,
    "family": "ccx-app",
    "taskRoleArn": "arn:aws:iam::1:role/task",
    "executionRoleArn": "arn:aws:iam::1:role/exec",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "256",
    "memory": "512",
    "revision": 4,
    "status": "ACTIVE",
    "containerDefinitions": [
        {
            "name": "app",
            "image": "1.dkr.ecr/app@sha256:abc",
            "environment": [{"name": "PGSSLMODE", "value": "require"}],
            "secrets": [{"name": "DATABASE_URL", "valueFrom": _SRC_SECRET}],
        }
    ],
}


def _ecs_client():
    return boto3.client(
        "ecs",
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def _stable(task_definition):
    return {
        "services": [
            {
                "serviceName": "ccx-app",
                "taskDefinition": task_definition,
                "runningCount": 1,
                "desiredCount": 1,
                "deployments": [{"id": "d", "status": "PRIMARY"}],
            }
        ]
    }


def test_ecs_registers_same_family_revision_and_waits_stable():
    client = _ecs_client()
    profile = target_deploy.TargetProfile(
        secret_env=(target_deploy.SecretEnv("DATABASE_URL", _TGT_SECRET),),
        plain_env=(("PGSSLMODE", "verify-full"), ("DB_SSL_CA", "PEM")),
    )
    expected_container = {
        **_TASK_DEFINITION["containerDefinitions"][0],
        "environment": [
            {"name": "PGSSLMODE", "value": "verify-full"},
            {"name": "DB_SSL_CA", "value": "PEM"},
        ],
        "secrets": [{"name": "DATABASE_URL", "valueFrom": _TGT_SECRET}],
    }
    with stub.Stubber(client) as stubber:
        stubber.add_response(
            "describe_services",
            _stable(_TD_OLD),
            {"cluster": _CLUSTER, "services": ["ccx-app"]},
        )
        stubber.add_response(
            "describe_task_definition",
            {"taskDefinition": _TASK_DEFINITION},
            {"taskDefinition": _TD_OLD},
        )
        stubber.add_response(
            "register_task_definition",
            {
                "taskDefinition": {
                    **_TASK_DEFINITION,
                    "taskDefinitionArn": _TD_NEW,
                }
            },
            {
                "family": "ccx-app",
                "taskRoleArn": "arn:aws:iam::1:role/task",
                "executionRoleArn": "arn:aws:iam::1:role/exec",
                "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"],
                "cpu": "256",
                "memory": "512",
                "containerDefinitions": [expected_container],
            },
        )
        stubber.add_response(
            "update_service",
            {"service": {"serviceName": "ccx-app"}},
            {
                "cluster": _CLUSTER,
                "service": "ccx-app",
                "taskDefinition": _TD_NEW,
            },
        )
        stubber.add_response(
            "describe_services",
            _stable(_TD_NEW),
            {"cluster": _CLUSTER, "services": ["ccx-app"]},
        )

        revision = target_deploy.EcsDeployer(
            client, _CLUSTER, "ccx-app", waiter_delay=1
        ).deploy(profile)

    assert revision == _TD_NEW


def test_ecs_rejects_secret_version_field():
    profile = target_deploy.TargetProfile(
        secret_env=(target_deploy.SecretEnv("DATABASE_URL", _TGT_SECRET, "3"),),
        plain_env=(),
    )

    with pytest.raises(ValueError, match="version"):
        target_deploy.EcsDeployer(_ecs_client(), _CLUSTER, "ccx-app").deploy(
            profile
        )


def test_load_deploy_spec_builds_platform_deployer(tmp_path):
    spec = tmp_path / "target-deploy.yaml"
    spec.write_text(
        "platform: cloud_run\n"
        f"service: {_SERVICE}\n"
        "secret_env:\n"
        "  DATABASE_URL: {secret: ccx-target-profile, version: '3'}\n"
    )

    loaded = target_deploy.load_deploy_spec(spec)

    assert loaded.platform == "cloud_run"
    assert loaded.service == _SERVICE
    assert loaded.secret_env == (
        target_deploy.SecretEnv("DATABASE_URL", "ccx-target-profile", "3"),
    )


@pytest.mark.parametrize(
    "text",
    [
        "platform: lambda\nservice: s\nsecret_env: {}\n",
        "platform: ecs\nservice: s\nsecret_env: {A: {secret: x}}\n",
        "platform: cloud_run\nsecret_env: {A: {secret: x}}\n",
        "platform: cloud_run\nservice: s\nsecret_env: {}\n",
    ],
)
def test_load_deploy_spec_rejects_incomplete_spec(tmp_path, text):
    spec = tmp_path / "target-deploy.yaml"
    spec.write_text(text)

    with pytest.raises(ValueError):
        target_deploy.load_deploy_spec(spec)
