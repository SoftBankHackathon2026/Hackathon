import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from migration.aws_target_check import __main__ as cli
from migration.aws_target_check.config import load
from migration.aws_target_check.runner import check, plan
from migration.wazuh_infra.config import ConfigError

botocore = pytest.importorskip("botocore.exceptions")
ClientError = botocore.ClientError
EndpointConnectionError = botocore.EndpointConnectionError
NoCredentialsError = botocore.NoCredentialsError

ACCOUNT = "000000000000"
INSTANCE = "arn:aws:sso:::instance/ssoins-0000000000000000"
EXAMPLE = Path(__file__).resolve().parents[1] / "config.aws-target-check.example.yaml"
READ_ONLY = ("describe_", "list_", "get_")

BASE = {
    "schema_version": 1,
    "account_id": ACCOUNT,
    "region": "ap-northeast-2",
    "registry": {
        "repositories": ["example/app"],
        "scan_on_push": True,
        "encryption": "KMS",
    },
    "secret_store": {"kms_key": "alias/example-key"},
    "identity": {"permission_sets": ["ExampleReader"]},
    "audit_log": {"multi_region": True},
    "data_target": {
        "rds_instance": "example-db",
        "approved_regions": ["ap-northeast-2"],
        "eks_cluster": "example-cluster",
    },
}


def denied(code="AccessDeniedException", operation="Call"):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)


class Fake:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __getattr__(self, method):
        if method not in self.responses:
            raise AttributeError(method)

        def call(**params):
            self.calls.append((method, params))
            response = self.responses[method]
            if callable(response):
                response = response(**params)
            if isinstance(response, Exception):
                raise response
            return copy.deepcopy(response)

        return call


class Session:
    def __init__(self, clients):
        self.clients = clients
        self.regions = {}

    def client(self, service, region_name=None, config=None):
        self.regions[service] = region_name
        return self.clients[service]


def healthy():
    return {
        "sts": Fake({"get_caller_identity": {"Account": ACCOUNT}}),
        "ecr": Fake(
            {
                "describe_registry": {"registryId": ACCOUNT},
                "get_authorization_token": {
                    "authorizationData": [
                        {
                            "authorizationToken": "QVdTOmZha2U=",
                            "expiresAt": datetime(2099, 1, 1, tzinfo=UTC),
                        }
                    ]
                },
                "get_registry_scanning_configuration": {
                    "scanningConfiguration": {"scanType": "BASIC", "rules": []}
                },
                "describe_repositories": {
                    "repositories": [
                        {
                            "repositoryName": "example/app",
                            "imageScanningConfiguration": {"scanOnPush": True},
                            "encryptionConfiguration": {"encryptionType": "KMS"},
                        }
                    ]
                },
            }
        ),
        "secretsmanager": Fake({"list_secrets": {"SecretList": []}}),
        "kms": Fake(
            {
                "describe_key": {
                    "KeyMetadata": {
                        "Enabled": True,
                        "KeyState": "Enabled",
                        "KeyUsage": "ENCRYPT_DECRYPT",
                        "KeySpec": "SYMMETRIC_DEFAULT",
                        "KeyManager": "CUSTOMER",
                    }
                }
            }
        ),
        "sso-admin": Fake(
            {
                "list_instances": {
                    "Instances": [{"InstanceArn": INSTANCE, "Status": "ACTIVE"}]
                },
                "list_permission_sets": {"PermissionSets": ["ps-1"]},
                "describe_permission_set": {"PermissionSet": {"Name": "ExampleReader"}},
            }
        ),
        "cloudtrail": Fake(
            {
                "describe_trails": {
                    "trailList": [{"Name": "example-trail", "IsMultiRegionTrail": True}]
                },
                "get_trail_status": {"IsLogging": True},
            }
        ),
        "rds": Fake(
            {
                "describe_db_instances": {
                    "DBInstances": [
                        {
                            "DBInstanceStatus": "available",
                            "PubliclyAccessible": False,
                            "DBInstanceArn": f"arn:aws:rds:ap-northeast-2:{ACCOUNT}:db:example-db",
                        }
                    ]
                }
            }
        ),
        "eks": Fake({"describe_cluster": {"cluster": {"status": "ACTIVE"}}}),
    }


