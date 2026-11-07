import boto3
import pytest
from botocore import exceptions
from botocore.stub import Stubber

from crosscloud.provision import residue, residue_aws

_PREFIX = "ccx-r1"
_OTHER = "ccx-r10"
_ACCOUNT = "111122223333"
_REGION = "ap-northeast-2"
_SERVICES = (
    "ec2",
    "rds",
    "secretsmanager",
    "ecr",
    "ecs",
    "elbv2",
    "iam",
    "logs",
)
_TAG_FILTER = {"Filters": [{"Name": "tag:crosscloud-run", "Values": [_PREFIX]}]}


def _arn(service, resource):
    return f"arn:aws:{service}:{_REGION}:{_ACCOUNT}:{resource}"


def _client(service, region):
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


_CLUSTER_ARN = _arn("ecs", f"cluster/{_PREFIX}")
_LB_ARN = _arn("elasticloadbalancing", f"loadbalancer/app/{_PREFIX}-alb/1")
_SECRET_ARN = _arn("secretsmanager", "secret:ccx-r1-database-url-AbCdEf")

# 클라이언트별 호출 순서. 스캐너는 같은 클라이언트 안에서 이 순서로 부른다.
# (서비스, 작업, 기대 인자, 빈 응답). 빈 응답이 None 인 작업은 앞 작업의
# 결과가 있을 때만 불리므로 기본으로는 응답을 넣지 않는다.
_CALLS = (
    ("ec2", "describe_vpcs", _TAG_FILTER, {"Vpcs": []}),
    ("ec2", "describe_subnets", _TAG_FILTER, {"Subnets": []}),
    (
        "ec2",
        "describe_internet_gateways",
        _TAG_FILTER,
        {"InternetGateways": []},
    ),
    ("ec2", "describe_route_tables", _TAG_FILTER, {"RouteTables": []}),
    ("ec2", "describe_security_groups", _TAG_FILTER, {"SecurityGroups": []}),
    ("rds", "describe_db_instances", {}, {"DBInstances": []}),
    ("rds", "describe_db_parameter_groups", {}, {"DBParameterGroups": []}),
    ("rds", "describe_db_subnet_groups", {}, {"DBSubnetGroups": []}),
    (
        "secretsmanager",
        "list_secrets",
        {
            "IncludePlannedDeletion": True,
            "Filters": [{"Key": "name", "Values": [_PREFIX]}],
        },
        {"SecretList": []},
    ),
    ("secretsmanager", "describe_secret", None, None),
    ("ecr", "describe_repositories", {}, {"repositories": []}),
    ("ecs", "list_clusters", {}, {"clusterArns": []}),
    ("ecs", "list_services", None, None),
    (
        "ecs",
        "list_task_definitions",
        {"familyPrefix": _PREFIX, "status": "ACTIVE"},
        {"taskDefinitionArns": []},
    ),
    ("elbv2", "describe_load_balancers", {}, {"LoadBalancers": []}),
    ("elbv2", "describe_listeners", None, None),
    ("elbv2", "describe_target_groups", {}, {"TargetGroups": []}),
    ("iam", "list_roles", {}, {"Roles": [], "IsTruncated": False}),
    (
        "logs",
        "describe_log_groups",
        {"logGroupNamePrefix": _PREFIX},
        {"logGroups": []},
    ),
)


class _Clients:
    """Stubber 를 붙인 클라이언트 묶음. 기대 호출이 남으면 실패한다.

    실패 경로 시험은 스캐너가 첫 실패에서 멈추므로 남은 응답을 따지지
    않는다(expect_failure=True).
    """

    def __init__(self, expect_failure=False, regions=None):
        self.expect_failure = expect_failure
        regions = regions or {}
        self.clients = {
            service: _client(service, regions.get(service, _REGION))
            for service in _SERVICES
        }
        self.requested_regions = []
        self.stubs = {
            service: Stubber(client) for service, client in self.clients.items()
        }

    def factory(self, service, region_name):
        self.requested_regions.append((service, region_name))
        return self.clients[service]

    def scanner(self):
        return residue_aws.AwsResidueScanner(self.factory, region=_REGION)

    def expect(self, overrides=None, errors=None):
        """_CALLS 순서로 응답을 넣는다.

        Args:
            overrides: 작업 → [(응답, 기대 인자)]. 없으면 빈 응답 한 번.
            errors: 작업 → 오류 코드. 그 작업에서 멈추므로 뒤 응답은 넣지
                않는다(같은 클라이언트 안에서만).
        """
        overrides = overrides or {}
        errors = errors or {}
        stopped = set()
        for service, operation, params, empty in _CALLS:
            if service in stopped:
                continue
            stub = self.stubs[service]
            if operation in errors:
                stub.add_client_error(
                    operation, service_error_code=errors[operation]
                )
                stopped.add(service)
                continue
            entries = overrides.get(operation)
            if entries is None:
                entries = [] if empty is None else [(empty, params)]
            for response, expected in entries:
                stub.add_response(operation, response, expected)

    def __enter__(self):
        for stub in self.stubs.values():
            stub.activate()
        return self

    def __exit__(self, *exc):
        for stub in self.stubs.values():
            if not self.expect_failure:
                stub.assert_no_pending_responses()
            stub.deactivate()


