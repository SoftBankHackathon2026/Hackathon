"""AWS 잔여 검사 — 실행 접두사 자원이 아직 남았는지 질의로 판정한다.

terraform destroy 뒤에 부른다. ResidueScanner 계약(scan(name_prefix))을 따른다.
질의는 자원 무리마다 한 번이다.

*   리전 자원: Resource Groups Tagging API 로 태그 `crosscloud-run=<접두사>`
    가 붙은 것.
*   IAM 역할: 전역이라 태그 질의에 안 나온다. 이름 접두사로 거른다.
*   Route 53 호스팅 영역: 이름 첫 조각(`ccx-r1.internal.` 의 `ccx-r1`)으로
    거른다.
*   Secrets Manager: 삭제 예약된 Secret 도 목록에 넣어 이름으로 거른 뒤, ID 로
    다시 조회해 정말 남았는지 본다(강제 삭제 직후에는 목록이 늦다).

질의가 실패하면 ResidueScanError 를 올린다. 실패를 "잔여 0"으로 읽으면 남은
자원을 놓치기 때문이다.

ECS 작업 정의는 terraform destroy 가 등록 해제(INACTIVE)만 한다. 태그 질의가
INACTIVE 작업 정의를 돌려주면 잔여로 보고된다 — 거르지 않는다(실측 미확인).

  scanner = residue_aws.AwsResidueScanner.from_session(boto3.Session())
  residue = scanner.scan("ccx-r1")
"""

import dataclasses
import re
from collections.abc import Iterator
from typing import Any, Self

import boto3
from botocore import exceptions

from crosscloud import model

# TODO(rebase): 아래 네 정의는 R2g 의 crosscloud.provision.residue 와 같은
# 이름·모양의 임시 사본이다. 합칠 때 지우고 residue 모듈을 쓴다.
_NAME_PREFIX_PATTERN = re.compile(r"ccx-[a-z0-9]{1,16}")


class ResidueScanError(RuntimeError):
    """잔여 질의가 실패했거나 결과를 읽지 못했다. 잔여 0 으로 보지 않는다."""


@dataclasses.dataclass(frozen=True)
class ResidueItem:
    """클라우드에 남은 자원 하나.

    Attributes:
        cloud: 자원이 있는 클라우드.
        kind: 클라우드 고유 자원 종류(AWS 는 "서비스:자원 종류").
        native_id: 클라우드 고유 식별자(AWS ARN).
    """

    cloud: model.Cloud
    kind: str
    native_id: str


def check_name_prefix(name_prefix: str) -> None:
    """실행 접두사 형식(`ccx-<run>`, run 은 [a-z0-9]{1,16})을 확인한다.

    Raises:
        ValueError: 형식이 아닐 때.
    """
    if not _NAME_PREFIX_PATTERN.fullmatch(name_prefix):
        raise ValueError(
            f"실행 접두사는 ccx-<영문 소문자·숫자 1–16자> 여야 한다: "
            f"{name_prefix!r}"
        )


def is_named_with_prefix(name: str, name_prefix: str) -> bool:
    """자원 이름이 실행 접두사 자체이거나 `접두사-` 로 시작하는지."""
    return name == name_prefix or name.startswith(f"{name_prefix}-")


# 위 TODO(rebase) 끝.

_RUN_TAG_KEY = "crosscloud-run"


class AwsResidueScanner:
    """한 AWS 계정·리전에서 실행 접두사 자원을 찾는다(ResidueScanner 구현)."""

    def __init__(
        self, *, tagging: Any, iam: Any, route53: Any, secrets: Any
    ) -> None:
        """질의에 쓸 boto3 클라이언트를 받는다.

        Args:
            tagging: 대상 리전의 `resourcegroupstaggingapi` 클라이언트.
            iam: `iam` 클라이언트.
            route53: `route53` 클라이언트.
            secrets: 대상 리전의 `secretsmanager` 클라이언트.
        """
        self._tagging = tagging
        self._iam = iam
        self._route53 = route53
        self._secrets = secrets

    @classmethod
    def from_session(cls, session: boto3.Session) -> Self:
        """세션의 자격 증명·리전으로 클라이언트를 만든다(호출은 아직 없음)."""
        return cls(
            tagging=session.client("resourcegroupstaggingapi"),
            iam=session.client("iam"),
            route53=session.client("route53"),
            secrets=session.client("secretsmanager"),
        )

    def scan(self, name_prefix: str) -> tuple[ResidueItem, ...]:
        """이름이 name_prefix 이거나 `name_prefix-` 로 시작하는 자원을 찾는다.

        Args:
            name_prefix: 실행 접두사(`ccx-<run>`).

        Returns:
            남은 자원을 ARN 순으로. 같은 ARN 은 한 번만 담는다. 비면 잔여 0.

        Raises:
            ValueError: name_prefix 가 실행 접두사 형식이 아닐 때.
            ResidueScanError: 질의가 실패했을 때.
        """
        check_name_prefix(name_prefix)
        try:
            arns = {
                *self._tagged(name_prefix),
                *self._roles(name_prefix),
                *self._zones(name_prefix),
                *self._secrets_left(name_prefix),
            }
        except exceptions.ClientError as error:
            raise ResidueScanError(
                f"AWS 잔여 질의 실패: {error.operation_name} "
                f"{error.response['Error']['Code']}"
            ) from error
        return tuple(
            ResidueItem(cloud="aws", kind=_kind(arn), native_id=arn)
            for arn in sorted(arns)
        )

    def _tagged(self, name_prefix: str) -> Iterator[str]:
        pages = self._tagging.get_paginator("get_resources").paginate(
            TagFilters=[{"Key": _RUN_TAG_KEY, "Values": [name_prefix]}]
        )
        for page in pages:
            for mapping in page["ResourceTagMappingList"]:
                yield mapping["ResourceARN"]

    def _roles(self, name_prefix: str) -> Iterator[str]:
        for page in self._iam.get_paginator("list_roles").paginate():
            for role in page["Roles"]:
                if is_named_with_prefix(role["RoleName"], name_prefix):
                    yield role["Arn"]

    def _zones(self, name_prefix: str) -> Iterator[str]:
        pages = self._route53.get_paginator("list_hosted_zones").paginate()
        for page in pages:
            for zone in page["HostedZones"]:
                first_label = zone["Name"].split(".", 1)[0]
                if is_named_with_prefix(first_label, name_prefix):
                    yield f"arn:aws:route53:::{zone['Id'].lstrip('/')}"

    def _secrets_left(self, name_prefix: str) -> Iterator[str]:
        # name 필터는 앞부분 일치라 ccx-r10 도 돌려준다. 이름으로 다시 거른다.
        pages = self._secrets.get_paginator("list_secrets").paginate(
            IncludePlannedDeletion=True,
            Filters=[{"Key": "name", "Values": [name_prefix]}],
        )
        for page in pages:
            for secret in page["SecretList"]:
                if not is_named_with_prefix(secret["Name"], name_prefix):
                    continue
                if self._secret_exists(secret["ARN"]):
                    yield secret["ARN"]

    def _secret_exists(self, arn: str) -> bool:
        try:
            self._secrets.describe_secret(SecretId=arn)
        except self._secrets.exceptions.ResourceNotFoundException:
            return False
        return True


def _kind(arn: str) -> str:
    """ARN 에서 "서비스:자원 종류"를 뽑는다(예: ec2:security-group)."""
    _, _, service, _, _, resource = arn.split(":", 5)
    resource_type = re.match(r"[^:/]*", resource).group(0)
    return f"{service}:{resource_type}"
