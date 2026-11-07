import boto3
import pytest
from botocore.stub import Stubber

from crosscloud.provision import residue_aws

_PREFIX = "ccx-r1"
_ACCOUNT = "111122223333"
_REGION = "ap-northeast-2"
_SG_ARN = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:security-group/sg-1"
_RDS_ARN = f"arn:aws:rds:{_REGION}:{_ACCOUNT}:db:ccx-r1-db"
_LOG_ARN = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:/ecs/ccx-r1"
_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/ccx-r1-exec"
_SECRET_ARN = (
    f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:ccx-r1-db-url-AbCdEf"
)


def _client(service):
    return boto3.client(
        service,
        region_name=_REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


class _Clients:
    """Stubber 를 붙인 세 클라이언트. 기대 호출이 남으면 실패한다."""

    def __init__(self):
        self.tagging = _client("resourcegroupstaggingapi")
        self.iam = _client("iam")
        self.secrets = _client("secretsmanager")
        self.tag_stub = Stubber(self.tagging)
        self.iam_stub = Stubber(self.iam)
        self.secret_stub = Stubber(self.secrets)

    def scanner(self):
        return residue_aws.AwsResidueScanner(
            tagging=self.tagging,
            iam=self.iam,
            secrets=self.secrets,
        )

    def stubs(self):
        return (self.tag_stub, self.iam_stub, self.secret_stub)

    def __enter__(self):
        for stub in self.stubs():
            stub.activate()
        return self

    def __exit__(self, *exc):
        for stub in self.stubs():
            stub.assert_no_pending_responses()
            stub.deactivate()


def _expect_tagged(clients, *pages):
    tag_filters = [{"Key": "crosscloud-run", "Values": [_PREFIX]}]
    for index, page in enumerate(pages):
        params = {"TagFilters": tag_filters}
        if index > 0:
            params["PaginationToken"] = f"t{index}"
        response = {
            "ResourceTagMappingList": [{"ResourceARN": arn} for arn in page]
        }
        if index + 1 < len(pages):
            response["PaginationToken"] = f"t{index + 1}"
        clients.tag_stub.add_response("get_resources", response, params)


def _expect_roles(clients, *names):
    roles = [
        {
            "Path": "/",
            "RoleName": name,
            "RoleId": "AROAEXAMPLEEXAMPLE01",
            "Arn": f"arn:aws:iam::{_ACCOUNT}:role/{name}",
            "CreateDate": "2026-11-07T00:00:00Z",
        }
        for name in names
    ]
    clients.iam_stub.add_response(
        "list_roles", {"Roles": roles, "IsTruncated": False}, {}
    )


def _expect_secret_list(clients, *secrets):
    clients.secret_stub.add_response(
        "list_secrets",
        {"SecretList": [{"ARN": arn, "Name": name} for arn, name in secrets]},
        {
            "IncludePlannedDeletion": True,
            "Filters": [{"Key": "name", "Values": [_PREFIX]}],
        },
    )


def _expect_empty_queries(clients, skip=()):
    if "tagged" not in skip:
        _expect_tagged(clients, [])
    if "roles" not in skip:
        _expect_roles(clients)
    if "secrets" not in skip:
        _expect_secret_list(clients)


def test_scan_returns_empty_when_nothing_remains():
    with _Clients() as clients:
        _expect_empty_queries(clients)

        assert clients.scanner().scan(_PREFIX) == ()


def test_scan_reports_tagged_regional_resources_across_pages_by_arn():
    with _Clients() as clients:
        _expect_tagged(clients, [_SG_ARN, _RDS_ARN], [_LOG_ARN])
        _expect_empty_queries(clients, skip=("tagged",))

        residue = clients.scanner().scan(_PREFIX)

    assert residue == (
        residue_aws.ResidueItem(
            cloud="aws", kind="ec2:security-group", native_id=_SG_ARN
        ),
        residue_aws.ResidueItem(
            cloud="aws", kind="logs:log-group", native_id=_LOG_ARN
        ),
        residue_aws.ResidueItem(cloud="aws", kind="rds:db", native_id=_RDS_ARN),
    )


def test_scan_finds_iam_roles_by_name_prefix_only():
    with _Clients() as clients:
        _expect_roles(clients, "ccx-r1-exec", "ccx-r10-exec", "team-ccx-r1")
        _expect_empty_queries(clients, skip=("roles",))

        residue = clients.scanner().scan(_PREFIX)

    assert residue == (
        residue_aws.ResidueItem(
            cloud="aws", kind="iam:role", native_id=_ROLE_ARN
        ),
    )


def test_scan_rechecks_listed_secrets_by_id_and_keeps_planned_deletion():
    gone_arn = _SECRET_ARN.replace("db-url", "old")
    with _Clients() as clients:
        _expect_secret_list(
            clients,
            (_SECRET_ARN, "ccx-r1-db-url"),
            (gone_arn, "ccx-r1-old"),
        )
        clients.secret_stub.add_response(
            "describe_secret",
            {
                "ARN": _SECRET_ARN,
                "Name": "ccx-r1-db-url",
                "DeletedDate": "2026-11-07T00:00:00Z",
            },
            {"SecretId": _SECRET_ARN},
        )
        clients.secret_stub.add_client_error(
            "describe_secret",
            service_error_code="ResourceNotFoundException",
            expected_params={"SecretId": gone_arn},
        )
        _expect_empty_queries(clients, skip=("secrets",))

        residue = clients.scanner().scan(_PREFIX)

    assert residue == (
        residue_aws.ResidueItem(
            cloud="aws", kind="secretsmanager:secret", native_id=_SECRET_ARN
        ),
    )


def test_scan_skips_listed_secret_named_outside_the_prefix():
    with _Clients() as clients:
        _expect_secret_list(clients, (_SECRET_ARN, "ccx-r10-db-url"))
        _expect_empty_queries(clients, skip=("secrets",))

        assert clients.scanner().scan(_PREFIX) == ()


def test_scan_reports_secret_found_by_tag_and_name_queries_once():
    with _Clients() as clients:
        _expect_tagged(clients, [_SECRET_ARN])
        _expect_roles(clients)
        _expect_secret_list(clients, (_SECRET_ARN, "ccx-r1-db-url"))
        clients.secret_stub.add_response(
            "describe_secret",
            {"ARN": _SECRET_ARN, "Name": "ccx-r1-db-url"},
            {"SecretId": _SECRET_ARN},
        )

        residue = clients.scanner().scan(_PREFIX)

    assert len(residue) == 1


@pytest.mark.parametrize("prefix", ["", "ccx-", "ccx-R1", "team-ccx-r1"])
def test_scan_rejects_malformed_prefix_before_querying(prefix):
    scanner = residue_aws.AwsResidueScanner(
        tagging=None, iam=None, secrets=None
    )

    with pytest.raises(ValueError):
        scanner.scan(prefix)


def test_scan_raises_scan_error_when_a_query_fails():
    with _Clients() as clients:
        clients.tag_stub.add_client_error(
            "get_resources", service_error_code="ThrottlingException"
        )

        with pytest.raises(residue_aws.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_scan_raises_scan_error_when_secret_recheck_fails():
    with _Clients() as clients:
        _expect_secret_list(clients, (_SECRET_ARN, "ccx-r1-db-url"))
        clients.secret_stub.add_client_error(
            "describe_secret",
            service_error_code="AccessDeniedException",
            expected_params={"SecretId": _SECRET_ARN},
        )
        _expect_empty_queries(clients, skip=("secrets",))

        with pytest.raises(residue_aws.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_from_session_builds_scanner_without_calling_aws():
    session = boto3.Session(
        region_name=_REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )

    scanner = residue_aws.AwsResidueScanner.from_session(session)

    assert isinstance(scanner, residue_aws.AwsResidueScanner)