@pytest.fixture
def write(tmp_path):
    def write(raw=None, **changes):
        data = copy.deepcopy(BASE if raw is None else raw)
        for name, value in changes.items():
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
        path = tmp_path.resolve() / "aws-target.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True))
        return str(path)

    return write


@pytest.fixture
def target(write):
    def target(clients=None, **changes):
        clients = clients or healthy()
        session = Session(clients)
        return check(load(write(**changes)), session), clients, session

    return target


def group(result, name):
    return next(g for g in result["groups"] if g["id"] == name)


def test_healthy_target_is_ready_with_single_json_and_exit_zero(write, capsys):
    clients = healthy()
    code = cli.main(["check", "--config", write()], Session(clients))
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and len(lines) == 1
    result = json.loads(lines[0])
    assert result["schema_version"] == 1 and result["operation"] == "check"
    assert result["status"] == "ready" and result["reason_codes"] == []
    assert [g["id"] for g in result["groups"]] == [
        "registry",
        "secret_store",
        "identity",
        "audit_log",
        "data_target",
    ]
    assert all(g["status"] == "pass" for g in result["groups"])
    assert "QVdTOmZha2U" not in lines[0]


def test_only_read_only_calls_are_made(target):
    _, clients, _ = target()
    calls = [method for client in clients.values() for method, _ in client.calls]
    assert calls and all(method.startswith(READ_ONLY) for method in calls)
    assert clients["secretsmanager"].calls == [("list_secrets", {"MaxResults": 1})]


def test_public_rds_fails_required_data_target(target):
    clients = healthy()
    clients["rds"].responses["describe_db_instances"]["DBInstances"][0][
        "PubliclyAccessible"
    ] = True
    data = {**BASE["data_target"], "required": True}
    result, _, _ = target(clients, data_target=data)
    assert result["status"] == "not_ready"
    assert group(result, "data_target")["status"] == "fail"
    assert result["reason_codes"] == ["RDS_PUBLICLY_ACCESSIBLE"]


def test_optional_check_failure_does_not_fail_overall(target):
    clients = healthy()
    clients["rds"].responses["describe_db_instances"]["DBInstances"][0][
        "PubliclyAccessible"
    ] = True
    data = {**BASE["data_target"], "required": False}
    result, _, _ = target(clients, data_target=data)
    assert result["status"] == "ready"
    assert group(result, "data_target")["status"] == "fail"
    assert result["reason_codes"] == []
    assert result["optional_reason_codes"] == ["RDS_PUBLICLY_ACCESSIBLE"]


def test_optional_core_group_can_be_relaxed(target):
    clients = healthy()
    clients["sso-admin"].responses["list_instances"] = {"Instances": []}
    result, _, _ = target(clients, identity={"required": False})
    assert result["status"] == "ready"
    assert result["optional_reason_codes"] == ["IDENTITY_CENTER_MISSING"]


@pytest.mark.parametrize(
    "metadata, code",
    [
        ({"Enabled": False, "KeyState": "Disabled"}, "KMS_KEY_NOT_ENABLED"),
        ({"Enabled": False, "KeyState": "PendingDeletion"}, "KMS_KEY_NOT_ENABLED"),
        ({"KeySpec": "RSA_2048"}, "KMS_KEY_NOT_SYMMETRIC"),
    ],
)
def test_unusable_kms_key_fails(target, metadata, code):
    clients = healthy()
    clients["kms"].responses["describe_key"]["KeyMetadata"].update(metadata)
    result, _, _ = target(clients)
    assert result["status"] == "not_ready"
    assert group(result, "secret_store")["status"] == "fail"
    assert result["reason_codes"] == [code]


def test_disabled_kms_key_reports_state_without_identifiers(target):
    clients = healthy()
    clients["kms"].responses["describe_key"]["KeyMetadata"].update(
        Enabled=False, KeyState="Disabled"
    )
    result, _, _ = target(clients)
    item = group(result, "secret_store")["items"][1]
    assert item["detail"] == {"key_state": "Disabled"}


def test_missing_kms_key_fails(target):
    clients = healthy()
    clients["kms"].responses["describe_key"] = denied("NotFoundException")
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["KMS_KEY_MISSING"]