def _role(name):
    return {
        "Path": "/",
        "RoleName": name,
        "RoleId": "AROAEXAMPLEEXAMPLE01",
        "Arn": f"arn:aws:iam::{_ACCOUNT}:role/{name}",
        "CreateDate": "2026-11-07T00:00:00Z",
    }


def _named(key_name, key_arn, service, resource_format, *names):
    return [
        {key_name: name, key_arn: _arn(service, resource_format.format(name))}
        for name in names
    ]


def test_scan_returns_empty_when_nothing_remains():
    with _Clients() as clients:
        clients.expect()

        assert clients.scanner().scan(_PREFIX) == ()


def test_scan_builds_every_client_in_the_requested_region():
    with _Clients() as clients:
        clients.expect()

        clients.scanner().scan(_PREFIX)

    assert {region for _, region in clients.requested_regions} == {_REGION}
    assert {service for service, _ in clients.requested_regions} == set(
        _SERVICES
    )


def test_scan_raises_scan_error_when_a_regional_client_is_in_another_region():
    # 운영자 기본 리전(us-east-1)을 따르는 생성 함수가 리전 인자를 무시한 경우.
    with _Clients(expect_failure=True, regions={"rds": "us-east-1"}) as clients:
        clients.expect()

        with pytest.raises(residue.ResidueScanError, match="us-east-1"):
            clients.scanner().scan(_PREFIX)


def test_scan_accepts_global_iam_client_in_any_region():
    with _Clients(regions={"iam": "us-east-1"}) as clients:
        clients.expect()

        assert clients.scanner().scan(_PREFIX) == ()


def test_scanner_rejects_empty_region():
    with pytest.raises(ValueError):
        residue_aws.AwsResidueScanner(
            lambda service, region_name: None, region=" "
        )


def test_scan_reports_tagged_network_resources_from_ec2_describe():
    owner = {"OwnerId": _ACCOUNT}
    with _Clients() as clients:
        clients.expect(
            {
                "describe_vpcs": [
                    ({"Vpcs": [{"VpcId": "vpc-1", **owner}]}, _TAG_FILTER)
                ],
                "describe_subnets": [
                    (
                        {"Subnets": [{"SubnetId": "subnet-1", **owner}]},
                        _TAG_FILTER,
                    )
                ],
                "describe_internet_gateways": [
                    (
                        {
                            "InternetGateways": [
                                {"InternetGatewayId": "igw-1", **owner}
                            ]
                        },
                        _TAG_FILTER,
                    )
                ],
                "describe_route_tables": [
                    (
                        {"RouteTables": [{"RouteTableId": "rtb-1", **owner}]},
                        _TAG_FILTER,
                    )
                ],
                "describe_security_groups": [
                    (
                        {
                            "SecurityGroups": [
                                {
                                    "GroupId": "sg-1",
                                    "GroupName": "ccx-r1-db",
                                    **owner,
                                }
                            ]
                        },
                        _TAG_FILTER,
                    )
                ],
            }
        )

        residue = clients.scanner().scan(_PREFIX)

    assert [(item.kind, item.native_id) for item in residue] == [
        ("ec2:internet-gateway", _arn("ec2", "internet-gateway/igw-1")),
        ("ec2:route-table", _arn("ec2", "route-table/rtb-1")),
        ("ec2:security-group", _arn("ec2", "security-group/sg-1")),
        ("ec2:subnet", _arn("ec2", "subnet/subnet-1")),
        ("ec2:vpc", _arn("ec2", "vpc/vpc-1")),
    ]
    assert {item.cloud for item in residue} == {"aws"}


