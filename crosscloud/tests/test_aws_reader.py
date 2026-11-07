import datetime
import json

import boto3
import pytest
from botocore import stub

from crosscloud import inventory_json, model
from crosscloud.discover import aws

_REGION = "ap-northeast-2"
_ACCOUNT = "111122223333"
_NOW = datetime.datetime(2026, 11, 7, 9, 0, tzinfo=datetime.UTC)
_CLUSTER = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/demo"
_SERVICE = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:service/demo/request-api"
_TASK_DEF = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task-definition/request-api:3"
_TARGET_GROUP = f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:tg/a/1"
_SECRET = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:app-db-AbCdEf"
_DB = f"arn:aws:rds:{_REGION}:{_ACCOUNT}:db:app"
_REPO = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/request-api"
_DIGEST = "sha256:" + "b" * 64

# 원본 env 에 실린 비밀값. 어떤 출력에도 나오면 안 된다.
_CANARIES = (
    "canary-pw-7f3a91",
    "postgres://app:canary-url-pw-11@db.internal:5432/shop",
    "Zq8canaryR2Lm9Xv4Tn7Wk1Pb6Hs3Jd5Fg0",
    "canary ambiguous auth 55",
)


class _Clients:
    """서비스별 실제 boto3 클라이언트와 Stubber. 네트워크에 나가지 않는다."""

    def __init__(self):
        self.clients = {}
        self.stubbers = {}

    def __call__(self, service_name):
        if service_name not in self.clients:
            client = boto3.client(
                service_name,
                region_name=_REGION,
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            )
            self.clients[service_name] = client
            self.stubbers[service_name] = stub.Stubber(client)
            self.stubbers[service_name].activate()
        return self.clients[service_name]

    def add(self, service_name, method, response, expected_params=None):
        self(service_name)
        self.stubbers[service_name].add_response(
            method, response, expected_params
        )

    def add_error(self, service_name, method, code):
        self(service_name)
        self.stubbers[service_name].add_client_error(
            method, service_error_code=code, http_status_code=403
        )

    def assert_all_used(self):
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


def _container(**overrides):
    container = {
        "name": "app",
        "image": f"{_REPO}:v1",
        "environment": [
            {"name": "LOG_LEVEL", "value": "info"},
            {"name": "DB_PASSWORD", "value": _CANARIES[0]},
            {"name": "LEGACY_URL", "value": _CANARIES[1]},
            {"name": "OPAQUE", "value": _CANARIES[2]},
            {"name": "AUTH_HEADER", "value": _CANARIES[3]},
        ],
        "secrets": [{"name": "DATABASE_URL", "valueFrom": _SECRET}],
    }
    container.update(overrides)
    return container


def _stub_ecs(clients, containers=None, launch_type="FARGATE"):
    clients.add("ecs", "list_clusters", {"clusterArns": [_CLUSTER]})
    clients.add(
        "ecs",
        "list_services",
        {"serviceArns": [_SERVICE]},
        {"cluster": _CLUSTER},
    )
    clients.add(
        "ecs",
        "describe_services",
        {
            "services": [
                {
                    "serviceArn": _SERVICE,
                    "serviceName": "request-api",
                    "launchType": launch_type,
                    "taskDefinition": _TASK_DEF,
                    "loadBalancers": [
                        {
                            "targetGroupArn": _TARGET_GROUP,
                            "containerName": "app",
                            "containerPort": 8080,
                        }
                    ],
                }
            ]
        },
        {"cluster": _CLUSTER, "services": [_SERVICE]},
    )
    if launch_type != "FARGATE":
        return
    clients.add(
        "ecs",
        "describe_task_definition",
        {
            "taskDefinition": {
                "taskDefinitionArn": _TASK_DEF,
                "taskRoleArn": f"arn:aws:iam::{_ACCOUNT}:role/request-api",
                "containerDefinitions": containers or [_container()],
            }
        },
        {"taskDefinition": _TASK_DEF},
    )


def _stub_image_digest(clients):
    clients.add(
        "ecr",
        "describe_images",
        {"imageDetails": [{"imageDigest": _DIGEST}]},
        {
            "repositoryName": "request-api",
            "registryId": _ACCOUNT,
            "imageIds": [{"imageTag": "v1"}],
        },
    )