def test_trail_not_logging_fails(target):
    clients = healthy()
    clients["cloudtrail"].responses["get_trail_status"] = {"IsLogging": False}
    result, _, _ = target(clients)
    assert result["status"] == "not_ready"
    assert result["reason_codes"] == ["CLOUDTRAIL_NOT_LOGGING"]
    assert group(result, "audit_log")["items"][0]["detail"] == {
        "trails": 1,
        "logging": 0,
    }


def test_single_region_trail_fails_only_when_multi_region_required(target):
    clients = healthy()
    trail = clients["cloudtrail"].responses["describe_trails"]["trailList"][0]
    trail["IsMultiRegionTrail"] = False
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["CLOUDTRAIL_NOT_MULTI_REGION"]
    result, _, _ = target(clients, audit_log={"multi_region": False})
    assert result["status"] == "ready"


def test_any_logging_trail_is_enough(target):
    clients = healthy()
    clients["cloudtrail"].responses["describe_trails"]["trailList"] = [
        {"TrailARN": "trail-a", "IsMultiRegionTrail": True},
        {"TrailARN": "trail-b", "IsMultiRegionTrail": True},
    ]
    clients["cloudtrail"].responses["get_trail_status"] = lambda Name: {
        "IsLogging": Name == "trail-b"
    }
    result, _, _ = target(clients)
    assert result["status"] == "ready"


def test_no_trail_fails(target):
    clients = healthy()
    clients["cloudtrail"].responses["describe_trails"] = {"trailList": []}
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["CLOUDTRAIL_TRAIL_MISSING"]


def test_access_denied_is_error_and_unverified(target):
    clients = healthy()
    clients["cloudtrail"].responses["describe_trails"] = denied()
    result, _, _ = target(clients)
    assert result["status"] == "unverified"
    assert group(result, "audit_log")["status"] == "error"
    assert result["reason_codes"] == ["AWS_ACCESS_DENIED"]


def test_fail_takes_precedence_over_error(target):
    clients = healthy()
    clients["cloudtrail"].responses["describe_trails"] = denied()
    clients["eks"].responses["describe_cluster"] = {"cluster": {"status": "CREATING"}}
    data = {**BASE["data_target"], "required": True}
    result, _, _ = target(clients, data_target=data)
    assert result["status"] == "not_ready"


def test_missing_repository_and_registry_account_mismatch(target):
    clients = healthy()
    clients["ecr"].responses["describe_repositories"] = denied(
        "RepositoryNotFoundException"
    )
    clients["ecr"].responses["describe_registry"] = {"registryId": "999999999999"}
    result, _, _ = target(clients)
    assert result["reason_codes"] == [
        "ECR_REGISTRY_ACCOUNT_MISMATCH",
        "ECR_REPOSITORY_MISSING",
    ]
    _, params = clients["ecr"].calls[-1]
    assert params == {"registryId": ACCOUNT, "repositoryNames": ["example/app"]}


def test_scan_on_push_from_registry_rule_and_enhanced_ignores_repository_flag(target):
    clients = healthy()
    repository = clients["ecr"].responses["describe_repositories"]["repositories"][0]
    repository["imageScanningConfiguration"]["scanOnPush"] = False
    clients["ecr"].responses["get_registry_scanning_configuration"] = {
        "scanningConfiguration": {
            "scanType": "ENHANCED",
            "rules": [
                {
                    "scanFrequency": "CONTINUOUS_SCAN",
                    "repositoryFilters": [
                        {"filter": "example/*", "filterType": "WILDCARD"}
                    ],
                }
            ],
        }
    }
    result, _, _ = target(clients)
    assert group(result, "registry")["status"] == "pass"
    repository["imageScanningConfiguration"]["scanOnPush"] = True
    clients["ecr"].responses["get_registry_scanning_configuration"] = {
        "scanningConfiguration": {"scanType": "ENHANCED", "rules": []}
    }
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["ECR_SCAN_ON_PUSH_DISABLED"]


def test_encryption_mismatch_and_empty_auth_token(target):
    clients = healthy()
    repository = clients["ecr"].responses["describe_repositories"]["repositories"][0]
    repository["encryptionConfiguration"]["encryptionType"] = "AES256"
    clients["ecr"].responses["get_authorization_token"] = {"authorizationData": []}
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["ECR_AUTH_TOKEN_EMPTY", "ECR_ENCRYPTION_MISMATCH"]
    mismatch = [
        i for i in group(result, "registry")["items"] if i["name"] == "ecr.encryption"
    ][0]
    assert mismatch["detail"] == {
        "repository": "example/app",
        "expected": "KMS",
        "actual": "AES256",
    }


