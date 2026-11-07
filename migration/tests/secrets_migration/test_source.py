import pytest

boto3 = pytest.importorskip(
    "boto3", reason="install migration[secrets] for AWS SDK tests"
)
from botocore.stub import Stubber
from migration.secrets.common import SecretsError
from migration.secrets.source import AwsSource

from .conftest import ACCOUNT, ARN

V1, V2 = "1" * 32, "2" * 32


@pytest.fixture
def clients():
    def client(name):
        return boto3.client(
            name,
            region_name="ap-northeast-2",
            aws_access_key_id="test-access",
            aws_secret_access_key="test-secret",
        )

    return [client(name) for name in ("secretsmanager", "sts", "kms")]


def inventory_calls(sm, sts, *, key=None, policy=None):
    sts.add_response(
        "get_caller_identity",
        {
            "Account": ACCOUNT,
            "Arn": "arn:aws:iam::" + ACCOUNT + ":user/test",
            "UserId": "TEST",
        },
        {},
    )
    sm.add_response(
        "list_secrets",
        {"SecretList": [{"ARN": ARN, "Name": "orders"}], "NextToken": "next"},
        {"IncludePlannedDeletion": True},
    )
    sm.add_response(
        "list_secrets",
        {"SecretList": []},
        {"IncludePlannedDeletion": True, "NextToken": "next"},
    )
    detail = {
        "ARN": ARN,
        "Name": "orders",
        "Tags": [{"Key": "team", "Value": "orders"}],
    }
    if key:
        detail["KmsKeyId"] = key
    sm.add_response("describe_secret", detail, {"SecretId": ARN})
    sm.add_response(
        "list_secret_version_ids",
        {
            "Versions": [{"VersionId": V2, "VersionStages": ["AWSCURRENT"]}],
            "NextToken": "older",
        },
        {"SecretId": ARN, "IncludeDeprecated": True},
    )
    sm.add_response(
        "list_secret_version_ids",
        {"Versions": [{"VersionId": V1, "VersionStages": ["AWSPREVIOUS"]}]},
        {"SecretId": ARN, "IncludeDeprecated": True, "NextToken": "older"},
    )
    sm.add_response(
        "get_resource_policy",
        {"ARN": ARN, **({"ResourcePolicy": policy} if policy else {})},
        {"SecretId": ARN},
    )


def test_real_aws_sdk_pagination_uses_only_read_calls_and_captures_policy(clients):
    sm, sts, kms = clients
    source = AwsSource(
        {
            "account_id": ACCOUNT,
            "region": "ap-northeast-2",
            "include_names": ["orders"],
        },
        sm=sm,
        sts=sts,
        kms=kms,
    )
    policy = '{"Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::111122223333:role/reader"},"Action":"secretsmanager:GetSecretValue","Resource":"*","Condition":{"StringEquals":{"example":"preserved"}}}]}'
    with Stubber(sm) as secrets_stub, Stubber(sts) as sts_stub:
        inventory_calls(secrets_stub, sts_stub, policy=policy)
        inventory = source.inventory()
        assert inventory["resources"][0]["resource_policy"]["Statement"][0][
            "Condition"
        ] == {"StringEquals": {"example": "preserved"}}
        assert len(inventory["resources"][0]["versions"]) == 2
        secrets_stub.assert_no_pending_responses()
        sts_stub.assert_no_pending_responses()


@pytest.mark.parametrize("binary", [False, True])
def test_secret_bytes_and_version_are_preserved_by_actual_boto_rules(clients, binary):
    sm, sts, kms = clients
    source = AwsSource(
        {"account_id": ACCOUNT, "region": "ap-northeast-2"}, sm=sm, sts=sts, kms=kms
    )
    payload = b"\x00\xffbinary" if binary else "  password文字列\n"
    with Stubber(sm) as secret_stub, Stubber(sts) as sts_stub:
        sts_stub.add_response("get_caller_identity", {"Account": ACCOUNT}, {})
        secret_stub.add_response(
            "get_secret_value",
            {
                "ARN": ARN,
                "Name": "orders",
                "VersionId": V2,
                "SecretBinary" if binary else "SecretString": payload,
            },
            {"SecretId": ARN, "VersionId": V2},
        )
        assert source.read(ARN, V2).data == (payload if binary else payload.encode())


