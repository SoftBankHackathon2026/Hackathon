"""GCP 잔여 검사 — Cloud Asset 검색과 서비스 계정 목록으로 접두사 자원을 찾는다.

한 회차는 질의 둘이다.

*   `gcloud asset search-all-resources`: 프로젝트 안의 모든 자원 종류(Cloud Run·
    Cloud SQL·Secret Manager·Artifact Registry·부하 분산기 구성 요소 등)를 한
    번에 검색한다. 이름 구문 검색(name:"ccx-r1")으로 후보를 받은 뒤, 전체 리소스
    이름의 마지막 조각이 접두사로 시작하는 것만 남긴다 — 구문 검색은 단어
    단위라 `team-ccx-r1` 같은 이름도 돌려줄 수 있다.
*   `gcloud iam service-accounts list`: 서비스 계정은 Cloud Asset 이 이름을
    숫자 ID(…/serviceAccounts/<숫자>)로 돌려줄 수 있어 이름으로는 못 거른다.
    IAM API 를 직접 불러 이메일 앞부분으로 거른다. Cloud Asset 결과의 서비스
    계정은 쓰지 않는다.

Cloud Asset 은 실시간 조회가 아니라 색인이라 자원 변경을 몇 분 늦게 반영할 수
있다. 그래서 빈 결과 한 번으로 잔여 0 을 확정하지 않고, 간격을 두고 여러 회차
연속으로 비어야 0 으로 본다. 잔여가 보이면 그 회차에서 바로 돌려준다.

전제: 프로젝트에 cloudasset.googleapis.com 이 켜져 있고, 호출 신원에
cloudasset.assets.searchAllResources 와 iam.serviceAccounts.list 권한이 있다.
"""

import dataclasses
import json
import subprocess
import time
from collections.abc import Callable
from typing import Any

from crosscloud.provision import residue

SERVICE_ACCOUNT = "iam.googleapis.com/ServiceAccount"

# 실패 예외에 담는 gcloud 표준 오류 끝 줄 수. 목록 조회라 비밀 입력이 없다.
_STDERR_TAIL_LINES = 10


@dataclasses.dataclass(frozen=True)
class GcpResidueScanner:
    """GCP 프로젝트 하나에서 실행 접두사 자원을 찾는다(ResidueScanner 구현).

    Attributes:
        project: GCP 프로젝트 ID.
        confirmations: 잔여 0 으로 보려면 연속으로 비어야 하는 회차 수.
        recheck_seconds: 빈 회차 뒤 다음 회차까지 기다리는 초.
        gcloud: gcloud 실행 파일.
        run: 명령 실행 함수. 시험에서 바꿔 끼운다.
        sleep: 기다리는 함수. 시험에서 바꿔 끼운다.
    """

    project: str
    confirmations: int = 3
    recheck_seconds: float = 60
    gcloud: str = "gcloud"
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    sleep: Callable[[float], None] = time.sleep

    def scan(self, name_prefix: str) -> tuple[residue.ResidueItem, ...]:
        """residue.ResidueScanner.scan 계약을 따른다.

        잔여가 없으면 confirmations 회차를 모두 돌아 그만큼 시간이 걸린다
        (기본 약 2분).
        """
        residue.check_name_prefix(name_prefix)
        for round_number in range(self.confirmations):
            if round_number:
                self.sleep(self.recheck_seconds)
            items = self._search_assets(name_prefix) + self._list_accounts(
                name_prefix
            )
            if items:
                return items
        return ()

    def _search_assets(
        self, name_prefix: str
    ) -> tuple[residue.ResidueItem, ...]:
        assets = self._gcloud_list(
            "asset",
            "search-all-resources",
            f"--scope=projects/{self.project}",
            f'--query=name:"{name_prefix}"',
            keys={"name", "assetType"},
        )
        return tuple(
            residue.ResidueItem(
                cloud="gcp", kind=asset["assetType"], native_id=asset["name"]
            )
            for asset in assets
            if asset["assetType"] != SERVICE_ACCOUNT
            and residue.is_named_with_prefix(
                asset["name"].rsplit("/", 1)[-1], name_prefix
            )
        )

    def _list_accounts(
        self, name_prefix: str
    ) -> tuple[residue.ResidueItem, ...]:
        accounts = self._gcloud_list(
            "iam",
            "service-accounts",
            "list",
            f"--project={self.project}",
            f"--filter=email~^{name_prefix}[-@]",
            keys={"email", "name"},
        )
        return tuple(
            residue.ResidueItem(
                cloud="gcp", kind=SERVICE_ACCOUNT, native_id=account["name"]
            )
            for account in accounts
            if residue.is_named_with_prefix(
                account["email"].split("@", 1)[0], name_prefix
            )
        )

    def _gcloud_list(self, *args: str, keys: set[str]) -> list[dict[str, Any]]:
        """Gcloud 목록 명령을 JSON 으로 돌리고 각 객체에 keys 가 있는지 본다."""
        result = self.run(
            [self.gcloud, *args, "--format=json"],
            capture_output=True,
            text=True,
            check=False,
        )
        command = f"gcloud {args[0]} {args[1]}"
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
            raise residue.ResidueScanError(
                f"{command} 실패(종료 코드 {result.returncode}): "
                + "\n".join(tail)
            )
        try:
            items = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise residue.ResidueScanError(
                f"{command} 결과가 JSON 이 아니다(위치 {error.pos})"
            ) from None
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and keys <= item.keys() for item in items
        ):
            raise residue.ResidueScanError(
                f"{command} 결과가 {sorted(keys)} 를 가진 객체 목록이 아니다"
            )
        return items
