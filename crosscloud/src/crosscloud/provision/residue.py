"""잔여 검사 — 이 실행(run)이 만든 자원이 클라우드에 남았는지 본다.

crosscloud 가 만드는 모든 자원의 이름은 실행 접두사 `ccx-<run>` 으로 시작한다.
terraform destroy 뒤 클라우드마다 이 접두사로 질의해, 결과가 비어야 잔여 0
이다. 질의가 실패하면 "잔여 없음"이 아니라 오류다. 조회 수단이 색인처럼 늦게
반영되면, 구현은 빈 결과 한 번으로 0 을 확정하지 말고 간격을 두고 다시 본다.

클라우드별 구현은 residue_gcp(GCP)·residue_aws(AWS) 에 둔다.

  items = scanner.scan("ccx-r42")
  if items: ...  # 사람에게 넘긴다
"""

import dataclasses
import re
from typing import Protocol

from crosscloud import model

# 실행 접두사 형식. run 은 영문 소문자·숫자 1–16자다. 클라우드별 이름 길이
# 제한(Cloud Run 서비스 49자, ALB·대상 그룹 32자, 서비스 계정 30자)에 접미사를
# 붙여도 들어가게 짧게 잡는다. 하이픈을 run 안에 두지 않아, 접두사 뒤 첫
# 하이픈이 곧 접두사의 끝이다(ccx-r1 이 ccx-r12-app 을 잡지 않는다).
NAME_PREFIX_PATTERN = re.compile(r"ccx-[a-z0-9]{1,16}")


class ResidueScanError(RuntimeError):
    """잔여 질의가 실패했거나 결과를 읽지 못했다. 잔여 0 으로 보지 않는다."""


@dataclasses.dataclass(frozen=True)
class ResidueItem:
    """클라우드에 남은 자원 하나.

    Attributes:
        cloud: 자원이 있는 클라우드.
        kind: 클라우드 고유 자원 종류(GCP asset type, AWS 리소스 종류).
        native_id: 클라우드 고유 식별자(GCP 전체 리소스 이름, AWS ARN).
    """

    cloud: model.Cloud
    kind: str
    native_id: str


class ResidueScanner(Protocol):
    """한 클라우드에서 실행 접두사로 남은 자원을 찾는다."""

    def scan(self, name_prefix: str) -> tuple[ResidueItem, ...]:
        """이름이 name_prefix 이거나 `name_prefix-` 로 시작하는 자원을 찾는다.

        Args:
            name_prefix: 실행 접두사(`ccx-<run>`).

        Returns:
            남은 자원. 비었으면 잔여 0 이다.

        Raises:
            ValueError: name_prefix 가 실행 접두사 형식이 아닐 때.
            ResidueScanError: 질의가 실패했거나 결과를 읽지 못할 때.
        """
        ...


def check_name_prefix(name_prefix: str) -> None:
    """실행 접두사 형식(`ccx-<run>`, run 은 [a-z0-9]{1,16})을 확인한다.

    빈 접두사나 짧은 접두사로 질의하면 남의 자원까지 잔여로 잡힌다.

    Raises:
        ValueError: 형식이 아닐 때.
    """
    if not NAME_PREFIX_PATTERN.fullmatch(name_prefix):
        raise ValueError(
            f"실행 접두사는 ccx-<영문 소문자·숫자 1–16자> 여야 한다: "
            f"{name_prefix!r}"
        )


def is_named_with_prefix(name: str, name_prefix: str) -> bool:
    """자원 이름이 실행 접두사 자체이거나 `접두사-` 로 시작하는지."""
    return name == name_prefix or name.startswith(f"{name_prefix}-")