def test_permission_sets_are_paginated_and_missing_names_fail(target):
    clients = healthy()
    pages = {
        None: {"PermissionSets": ["ps-1"], "NextToken": "t1"},
        "t1": {"PermissionSets": ["ps-2"]},
    }
    clients["sso-admin"].responses["list_permission_sets"] = lambda **p: pages[
        p.get("NextToken")
    ]
    clients["sso-admin"].responses["describe_permission_set"] = lambda **p: {
        "PermissionSet": {
            "Name": {"ps-1": "ExampleReader", "ps-2": "ExampleAdmin"}[
                p["PermissionSetArn"]
            ]
        }
    }
    identity = {"permission_sets": ["ExampleReader", "ExampleAdmin", "ExampleAudit"]}
    result, _, _ = target(clients, identity=identity)
    items = group(result, "identity")["items"]
    assert [i["status"] for i in items] == ["pass", "pass", "pass", "fail"]
    assert items[-1]["detail"] == {"permission_set": "ExampleAudit"}


def test_identity_center_region_override_and_instance_rules(target):
    clients = healthy()
    result, _, session = target(clients, identity={"region": "us-east-1"})
    assert session.regions["sso-admin"] == "us-east-1"
    clients["sso-admin"].responses["list_instances"] = {
        "Instances": [{"InstanceArn": INSTANCE}, {"InstanceArn": INSTANCE + "1"}]
    }
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["IDENTITY_CENTER_INSTANCE_AMBIGUOUS"]
    result, _, _ = target(clients, identity={"instance_arn": INSTANCE})
    assert result["status"] == "ready"
    clients["sso-admin"].responses["list_instances"] = {
        "Instances": [{"InstanceArn": INSTANCE, "Status": "DELETE_IN_PROGRESS"}]
    }
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["IDENTITY_CENTER_NOT_ACTIVE"]


def test_rds_state_region_and_eks_status(target):
    clients = healthy()
    instance = clients["rds"].responses["describe_db_instances"]["DBInstances"][0]
    instance["DBInstanceStatus"] = "stopped"
    instance["DBInstanceArn"] = f"arn:aws:rds:us-west-2:{ACCOUNT}:db:example-db"
    clients["eks"].responses["describe_cluster"] = {"cluster": {"status": "FAILED"}}
    result, _, _ = target(clients)
    assert result["status"] == "not_ready"
    assert result["reason_codes"] == [
        "EKS_CLUSTER_NOT_ACTIVE",
        "RDS_INSTANCE_NOT_AVAILABLE",
        "RDS_REGION_NOT_APPROVED",
    ]
    clients["rds"].responses["describe_db_instances"] = denied("DBInstanceNotFound")
    clients["eks"].responses["describe_cluster"] = denied("ResourceNotFoundException")
    result, _, _ = target(clients)
    assert result["reason_codes"] == ["EKS_CLUSTER_MISSING", "RDS_INSTANCE_MISSING"]


def test_data_target_is_skipped_when_not_configured(target):
    clients = healthy()
    result, _, _ = target(clients, data_target=None)
    assert result["status"] == "ready"
    assert "data_target" not in [g["id"] for g in result["groups"]]
    assert clients["rds"].calls == [] and clients["eks"].calls == []


def test_account_mismatch_blocks_before_checks(write, capsys):
    clients = healthy()
    clients["sts"].responses["get_caller_identity"] = {"Account": "999999999999"}
    code = cli.main(["check", "--config", write()], Session(clients))
    result = json.loads(capsys.readouterr().out)
    assert code == 3
    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["AWS_ACCOUNT_MISMATCH"]
    assert clients["ecr"].calls == []


@pytest.mark.parametrize(
    "error, code",
    [
        (NoCredentialsError(), "AWS_CREDENTIALS_UNAVAILABLE"),
        (EndpointConnectionError(endpoint_url="https://sts"), "AWS_UNREACHABLE"),
        (denied("ExpiredToken"), "AWS_CREDENTIALS_INVALID"),
    ],
)
def test_credential_and_network_errors_block(target, error, code):
    clients = healthy()
    clients["sts"].responses["get_caller_identity"] = error
    result, _, _ = target(clients)
    assert result == {"status": "blocked", "reason_codes": [code]}


