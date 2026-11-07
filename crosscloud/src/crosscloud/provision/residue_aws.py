"""AWS 잔여 검사 — 우리 모듈이 만드는 자원 종류마다 서비스 API 를 직접 조회한다.

residue.ResidueScanner 계약(scan(name_prefix))을 따른다. 잔여 검사는 보조
확인이다 — 정리됐다는 1차 근거는 terraform destroy 뒤 state 가 빈 것이고, 이
검사는 그 뒤 state 밖에 남은 자원(생성 응답 유실 등)을 찾는다. 서비스 API 를
직접 부른다. Resource Groups Tagging API 는 색인이라 변경을 늦게 반영해 쓰지
않는다. 서비스 목록 자체도 최종 일관성일 수 있어(예: Secrets Manager 목록),
빈 결과가 잔여 0 을 보장하지는 않는다.

crosscloud/terraform/aws 가 만드는 자원 종류와 조회 방법:

*   EC2 VPC·서브넷·인터넷 게이트웨이·라우트 테이블·보안 그룹: Describe 에
    태그 필터 `tag:crosscloud-run=<접두사>` 를 건다(모든 자원에 default_tags).
*   RDS 인스턴스·파라미터 그룹·서브넷 그룹, ECR 저장소, ALB·대상 그룹, IAM 역할,
    CloudWatch 로그 그룹: Describe/List 한 뒤 이름으로 거른다.
*   ALB 리스너: 이 실행의 ALB 마다 리스너를 조회한다.
*   ECS: 이 실행의 클러스터와 그 안의 서비스, ACTIVE 작업 정의.
*   Secrets Manager: 삭제 예약 포함 목록을 이름으로 거른 뒤 ID 로 다시
    조회한다(강제 삭제 직후에는 목록이 늦다).

이름 거르기는 이름이 접두사 자체이거나 `접두사-` 로 시작하는지 본다.

INACTIVE 작업 정의는 잔여로 세지 않는다. terraform destroy 는 작업 정의를 등록
해제(INACTIVE)만 할 수 있고 지우지 못한다. 등록 해제된 정의는 과금되지 않고 새
서비스·작업에 쓸 수 없다. 이것까지 세면 정상 해체 뒤에도 늘 잔여가 보고된다.

조회 하나라도 실패하거나 응답 형식이 어긋나면 residue.ResidueScanError 다.
잔여 0 으로 보지 않는다. 모듈에 새 자원 종류를 더하면 _SCANS 에도 더해야
한다 — 표에 없는 종류는 잔여 검사가 보지 않는다.

  scanner = residue_aws.AwsResidueScanner(boto3.Session().client)
  items = scanner.scan("ccx-r1")
"""

import dataclasses
import re
from collections.abc import Callable, Iterator
from typing import Any

from botocore import exceptions

from crosscloud.provision import residue

_RUN_TAG_KEY = "crosscloud-run"

# boto3 클라이언트를 서비스 이름으로 만드는 함수(예: boto3.Session().client).
ClientFactory = Callable[[str], Any]


