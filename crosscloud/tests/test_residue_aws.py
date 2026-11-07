import dataclasses

import boto3
import pytest
from botocore import exceptions
from botocore.stub import Stubber

from crosscloud import model
from crosscloud.provision import residue_aws

_RUN_LABELS = (model.KeyValue(key="crosscloud-run", value="r1"),)
_PREFIX = "ccx-r1"
_ZONE_ARN = "arn:aws:route53:::hostedzone/Z0123"
_ROLE_ARN = "arn:aws:iam::111122223333:role/ccx-r1/ccx-r1-exec"
_SG_ARN = "arn:aws:ec2:ap-northeast-2:111122223333:security-group/sg-1"
_RDS_ARN = "arn:aws:rds:ap-northeast-2:111122223333:db:ccx-r1-db"


@dataclasses.dataclass(frozen=True)
class _Selector:
    labels: tuple[model.KeyValue, ...]
    name_prefix: str


def _client(service):
    return boto3.client(
        service,
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


class _Clients:
    """Stubber 를 붙인 세 클라이언트. 기대 호출을 다 쓰지 않으면 실패한다."""

    def __init__(self):
        self.tagging = _client("resourcegroupstaggingapi")
        self.iam = _client("iam")
        self.route53 = _client("route53")
        self.stubs = [
            Stubber(self.tagging),
            Stubber(self.iam),
            Stubber(self.route53),
        ]

    def scanner(self):
        return residue_aws.AwsResidueScanner(
            tagging=self.tagging, iam=self.iam, route53=self.route53
        )

    def __enter__(self):
        for stub in self.stubs:
            stub.activate()
        return self

    def __exit__(self, *exc):
        for stub in self.stubs:
            stub.assert_no_pending_responses()
            stub.deactivate()


def _expect_tag_query(clients, *pages):
    tag_filters = [{"Key": "crosscloud-run", "Values": ["r1"]}]
    for index, page in enumerate(pages):
        params = {"TagFilters": tag_filters}
        if index > 0:
            params["PaginationToken"] = f"t{index}"
        response = {"ResourceTagMappingList": page}
        if index + 1 < len(pages):
            response["PaginationToken"] = f"t{index + 1}"
        clients.stubs[0].add_response("get_resources", response, params)


def _expect_roles(clients, roles):
    clients.stubs[1].add_response(
        "list_roles",
        {"Roles": roles, "IsTruncated": False},
        {"PathPrefix": "/ccx-r1/"},
    )


def _expect_zones(clients, zones):
    clients.stubs[2].add_response(
        "list_hosted_zones",
        {
            "HostedZones": zones,
            "IsTruncated": False,
            "Marker": "",
            "MaxItems": "100",
        },
        {},
    )


def _role(name, arn):
    return {
        "Path": "/ccx-r1/",
        "RoleName": name,
        "RoleId": "AROAEXAMPLEEXAMPLE01",
        "Arn": arn,
        "CreateDate": "2026-11-07T00:00:00Z",
    }


def _zone(zone_id, name):
    return {
        "Id": f"/hostedzone/{zone_id}",
        "Name": name,
        "CallerReference": "x",
    }


def test_scan_returns_empty_when_nothing_remains():
    with _Clients() as clients:
        _expect_tag_query(clients, [])
        _expect_roles(clients, [])
        _expect_zones(clients, [_zone("Z9", "other.internal.")])

        residue = clients.scanner().scan(_Selector(_RUN_LABELS, _PREFIX))

    assert residue == ()


def test_scan_reports_tagged_resources_across_pages_with_their_tags():
    with _Clients() as clients:
        _expect_tag_query(
            clients,
            [
                {
                    "ResourceARN": _SG_ARN,
                    "Tags": [{"Key": "crosscloud-run", "Value": "r1"}],
                }
            ],
            [{"ResourceARN": _RDS_ARN, "Tags": []}],
        )
        _expect_roles(clients, [])
        _expect_zones(clients, [])

        residue = clients.scanner().scan(_Selector(_RUN_LABELS, _PREFIX))

    assert residue == (
        residue_aws.ResidueItem(
            cloud="aws",
            native_id=_SG_ARN,
            labels=(model.KeyValue(key="crosscloud-run", value="r1"),),
        ),
        residue_aws.ResidueItem(cloud="aws", native_id=_RDS_ARN, labels=()),
    )


def test_scan_reports_global_iam_roles_under_the_prefix_path():
    with _Clients() as clients:
        _expect_tag_query(clients, [])
        _expect_roles(clients, [_role("ccx-r1-exec", _ROLE_ARN)])
        _expect_zones(clients, [])

        residue = clients.scanner().scan(_Selector(_RUN_LABELS, _PREFIX))

    assert residue == (
        residue_aws.ResidueItem(cloud="aws", native_id=_ROLE_ARN, labels=()),
    )


def test_scan_reports_prefixed_zone_once_keeping_tags_from_tag_query():
    with _Clients() as clients:
        _expect_tag_query(
            clients,
            [
                {
                    "ResourceARN": _ZONE_ARN,
                    "Tags": [{"Key": "crosscloud-run", "Value": "r1"}],
                }
            ],
        )
        _expect_roles(clients, [])
        _expect_zones(
            clients,
            [
                _zone("Z0123", "ccx-r1.internal."),
                _zone("Z0456", "ccx-r10.internal."),
            ],
        )

        residue = clients.scanner().scan(_Selector(_RUN_LABELS, _PREFIX))

    assert residue == (
        residue_aws.ResidueItem(
            cloud="aws", native_id=_ZONE_ARN, labels=_RUN_LABELS
        ),
    )


@pytest.mark.parametrize(
    ("labels", "prefix"), [((), _PREFIX), (_RUN_LABELS, ""), (_RUN_LABELS, " ")]
)
def test_scan_rejects_selector_that_would_match_everything(labels, prefix):
    scanner = residue_aws.AwsResidueScanner(
        tagging=None, iam=None, route53=None
    )

    with pytest.raises(ValueError):
        scanner.scan(_Selector(labels, prefix))


def test_scan_raises_when_a_query_fails_instead_of_reporting_zero():
    with _Clients() as clients:
        clients.stubs[0].add_client_error(
            "get_resources", service_error_code="ThrottlingException"
        )

        with pytest.raises(exceptions.ClientError):
            clients.scanner().scan(_Selector(_RUN_LABELS, _PREFIX))


def test_from_session_builds_scanner_without_calling_aws():
    session = boto3.Session(
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )

    scanner = residue_aws.AwsResidueScanner.from_session(session)

    assert isinstance(scanner, residue_aws.AwsResidueScanner)
