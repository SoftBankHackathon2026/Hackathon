import traceback

import boto3
import pytest
from botocore import exceptions as botocore_exceptions
from botocore import stub

from crosscloud.secret_transfer import store, store_aws

_CANARY = "canary-aws-41c2-should-not-leak"
_ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:x-AbCdEf"
_TOKEN = "3f1d2c4b-0000-5000-8000-000000000001"
_OLD_CURRENT = "11111111-0000-4000-8000-000000000001"
_OLD_PREVIOUS = "22222222-0000-4000-8000-000000000002"


@pytest.fixture
def client():
    secrets = boto3.client(
        "secretsmanager",
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    with stub.Stubber(secrets) as stubber:
        yield secrets, stubber
        stubber.assert_no_pending_responses()


def test_read_current_returns_string_as_utf8_bytes(client):
    secrets, stubber = client
    stubber.add_response(
        "get_secret_value",
        {"SecretString": _CANARY},
        {"SecretId": "app/db-url", "VersionStage": "AWSCURRENT"},
    )

    value = store_aws.AwsSecretStore(secrets).read_current("app/db-url")

    assert value == _CANARY.encode()


def test_read_current_returns_binary_as_is(client):
    secrets, stubber = client
    stubber.add_response(
        "get_secret_value",
        {"SecretBinary": b"\x00\xffraw"},
        {"SecretId": "app/key", "VersionStage": "AWSCURRENT"},
    )

    value = store_aws.AwsSecretStore(secrets).read_current("app/key")

    assert value == b"\x00\xffraw"


def test_read_current_returns_none_without_current_version(client):
    secrets, stubber = client
    stubber.add_client_error(
        "get_secret_value", service_error_code="ResourceNotFoundException"
    )

    assert store_aws.AwsSecretStore(secrets).read_current("empty") is None


def test_read_version_reads_by_version_id(client):
    secrets, stubber = client
    stubber.add_response(
        "get_secret_value",
        {"SecretString": "v"},
        {"SecretId": "app/db-url", "VersionId": _TOKEN},
    )

    value = store_aws.AwsSecretStore(secrets).read_version("app/db-url", _TOKEN)

    assert value == b"v"


def test_add_version_records_stage_map_and_uses_request_token(client):
    secrets, stubber = client
    stubber.add_response(
        "describe_secret",
        {
            "ARN": _ARN,
            "VersionIdsToStages": {
                _OLD_CURRENT: ["AWSCURRENT"],
                _OLD_PREVIOUS: ["AWSPREVIOUS"],
            },
        },
        {"SecretId": "tgt/db-url"},
    )
    stubber.add_response(
        "put_secret_value",
        {"ARN": _ARN, "VersionId": _TOKEN},
        {
            "SecretId": "tgt/db-url",
            "ClientRequestToken": _TOKEN,
            "SecretString": _CANARY,
        },
    )

    written = store_aws.AwsSecretStore(secrets).add_version(
        "tgt/db-url", _CANARY.encode(), _TOKEN
    )

    assert written == store.WrittenVersion(
        secret_id="tgt/db-url",
        version_id=_TOKEN,
        created=True,
        prior_labels=(
            ("AWSCURRENT", _OLD_CURRENT),
            ("AWSPREVIOUS", _OLD_PREVIOUS),
        ),
    )


def test_add_version_writes_non_utf8_value_as_binary(client):
    secrets, stubber = client
    stubber.add_response(
        "describe_secret", {"ARN": _ARN}, {"SecretId": "tgt/key"}
    )
    stubber.add_response(
        "put_secret_value",
        {"ARN": _ARN, "VersionId": _TOKEN},
        {
            "SecretId": "tgt/key",
            "ClientRequestToken": _TOKEN,
            "SecretBinary": b"\xff\xfe",
        },
    )

    written = store_aws.AwsSecretStore(secrets).add_version(
        "tgt/key", b"\xff\xfe", _TOKEN
    )

    assert written.prior_labels == ()


def test_add_version_retry_does_not_write_again(client):
    secrets, stubber = client
    stubber.add_response(
        "describe_secret",
        {
            "ARN": _ARN,
            "VersionIdsToStages": {
                _TOKEN: ["AWSCURRENT"],
                _OLD_CURRENT: ["AWSPREVIOUS"],
            },
        },
        {"SecretId": "tgt/db-url"},
    )

    written = store_aws.AwsSecretStore(secrets).add_version(
        "tgt/db-url", b"v", _TOKEN
    )

    assert written.created is True
    assert written.prior_labels == (("AWSCURRENT", _OLD_CURRENT),)


def test_retire_moves_labels_back_to_prior_versions(client):
    secrets, stubber = client
    stubber.add_response(
        "update_secret_version_stage",
        {"ARN": _ARN},
        {
            "SecretId": "tgt/db-url",
            "VersionStage": "AWSCURRENT",
            "MoveToVersionId": _OLD_CURRENT,
            "RemoveFromVersionId": _TOKEN,
        },
    )
    stubber.add_response(
        "update_secret_version_stage",
        {"ARN": _ARN},
        {
            "SecretId": "tgt/db-url",
            "VersionStage": "AWSPREVIOUS",
            "MoveToVersionId": _OLD_PREVIOUS,
            "RemoveFromVersionId": _TOKEN,
        },
    )
    written = store.WrittenVersion(
        secret_id="tgt/db-url",
        version_id=_TOKEN,
        created=True,
        prior_labels=(
            ("AWSCURRENT", _OLD_CURRENT),
            ("AWSPREVIOUS", _OLD_PREVIOUS),
        ),
    )

    outcome = store_aws.AwsSecretStore(secrets).retire_version(written)

    assert outcome == "retired"


def test_retire_without_prior_previous_strips_previous_label(client):
    secrets, stubber = client
    stubber.add_response(
        "update_secret_version_stage",
        {"ARN": _ARN},
        {
            "SecretId": "tgt/db-url",
            "VersionStage": "AWSCURRENT",
            "MoveToVersionId": _OLD_CURRENT,
            "RemoveFromVersionId": _TOKEN,
        },
    )
    stubber.add_response(
        "update_secret_version_stage",
        {"ARN": _ARN},
        {
            "SecretId": "tgt/db-url",
            "VersionStage": "AWSPREVIOUS",
            "RemoveFromVersionId": _TOKEN,
        },
    )
    written = store.WrittenVersion(
        secret_id="tgt/db-url",
        version_id=_TOKEN,
        created=True,
        prior_labels=(("AWSCURRENT", _OLD_CURRENT),),
    )

    assert store_aws.AwsSecretStore(secrets).retire_version(written) == (
        "retired"
    )


def test_retire_first_version_leaves_it_current(client):
    secrets, _ = client
    written = store.WrittenVersion(
        secret_id="tgt/db-url", version_id=_TOKEN, created=True, prior_labels=()
    )

    outcome = store_aws.AwsSecretStore(secrets).retire_version(written)

    assert outcome == "left_current"


def test_retire_keeps_version_not_created_by_this_run(client):
    secrets, _ = client
    written = store.WrittenVersion(
        secret_id="tgt/db-url",
        version_id=_TOKEN,
        created=False,
        prior_labels=(),
    )

    assert store_aws.AwsSecretStore(secrets).retire_version(written) == "kept"


def test_client_errors_become_store_error_without_value(client):
    secrets, stubber = client
    stubber.add_response(
        "describe_secret", {"ARN": _ARN}, {"SecretId": "tgt/db-url"}
    )
    stubber.add_client_error(
        "put_secret_value",
        service_error_code="AccessDeniedException",
        service_message="denied",
    )

    with pytest.raises(store.SecretStoreError) as excinfo:
        store_aws.AwsSecretStore(secrets).add_version(
            "tgt/db-url", _CANARY.encode(), _TOKEN
        )

    assert excinfo.value.reason == "AccessDeniedException"
    assert excinfo.value.__cause__ is None
    assert _CANARY not in "".join(traceback.format_exception(excinfo.value))


class _ValidationFailingClient:
    """botocore 처럼 인자 검사 오류 메시지에 요청 값을 담는 클라이언트."""

    def describe_secret(self, **kwargs):
        return {}

    def put_secret_value(self, **kwargs):
        raise botocore_exceptions.ParamValidationError(
            report=f"Invalid type, value: {kwargs['SecretString']}"
        )


def test_parameter_validation_error_does_not_leak_value():
    secrets = store_aws.AwsSecretStore(_ValidationFailingClient())

    with pytest.raises(store.SecretStoreError) as excinfo:
        secrets.add_version("tgt/db-url", _CANARY.encode(), _TOKEN)

    assert excinfo.value.reason == "ParamValidationError"
    assert _CANARY not in "".join(traceback.format_exception(excinfo.value))