def test_unknown_kms_evidence_is_reported_not_assumed(clients):
    sm, sts, kms = clients
    source = AwsSource(
        {
            "account_id": ACCOUNT,
            "region": "ap-northeast-2",
            "include_names": ["orders"],
        },
        sm=sm,
        sts=sts,
        kms=kms,
    )
    with Stubber(sm) as secret_stub, Stubber(sts) as sts_stub, Stubber(kms) as kms_stub:
        inventory_calls(secret_stub, sts_stub, key="alias/example")
        kms_stub.add_client_error(
            "describe_key",
            service_error_code="AccessDeniedException",
            service_message="PRIVATE-REMOTE-ERROR",
            expected_params={"KeyId": "alias/example"},
        )
        result = source.inventory()
        assert result["resources"][0]["kms"]["coverage"] == "unknown"
        assert "PRIVATE" not in str(result)


def test_source_account_mismatch_and_raw_exception_are_blocked(clients):
    sm, sts, kms = clients
    source = AwsSource(
        {"account_id": ACCOUNT, "region": "ap-northeast-2"}, sm=sm, sts=sts, kms=kms
    )
    with Stubber(sts) as stub:
        stub.add_response("get_caller_identity", {"Account": "999988887777"}, {})
        with pytest.raises(SecretsError, match="ACCOUNT_MISMATCH"):
            source.identity()
    with Stubber(sts) as stub:
        stub.add_client_error(
            "get_caller_identity",
            service_error_code="AccessDenied",
            service_message="PRIVATE-TOKEN-CANARY",
        )
        with pytest.raises(SecretsError, match="SOURCE_READ_FAILED") as error:
            source.identity()
        assert "PRIVATE" not in str(error.value)


def test_repeating_page_and_recreated_secret_are_not_trusted(clients):
    sm, sts, kms = clients
    source = AwsSource(
        {
            "account_id": ACCOUNT,
            "region": "ap-northeast-2",
            "include_names": ["orders"],
        },
        sm=sm,
        sts=sts,
        kms=kms,
    )
    with Stubber(sm) as stub:
        stub.add_response("list_secrets", {"SecretList": [], "NextToken": "loop"})
        stub.add_response("list_secrets", {"SecretList": [], "NextToken": "loop"})
        with pytest.raises(SecretsError, match="PAGINATION_LOOP"):
            list(source.pages("list_secrets", "SecretList"))
    with pytest.raises(SecretsError, match="IDENTITY_MISMATCH"):
        source.arn(ARN.replace(ACCOUNT, "999988887777"))


@pytest.mark.parametrize("failure", ["listing", "metadata"])
def test_inventory_retains_unknown_scope_without_remote_error(clients, failure):
    sm, sts, kms = clients
    source = AwsSource(
        {
            "account_id": ACCOUNT,
            "region": "ap-northeast-2",
            "include_names": ["orders"],
        },
        sm=sm,
        sts=sts,
        kms=kms,
    )
    with Stubber(sm) as sm_stub, Stubber(sts) as sts_stub:
        sts_stub.add_response("get_caller_identity", {"Account": ACCOUNT}, {})
        if failure == "listing":
            sm_stub.add_client_error(
                "list_secrets",
                service_error_code="AccessDeniedException",
                service_message="PRIVATE-REMOTE-TOKEN",
                expected_params={"IncludePlannedDeletion": True},
            )
        else:
            sm_stub.add_response(
                "list_secrets",
                {"SecretList": [{"ARN": ARN, "Name": "orders"}]},
                {"IncludePlannedDeletion": True},
            )
            sm_stub.add_client_error(
                "describe_secret",
                service_error_code="AccessDeniedException",
                service_message="PRIVATE-REMOTE-TOKEN",
                expected_params={"SecretId": ARN},
            )
        inventory = source.inventory()
        assert inventory["status"] == "partial" and inventory["resources"] == []
        assert any(c["status"] == "unknown" for c in inventory["coverage"])
        assert "PRIVATE" not in str(inventory)