def _stub_resources(clients):
    clients.add(
        "rds",
        "describe_db_instances",
        {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "app",
                    "DBInstanceArn": _DB,
                    "Engine": "postgres",
                    "EngineVersion": "16.4",
                    "DBInstanceClass": "db.t4g.micro",
                    "IAMDatabaseAuthenticationEnabled": False,
                    "PubliclyAccessible": True,
                    "DBName": "shop",
                    "Endpoint": {"Address": "app.rds.internal", "Port": 5432},
                },
                {
                    "DBInstanceIdentifier": "legacy",
                    "DBInstanceArn": _DB + "-legacy",
                    "Engine": "mysql",
                },
            ]
        },
    )
    clients.add(
        "secretsmanager",
        "list_secrets",
        {"SecretList": [{"ARN": _SECRET, "Name": "app-db"}]},
    )
    clients.add(
        "ecr",
        "describe_repositories",
        {
            "repositories": [
                {
                    "repositoryArn": f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:"
                    "repository/request-api",
                    "repositoryName": "request-api",
                    "repositoryUri": _REPO,
                }
            ]
        },
    )
    clients.add(
        "elbv2",
        "describe_load_balancers",
        {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": "alb-arn",
                    "Type": "application",
                    "Scheme": "internet-facing",
                    "DNSName": "alb.example.com",
                },
                {"LoadBalancerArn": "nlb-arn", "Type": "network"},
            ]
        },
    )
    clients.add(
        "route53",
        "list_hosted_zones",
        {
            "HostedZones": [
                {
                    "Id": "/hostedzone/Z1",
                    "Name": "demo.internal.",
                    "CallerReference": "x",
                    "Config": {"PrivateZone": True},
                }
            ],
            "Marker": "",
            "IsTruncated": False,
            "MaxItems": "100",
        },
    )
    clients.add(
        "route53",
        "list_resource_record_sets",
        {
            "ResourceRecordSets": [
                {"Name": "demo.internal.", "Type": "SOA"},
                {
                    "Name": "app.demo.internal.",
                    "Type": "CNAME",
                    "SetIdentifier": "source",
                    "Weight": 100,
                    "TTL": 30,
                    "ResourceRecords": [{"Value": "alb.example.com"}],
                },
            ],
            "IsTruncated": False,
            "MaxItems": "300",
        },
        {"HostedZoneId": "/hostedzone/Z1"},
    )


def _read(clients):
    reader = aws.AwsInventoryReader(
        region=_REGION, client_factory=clients, clock=lambda: _NOW
    )
    inventory = reader.read()
    clients.assert_all_used()
    return inventory


@pytest.fixture
def first_slice():
    clients = _Clients()
    _stub_ecs(clients)
    _stub_image_digest(clients)
    _stub_resources(clients)
    return _read(clients)


def test_reads_fargate_service_as_workload(first_slice):
    (workload,) = first_slice.workloads

    assert workload.name == "request-api"
    assert workload.platform == "ecs"
    assert workload.native_id == _SERVICE
    assert workload.identity_id == f"arn:aws:iam::{_ACCOUNT}:role/request-api"
    assert workload.image == model.ImageRef(registry_repo=_REPO, digest=_DIGEST)
    assert workload.entries == (
        model.Entry(
            kind="http",
            source_id=_TARGET_GROUP,
            attributes=(model.KeyValue(key="container_port", value=8080),),
        ),
    )
    assert workload.secret_refs == (
        model.SecretRef(cloud="aws", native_id=_SECRET, version="AWSCURRENT"),
    )


def test_env_goes_through_classify_env(first_slice):
    (workload,) = first_slice.workloads

    assert [(e.key, e.classification, e.value) for e in workload.env] == [
        ("LOG_LEVEL", "plain_nonsecret", "info"),
        ("DB_PASSWORD", "suspected_secret", None),
        ("LEGACY_URL", "suspected_secret", None),
        ("OPAQUE", "suspected_secret", None),
        ("AUTH_HEADER", "unknown", None),
        ("DATABASE_URL", "secret_ref", None),
    ]


def test_reads_resources_by_kind(first_slice):
    by_kind = {}
    for resource in first_slice.resources:
        by_kind.setdefault(resource.kind, []).append(resource)

    (database,) = by_kind["postgres_instance"]
    attributes = {kv.key: kv.value for kv in database.attributes}
    assert database.native_id == _DB
    assert attributes["engine"] == "postgres"
    assert attributes["iam_auth"] is False
    assert attributes["port"] == 5432
    assert [r.native_id for r in by_kind["secret"]] == [_SECRET]
    assert [r.kind for r in by_kind["container_registry"]] == [
        "container_registry"
    ]
    (balancer,) = by_kind["load_balancer"]
    assert ("lb_type", "application") in [
        (kv.key, kv.value) for kv in balancer.attributes
    ]
    (record,) = by_kind["dns_record"]
    record_attributes = {kv.key: kv.value for kv in record.attributes}
    assert record_attributes["private_zone"] is True
    assert record_attributes["weight"] == 100