class AwsResidueScanner:
    """한 AWS 계정·리전에서 실행 접두사 자원을 찾는다(ResidueScanner 구현)."""

    def __init__(self, client: ClientFactory) -> None:
        """질의에 쓸 클라이언트 생성 함수를 받는다.

        Args:
            client: 서비스 이름으로 boto3 클라이언트를 만드는 함수. 리전 자원은
                이 클라이언트의 리전에서 찾는다.
        """
        self._client = client

    def scan(self, name_prefix: str) -> tuple[residue.ResidueItem, ...]:
        """이름이 name_prefix 이거나 `name_prefix-` 로 시작하는 자원을 찾는다.

        Args:
            name_prefix: 실행 접두사(`ccx-<run>`).

        Returns:
            남은 자원을 ARN 순으로. 같은 ARN 은 한 번만 담는다. 비면 잔여 0.

        Raises:
            ValueError: name_prefix 가 실행 접두사 형식이 아닐 때.
            residue.ResidueScanError: 조회가 실패했거나 응답 형식이 어긋났을 때.
        """
        residue.check_name_prefix(name_prefix)
        try:
            arns = {
                arn
                for service, listing in _SCANS
                for arn in listing(self._client(service), name_prefix)
            }
        except exceptions.ClientError as error:
            raise residue.ResidueScanError(
                f"AWS 잔여 조회 실패: {error.operation_name} "
                f"{error.response['Error']['Code']}"
            ) from error
        except exceptions.BotoCoreError as error:
            raise residue.ResidueScanError(
                f"AWS 잔여 조회 실패: {error}"
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise residue.ResidueScanError(
                f"AWS 잔여 조회 응답 형식 오류: {error!r}"
            ) from error
        return tuple(
            residue.ResidueItem(cloud="aws", kind=_kind(arn), native_id=arn)
            for arn in sorted(arns)
        )


@dataclasses.dataclass(frozen=True)
class _Named:
    """이름으로 거르는 목록 조회 하나.

    Attributes:
        operation: 페이지 넘김을 지원하는 boto3 작업 이름.
        list_key: 응답에서 항목 목록 키.
        name_key: 항목의 이름 키.
        arn_key: 항목의 ARN 키.
        prefix_param: 서버 쪽 접두사 인자 이름. 없으면 전부 받아 거른다.
    """

    operation: str
    list_key: str
    name_key: str
    arn_key: str
    prefix_param: str | None = None


def _named(client: Any, listing: _Named, name_prefix: str) -> Iterator[str]:
    params = {listing.prefix_param: name_prefix} if listing.prefix_param else {}
    paginator = client.get_paginator(listing.operation)
    for page in paginator.paginate(**params):
        for item in page.get(listing.list_key, []):
            if residue.is_named_with_prefix(
                item[listing.name_key], name_prefix
            ):
                yield item[listing.arn_key]


# EC2 자원은 이름 대신 태그로 찾는다. (작업, 목록 키, ID 키, ARN 자원 종류)
_EC2_LISTINGS = (
    ("describe_vpcs", "Vpcs", "VpcId", "vpc"),
    ("describe_subnets", "Subnets", "SubnetId", "subnet"),
    (
        "describe_internet_gateways",
        "InternetGateways",
        "InternetGatewayId",
        "internet-gateway",
    ),
    ("describe_route_tables", "RouteTables", "RouteTableId", "route-table"),
    ("describe_security_groups", "SecurityGroups", "GroupId", "security-group"),
)


def _ec2_tagged(client: Any, name_prefix: str) -> Iterator[str]:
    region = client.meta.region_name
    filters = [{"Name": f"tag:{_RUN_TAG_KEY}", "Values": [name_prefix]}]
    for operation, list_key, id_key, resource in _EC2_LISTINGS:
        paginator = client.get_paginator(operation)
        for page in paginator.paginate(Filters=filters):
            for item in page[list_key]:
                yield (
                    f"arn:aws:ec2:{region}:{item['OwnerId']}:"
                    f"{resource}/{item[id_key]}"
                )


_RDS_LISTINGS = (
    _Named(
        "describe_db_instances",
        "DBInstances",
        "DBInstanceIdentifier",
        "DBInstanceArn",
    ),
    _Named(
        "describe_db_parameter_groups",
        "DBParameterGroups",
        "DBParameterGroupName",
        "DBParameterGroupArn",
    ),
    _Named(
        "describe_db_subnet_groups",
        "DBSubnetGroups",
        "DBSubnetGroupName",
        "DBSubnetGroupArn",
    ),
)


def _rds(client: Any, name_prefix: str) -> Iterator[str]:
    for listing in _RDS_LISTINGS:
        yield from _named(client, listing, name_prefix)


def _secrets(client: Any, name_prefix: str) -> Iterator[str]:
    # name 필터는 앞부분 일치라 ccx-r10 도 돌려준다. 이름으로 다시 거른다.
    pages = client.get_paginator("list_secrets").paginate(
        IncludePlannedDeletion=True,
        Filters=[{"Key": "name", "Values": [name_prefix]}],
    )
    for page in pages:
        for secret in page["SecretList"]:
            if not residue.is_named_with_prefix(secret["Name"], name_prefix):
                continue
            try:
                client.describe_secret(SecretId=secret["ARN"])
            except client.exceptions.ResourceNotFoundException:
                continue
            yield secret["ARN"]


def _ecr(client: Any, name_prefix: str) -> Iterator[str]:
    yield from _named(
        client,
        _Named(
            "describe_repositories",
            "repositories",
            "repositoryName",
            "repositoryArn",
        ),
        name_prefix,
    )


def _ecs(client: Any, name_prefix: str) -> Iterator[str]:
    for page in client.get_paginator("list_clusters").paginate():
        for cluster in page["clusterArns"]:
            if not residue.is_named_with_prefix(
                _last_segment(cluster), name_prefix
            ):
                continue
            yield cluster
            services = client.get_paginator("list_services").paginate(
                cluster=cluster
            )
            for service_page in services:
                yield from service_page["serviceArns"]
    definitions = client.get_paginator("list_task_definitions").paginate(
        familyPrefix=name_prefix, status="ACTIVE"
    )
    for page in definitions:
        for arn in page["taskDefinitionArns"]:
            family = _last_segment(arn).rsplit(":", 1)[0]
            if residue.is_named_with_prefix(family, name_prefix):
                yield arn


def _elbv2(client: Any, name_prefix: str) -> Iterator[str]:
    for page in client.get_paginator("describe_load_balancers").paginate():
        for balancer in page["LoadBalancers"]:
            name = balancer["LoadBalancerName"]
            if not residue.is_named_with_prefix(name, name_prefix):
                continue
            arn = balancer["LoadBalancerArn"]
            yield arn
            listeners = client.get_paginator("describe_listeners").paginate(
                LoadBalancerArn=arn
            )
            for listener_page in listeners:
                for listener in listener_page["Listeners"]:
                    yield listener["ListenerArn"]
    yield from _named(
        client,
        _Named(
            "describe_target_groups",
            "TargetGroups",
            "TargetGroupName",
            "TargetGroupArn",
        ),
        name_prefix,
    )


def _iam(client: Any, name_prefix: str) -> Iterator[str]:
    yield from _named(
        client, _Named("list_roles", "Roles", "RoleName", "Arn"), name_prefix
    )


def _logs(client: Any, name_prefix: str) -> Iterator[str]:
    yield from _named(
        client,
        _Named(
            "describe_log_groups",
            "logGroups",
            "logGroupName",
            "logGroupArn",
            prefix_param="logGroupNamePrefix",
        ),
        name_prefix,
    )


# (boto3 서비스 이름, 조회 함수). 모듈이 만드는 자원 종류를 빠짐없이 덮는다.
_SCANS: tuple[tuple[str, Callable[[Any, str], Iterator[str]]], ...] = (
    ("ec2", _ec2_tagged),
    ("rds", _rds),
    ("secretsmanager", _secrets),
    ("ecr", _ecr),
    ("ecs", _ecs),
    ("elbv2", _elbv2),
    ("iam", _iam),
    ("logs", _logs),
)


def _last_segment(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def _kind(arn: str) -> str:
    """ARN 에서 "서비스:자원 종류"를 뽑는다(예: ec2:security-group)."""
    _, _, service, _, _, resource = arn.split(":", 5)
    resource_type = re.match(r"[^:/]*", resource).group(0)
    return f"{service}:{resource_type}"