def test_scan_keeps_only_named_resources_of_this_run():
    db_names = (f"{_PREFIX}-db", f"{_OTHER}-db")
    with _Clients() as clients:
        clients.expect(
            {
                "describe_db_instances": [
                    (
                        {
                            "DBInstances": _named(
                                "DBInstanceIdentifier",
                                "DBInstanceArn",
                                "rds",
                                "db:{}",
                                *db_names,
                            )
                        },
                        {},
                    )
                ],
                "describe_db_parameter_groups": [
                    (
                        {
                            "DBParameterGroups": _named(
                                "DBParameterGroupName",
                                "DBParameterGroupArn",
                                "rds",
                                "pg:{}",
                                f"{_PREFIX}-pg",
                                "default.postgres16",
                            )
                        },
                        {},
                    )
                ],
                "describe_db_subnet_groups": [
                    (
                        {
                            "DBSubnetGroups": _named(
                                "DBSubnetGroupName",
                                "DBSubnetGroupArn",
                                "rds",
                                "subgrp:{}",
                                f"{_PREFIX}-db",
                                f"team-{_PREFIX}",
                            )
                        },
                        {},
                    )
                ],
                "describe_repositories": [
                    (
                        {
                            "repositories": _named(
                                "repositoryName",
                                "repositoryArn",
                                "ecr",
                                "repository/{}",
                                f"{_PREFIX}-app",
                                f"{_OTHER}-app",
                            )
                        },
                        {},
                    )
                ],
                "describe_target_groups": [
                    (
                        {
                            "TargetGroups": _named(
                                "TargetGroupName",
                                "TargetGroupArn",
                                "elasticloadbalancing",
                                "targetgroup/{}/1",
                                f"{_PREFIX}-app",
                                f"{_OTHER}-app",
                            )
                        },
                        {},
                    )
                ],
                "list_roles": [
                    (
                        {
                            "Roles": [
                                _role(f"{_PREFIX}-exec"),
                                _role(f"{_OTHER}-exec"),
                            ],
                            "IsTruncated": False,
                        },
                        {},
                    )
                ],
                "describe_log_groups": [
                    (
                        {
                            "logGroups": _named(
                                "logGroupName",
                                "logGroupArn",
                                "logs",
                                "log-group:{}",
                                f"{_PREFIX}-ecs",
                                f"{_OTHER}-ecs",
                            )
                        },
                        {"logGroupNamePrefix": _PREFIX},
                    )
                ],
            }
        )

        residue = clients.scanner().scan(_PREFIX)

    assert [item.native_id for item in residue] == [
        _arn("ecr", "repository/ccx-r1-app"),
        _arn("elasticloadbalancing", "targetgroup/ccx-r1-app/1"),
        f"arn:aws:iam::{_ACCOUNT}:role/ccx-r1-exec",
        _arn("logs", "log-group:ccx-r1-ecs"),
        _arn("rds", "db:ccx-r1-db"),
        _arn("rds", "pg:ccx-r1-pg"),
        _arn("rds", "subgrp:ccx-r1-db"),
    ]


def test_scan_lists_services_only_inside_clusters_of_this_run():
    other_cluster = _arn("ecs", f"cluster/{_OTHER}")
    service_arn = _arn("ecs", f"service/{_PREFIX}/{_PREFIX}-app")
    with _Clients() as clients:
        clients.expect(
            {
                "list_clusters": [
                    ({"clusterArns": [_CLUSTER_ARN, other_cluster]}, {})
                ],
                "list_services": [
                    ({"serviceArns": [service_arn]}, {"cluster": _CLUSTER_ARN})
                ],
            }
        )

        residue = clients.scanner().scan(_PREFIX)

    assert [item.native_id for item in residue] == [_CLUSTER_ARN, service_arn]
    assert residue[1].kind == "ecs:service"


def test_scan_counts_only_active_task_definitions_of_this_run():
    mine = _arn("ecs", f"task-definition/{_PREFIX}-app:3")
    other = _arn("ecs", f"task-definition/{_OTHER}-app:1")
    with _Clients() as clients:
        clients.expect(
            {
                "list_task_definitions": [
                    (
                        {"taskDefinitionArns": [mine, other]},
                        {"familyPrefix": _PREFIX, "status": "ACTIVE"},
                    )
                ]
            }
        )

        residue = clients.scanner().scan(_PREFIX)

    assert [(item.kind, item.native_id) for item in residue] == [
        ("ecs:task-definition", mine)
    ]


def test_scan_lists_listeners_of_load_balancers_of_this_run():
    listener_arn = _arn(
        "elasticloadbalancing", f"listener/app/{_PREFIX}-alb/1/2"
    )
    with _Clients() as clients:
        clients.expect(
            {
                "describe_load_balancers": [
                    (
                        {
                            "LoadBalancers": [
                                {
                                    "LoadBalancerName": f"{_PREFIX}-alb",
                                    "LoadBalancerArn": _LB_ARN,
                                },
                                {
                                    "LoadBalancerName": f"{_OTHER}-alb",
                                    "LoadBalancerArn": _LB_ARN + "0",
                                },
                            ]
                        },
                        {},
                    )
                ],
                "describe_listeners": [
                    (
                        {"Listeners": [{"ListenerArn": listener_arn}]},
                        {"LoadBalancerArn": _LB_ARN},
                    )
                ],
            }
        )

        residue = clients.scanner().scan(_PREFIX)

    assert [(item.kind, item.native_id) for item in residue] == [
        ("elasticloadbalancing:listener", listener_arn),
        ("elasticloadbalancing:loadbalancer", _LB_ARN),
    ]