def test_unsupported_resources_become_omissions(first_slice):
    assert {o.subject for o in first_slice.omissions} == {
        f"rds:{_DB}-legacy",
        "elbv2:nlb-arn",
        # 작업 역할로 닿는 S3·SQS 등 대응표 밖 의존은 수집하지 않는다.
        f"identity_grants:arn:aws:iam::{_ACCOUNT}:role/request-api",
    }


def test_inventory_metadata(first_slice):
    assert first_slice.cloud == "aws"
    assert first_slice.collected_at == _NOW
    assert first_slice.attestation.suspected_values_retained == 0


def test_no_env_secret_value_anywhere_in_inventory_json(first_slice):
    document = json.dumps(
        inventory_json.inventory_to_json(first_slice), ensure_ascii=False
    )

    for canary in _CANARIES:
        assert canary not in document
    assert "canary" not in document


def test_ec2_launch_type_service_is_omitted():
    clients = _Clients()
    _stub_ecs(clients, launch_type="EC2")
    _stub_resources(clients)

    inventory = _read(clients)

    assert inventory.workloads == ()
    assert f"ecs:{_SERVICE}" in {o.subject for o in inventory.omissions}


def test_pinned_digest_image_needs_no_registry_lookup():
    clients = _Clients()
    _stub_ecs(clients, containers=[_container(image=f"{_REPO}@{_DIGEST}")])
    _stub_resources(clients)

    (workload,) = _read(clients).workloads

    assert workload.image == model.ImageRef(registry_repo=_REPO, digest=_DIGEST)


def test_non_ecr_tag_image_is_omitted():
    clients = _Clients()
    _stub_ecs(clients, containers=[_container(image="nginx:1.27")])
    _stub_resources(clients)

    inventory = _read(clients)

    assert inventory.workloads[0].image is None
    assert "image:request-api" in {o.subject for o in inventory.omissions}


def test_multi_container_task_is_omitted():
    clients = _Clients()
    _stub_ecs(clients, containers=[_container(), _container(name="sidecar")])
    _stub_resources(clients)

    inventory = _read(clients)

    assert inventory.workloads == ()
    assert f"ecs:{_SERVICE}" in {o.subject for o in inventory.omissions}


def test_access_denied_section_becomes_omission():
    clients = _Clients()
    _stub_ecs(clients)
    _stub_image_digest(clients)
    clients.add_error("rds", "describe_db_instances", "AccessDenied")
    clients.add("secretsmanager", "list_secrets", {"SecretList": []})
    clients.add("ecr", "describe_repositories", {"repositories": []})
    clients.add("elbv2", "describe_load_balancers", {"LoadBalancers": []})
    clients.add(
        "route53",
        "list_hosted_zones",
        {
            "HostedZones": [],
            "Marker": "",
            "IsTruncated": False,
            "MaxItems": "100",
        },
    )

    inventory = _read(clients)

    assert (
        model.Omission(
            subject="rds:*",
            detected_by="permission_denied",
            acknowledged_by=None,
        )
        in inventory.omissions
    )


def test_other_client_errors_propagate():
    clients = _Clients()
    clients.add_error("ecs", "list_clusters", "ThrottlingException")

    with pytest.raises(Exception, match="ThrottlingException"):
        aws.AwsInventoryReader(
            region=_REGION, client_factory=clients, clock=lambda: _NOW
        ).read()


def _omitted_subjects(container):
    clients = _Clients()
    _stub_ecs(clients, containers=[container])
    _stub_image_digest(clients)
    _stub_resources(clients)
    inventory = _read(clients)
    return inventory, {o.subject for o in inventory.omissions}


def test_environment_files_are_omitted():
    _, subjects = _omitted_subjects(
        _container(
            environmentFiles=[
                {"value": "arn:aws:s3:::cfg/app.env", "type": "s3"}
            ]
        )
    )

    assert "env_files:request-api" in subjects


def test_secret_json_key_selector_is_omitted_and_ref_points_to_secret():
    inventory, subjects = _omitted_subjects(
        _container(
            secrets=[
                {
                    "name": "DB_HOST",
                    "valueFrom": f"{_SECRET}:host:AWSPREVIOUS:",
                }
            ]
        )
    )

    assert "secret_selector:request-api/DB_HOST" in subjects
    assert inventory.workloads[0].secret_refs == (
        model.SecretRef(cloud="aws", native_id=_SECRET, version="AWSPREVIOUS"),
    )


def test_ssm_parameter_secret_is_omitted():
    inventory, subjects = _omitted_subjects(
        _container(
            secrets=[
                {
                    "name": "API_URL",
                    "valueFrom": f"arn:aws:ssm:{_REGION}:{_ACCOUNT}"
                    ":parameter/a",
                }
            ]
        )
    )

    assert "secret_source:request-api/API_URL" in subjects
    assert inventory.workloads[0].env[-1].classification == "secret_ref"