def test_plan_is_offline_and_lists_iam_actions(write, capsys):
    class Offline:
        def client(self, *args, **kwargs):
            raise AssertionError("plan must not contact AWS")

    code = cli.main(["plan", "--config", write()], Offline())
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["status"] == "planned"
    assert result["actions"] == sorted(
        [
            "cloudtrail:DescribeTrails",
            "cloudtrail:GetTrailStatus",
            "ecr:DescribeRegistry",
            "ecr:DescribeRepositories",
            "ecr:GetAuthorizationToken",
            "ecr:GetRegistryScanningConfiguration",
            "eks:DescribeCluster",
            "kms:DescribeKey",
            "rds:DescribeDBInstances",
            "secretsmanager:ListSecrets",
            "sso:DescribePermissionSet",
            "sso:ListInstances",
            "sso:ListPermissionSets",
            "sts:GetCallerIdentity",
        ]
    )
    assert all(
        a.split(":")[1].startswith(("Describe", "List", "Get"))
        for a in result["actions"]
    )


def test_plan_omits_actions_for_unused_options(write):
    raw = copy.deepcopy(BASE)
    raw["registry"] = {}
    raw["identity"] = {}
    raw.pop("data_target")
    result = plan(load(write(raw)))
    assert result["actions"] == [
        "cloudtrail:DescribeTrails",
        "cloudtrail:GetTrailStatus",
        "ecr:DescribeRegistry",
        "ecr:GetAuthorizationToken",
        "kms:DescribeKey",
        "secretsmanager:ListSecrets",
        "sso:ListInstances",
        "sts:GetCallerIdentity",
    ]


@pytest.mark.parametrize(
    "changes, field",
    [
        ({"aws_access_key_id": "x"}, "config"),
        ({"account_id": "123"}, "account_id"),
        ({"audit_log": None}, "audit_log"),
        ({"secret_store": {}}, "secret_store.kms_key"),
        ({"registry": {"scan_on_push": True}}, "registry.repositories"),
        ({"registry": {"repositories": ["Bad Name"]}}, "registry.repositories"),
        ({"registry": {"encryption": "NONE", "repositories": ["a1"]}}, "encryption"),
        ({"identity": {"required": "yes"}}, "identity.required"),
        ({"data_target": {"required": False}}, "data_target"),
        ({"data_target": {"eks_cluster": "c", "approved_regions": ["x"]}}, "approved"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_config_rejects_invalid_input(write, changes, field):
    with pytest.raises(ConfigError, match=field):
        load(write(**changes))


def test_invalid_config_reports_field_not_value(write, capsys):
    code = cli.main(["check", "--config", write(profile="bad profile!")])
    output = capsys.readouterr().out
    result = json.loads(output)
    assert code == 3 and result["reason_codes"] == ["INVALID_CONFIG"]
    assert "bad profile" not in output


def test_unexpected_exception_is_redacted(write, capsys):
    class Broken:
        def client(self, *args, **kwargs):
            raise RuntimeError("secret detail")

    code = cli.main(["check", "--config", write()], Broken())
    output = capsys.readouterr().out
    assert code == 3 and json.loads(output)["reason_codes"] == ["EXECUTION_FAILED"]
    assert "secret detail" not in output


def test_example_config_uses_placeholders_and_matches_schema(write):
    text = EXAMPLE.read_text()
    assert "<계정 ID>" in text and "<RDS 이름>" in text
    with pytest.raises(ConfigError):
        load(str(EXAMPLE))
    values = {
        "<계정 ID>": ACCOUNT,
        "<리전>": "ap-northeast-2",
        "<Identity Center 리전>": "ap-northeast-2",
        "<프로필 이름>": "example",
        "<저장소 이름>": "example/app",
        "<KMS 키 별칭>": "example-key",
        "<권한 세트 이름>": "ExampleReader",
        "<RDS 이름>": "example-db",
        "<EKS 클러스터 이름>": "example-cluster",
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    assert "<" not in text
    config = load(write(yaml.safe_load(text)))
    assert config["data_target"]["required"] is False
    assert config["registry"]["required"] is True