def test_scan_rechecks_listed_secrets_by_id_and_keeps_planned_deletion():
    gone_arn = _SECRET_ARN.replace("database-url", "old")
    other_arn = _SECRET_ARN.replace("ccx-r1", _OTHER)
    with _Clients() as clients:
        clients.expect(
            {
                "list_secrets": [
                    (
                        {
                            "SecretList": [
                                {"ARN": _SECRET_ARN, "Name": "ccx-r1-db-url"},
                                {"ARN": gone_arn, "Name": "ccx-r1-old"},
                                {"ARN": other_arn, "Name": "ccx-r10-db-url"},
                            ]
                        },
                        {
                            "IncludePlannedDeletion": True,
                            "Filters": [{"Key": "name", "Values": [_PREFIX]}],
                        },
                    )
                ],
                "describe_secret": [
                    (
                        {
                            "ARN": _SECRET_ARN,
                            "Name": "ccx-r1-db-url",
                            "DeletedDate": "2026-11-07T00:00:00Z",
                        },
                        {"SecretId": _SECRET_ARN},
                    )
                ],
            }
        )
        clients.stubs["secretsmanager"].add_client_error(
            "describe_secret",
            service_error_code="ResourceNotFoundException",
            expected_params={"SecretId": gone_arn},
        )

        residue = clients.scanner().scan(_PREFIX)

    assert [(item.kind, item.native_id) for item in residue] == [
        ("secretsmanager:secret", _SECRET_ARN)
    ]


@pytest.mark.parametrize("prefix", ["", "ccx-", "ccx-R1", "team-ccx-r1"])
def test_scan_rejects_malformed_prefix_before_querying(prefix):
    scanner = residue_aws.AwsResidueScanner(
        lambda service, region_name: None, region=_REGION
    )

    with pytest.raises(ValueError):
        scanner.scan(prefix)


@pytest.mark.parametrize(
    "operation",
    ["describe_vpcs", "describe_db_instances", "list_roles", "list_clusters"],
)
def test_scan_raises_scan_error_when_any_listing_fails(operation):
    with _Clients(expect_failure=True) as clients:
        clients.expect(errors={operation: "ThrottlingException"})

        with pytest.raises(residue.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_scan_raises_scan_error_when_aws_is_unreachable():
    def client(service, region_name):
        raise exceptions.NoCredentialsError()

    with pytest.raises(residue.ResidueScanError):
        residue_aws.AwsResidueScanner(client, region=_REGION).scan(_PREFIX)


def test_scan_raises_scan_error_on_malformed_response():
    with _Clients(expect_failure=True) as clients:
        # OwnerId 가 빠진 VPC — ARN 을 만들 수 없다.
        clients.expect(
            {"describe_vpcs": [({"Vpcs": [{"VpcId": "vpc-1"}]}, _TAG_FILTER)]}
        )

        with pytest.raises(residue.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_scan_raises_scan_error_on_malformed_arn():
    with _Clients(expect_failure=True) as clients:
        clients.expect(
            {
                "describe_repositories": [
                    (
                        {
                            "repositories": [
                                {
                                    "repositoryName": f"{_PREFIX}-app",
                                    "repositoryArn": "not-an-arn",
                                }
                            ]
                        },
                        {},
                    )
                ]
            }
        )

        with pytest.raises(residue.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_scan_raises_scan_error_when_list_key_is_missing():
    with _Clients(expect_failure=True) as clients:
        clients.expect(
            {"describe_log_groups": [({}, {"logGroupNamePrefix": _PREFIX})]}
        )

        with pytest.raises(residue.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_scan_raises_scan_error_when_secret_recheck_fails():
    with _Clients(expect_failure=True) as clients:
        clients.expect(
            {
                "list_secrets": [
                    (
                        {
                            "SecretList": [
                                {"ARN": _SECRET_ARN, "Name": "ccx-r1-db-url"}
                            ]
                        },
                        {
                            "IncludePlannedDeletion": True,
                            "Filters": [{"Key": "name", "Values": [_PREFIX]}],
                        },
                    )
                ]
            }
        )
        clients.stubs["secretsmanager"].add_client_error(
            "describe_secret",
            service_error_code="AccessDeniedException",
            expected_params={"SecretId": _SECRET_ARN},
        )

        with pytest.raises(residue.ResidueScanError):
            clients.scanner().scan(_PREFIX)


def test_scanner_accepts_session_client_factory_without_calling_aws():
    session = boto3.Session(
        region_name=_REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )

    scanner = residue_aws.AwsResidueScanner(session.client, region=_REGION)

    assert isinstance(scanner, residue_aws.AwsResidueScanner)
