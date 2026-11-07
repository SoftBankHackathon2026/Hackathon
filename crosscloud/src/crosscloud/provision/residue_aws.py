"""AWS 잔여 검사 — 한 실행이 만든 자원이 아직 남았는지 질의로 판정한다.

terraform destroy 뒤에 부른다. 판정 수단은 둘이다.

*   태그 질의: 리전 자원은 Resource Groups Tagging API 로 실행 태그
    (`crosscloud-run`)가 붙은 것을 찾는다.
*   접두사 질의: 태그 질의가 리전 밖이라 못 보는 전역 자원은 이름 접두사로
    찾는다 — IAM 역할은 경로(`/<접두사>/`), Route 53 호스팅 영역은 이름
    (`<접두사>.`).

질의가 실패하면 예외를 그대로 올린다. 실패를 "잔여 0"으로 읽으면 남은 자원을
놓치기 때문이다.

ECS 작업 정의는 terraform destroy 가 등록 해제(INACTIVE)만 한다. 태그 질의가
INACTIVE 작업 정의를 돌려주면 여기서도 잔여로 보고된다 — 거르지 않는다(실측
미확인).

  scanner = residue_aws.AwsResidueScanner.from_session(boto3.Session())
  residue = scanner.scan(selector)
"""

import dataclasses
from typing import Any, Protocol, Self

import boto3

from crosscloud import model


@dataclasses.dataclass(frozen=True)
class ResidueItem:
    """남은 자원 하나.

    Attributes:
        cloud: 자원이 있는 클라우드.
        native_id: 자원 ARN.
        labels: 질의가 함께 돌려준 태그. 전역 자원은 비어 있다.
    """

    cloud: model.Cloud
    native_id: str
    labels: tuple[model.KeyValue, ...]


class ResidueSelector(Protocol):
    """잔여 검사 대상을 고르는 조건. 구조적 타이핑으로만 받는다."""

    @property
    def labels(self) -> tuple[model.KeyValue, ...]:
        """실행 태그. 모두 붙은 자원만 고른다."""

    @property
    def name_prefix(self) -> str:
        """자원 이름 접두사(예: `ccx-<run>`)."""


class AwsResidueScanner:
    """태그·접두사 질의로 AWS 잔여를 찾는다."""

    def __init__(self, *, tagging: Any, iam: Any, route53: Any) -> None:
        """질의에 쓸 boto3 클라이언트를 받는다.

        Args:
            tagging: 대상 리전의 `resourcegroupstaggingapi` 클라이언트.
            iam: `iam` 클라이언트.
            route53: `route53` 클라이언트.
        """
        self._tagging = tagging
        self._iam = iam
        self._route53 = route53

    @classmethod
    def from_session(cls, session: boto3.Session) -> Self:
        """세션의 자격 증명·리전으로 클라이언트를 만든다(호출은 아직 없음)."""
        return cls(
            tagging=session.client("resourcegroupstaggingapi"),
            iam=session.client("iam"),
            route53=session.client("route53"),
        )

    def scan(self, selector: ResidueSelector) -> tuple[ResidueItem, ...]:
        """선택 조건에 걸리는 남은 자원을 ARN 순으로 돌려준다.

        Args:
            selector: 실행 태그와 이름 접두사.

        Returns:
            남은 자원. 같은 ARN 은 한 번만 담는다. 비면 잔여 0 이다.

        Raises:
            ValueError: 태그나 접두사가 비어 계정 전체가 걸릴 때.
            botocore.exceptions.ClientError: 질의가 실패했을 때.
        """
        prefix = selector.name_prefix.strip()
        if not selector.labels or not prefix:
            raise ValueError(
                "잔여 검사에는 실행 태그와 이름 접두사가 모두 필요하다"
            )
        found: dict[str, ResidueItem] = {}
        # 태그 질의 결과를 먼저 넣는다 — 같은 ARN 이면 태그가 있는 쪽을 남긴다.
        for item in (
            *self._tagged(selector.labels),
            *self._roles(prefix),
            *self._zones(prefix),
        ):
            found.setdefault(item.native_id, item)
        return tuple(found[arn] for arn in sorted(found))

    def _tagged(self, labels: tuple[model.KeyValue, ...]) -> list[ResidueItem]:
        tag_filters = [
            {"Key": label.key, "Values": [str(label.value)]} for label in labels
        ]
        pages = self._tagging.get_paginator("get_resources").paginate(
            TagFilters=tag_filters
        )
        return [
            ResidueItem(
                cloud="aws",
                native_id=mapping["ResourceARN"],
                labels=tuple(
                    model.KeyValue(key=tag["Key"], value=tag["Value"])
                    for tag in mapping.get("Tags", [])
                ),
            )
            for page in pages
            for mapping in page["ResourceTagMappingList"]
        ]

    def _roles(self, prefix: str) -> list[ResidueItem]:
        pages = self._iam.get_paginator("list_roles").paginate(
            PathPrefix=f"/{prefix}/"
        )
        return [
            ResidueItem(cloud="aws", native_id=role["Arn"], labels=())
            for page in pages
            for role in page["Roles"]
        ]

    def _zones(self, prefix: str) -> list[ResidueItem]:
        # 영역 이름은 끝에 점이 붙는다("ccx-r1.internal."). 접두사 뒤에 점을
        # 붙여 비교해야 ccx-r10 같은 다른 실행이 걸리지 않는다.
        pages = self._route53.get_paginator("list_hosted_zones").paginate()
        return [
            ResidueItem(
                cloud="aws",
                native_id=f"arn:aws:route53:::{zone['Id'].lstrip('/')}",
                labels=(),
            )
            for page in pages
            for zone in page["HostedZones"]
            if zone["Name"].startswith(f"{prefix}.")
        ]
