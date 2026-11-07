"""GCP 잔여 검사 — Cloud Asset Inventory 검색 한 번으로 접두사 자원을 찾는다.

`gcloud asset search-all-resources` 는 프로젝트 안의 모든 자원 종류(Cloud Run·
Cloud SQL·Secret Manager·Artifact Registry·부하 분산기 구성 요소 등)를 한 번에
검색한다. 이름 구문 검색(name:"ccx-r1")으로 후보를 받은 뒤, 전체 리소스 이름의
마지막 조각이 접두사로 시작하는 것만 남긴다 — 구문 검색은 단어 단위라
`team-ccx-r1` 같은 이름도 돌려줄 수 있기 때문이다.

전제: 프로젝트에 cloudasset.googleapis.com 이 켜져 있고, 호출 신원에
cloudasset.assets.searchAllResources 권한이 있다. 검색 색인은 자원 변경을
몇 분 늦게 반영할 수 있다 — destroy 직후 잔여가 보이면 잠시 뒤 다시 본다.
"""

import dataclasses
import json
import subprocess
from collections.abc import Callable

from crosscloud.provision import residue

# 실패 예외에 담는 gcloud 표준 오류 끝 줄 수.
_STDERR_TAIL_LINES = 10


@dataclasses.dataclass(frozen=True)
class GcpResidueScanner:
    """GCP 프로젝트 하나에서 실행 접두사 자원을 찾는다(ResidueScanner 구현).

    Attributes:
        project: GCP 프로젝트 ID.
        gcloud: gcloud 실행 파일.
        run: 명령 실행 함수. 시험에서 바꿔 끼운다.
    """

    project: str
    gcloud: str = "gcloud"
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def scan(self, name_prefix: str) -> tuple[residue.ResidueItem, ...]:
        """residue.ResidueScanner.scan 계약을 따른다."""
        residue.check_name_prefix(name_prefix)
        result = self.run(
            [
                self.gcloud,
                "asset",
                "search-all-resources",
                f"--scope=projects/{self.project}",
                f'--query=name:"{name_prefix}"',
                "--format=json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
            raise residue.ResidueScanError(
                f"gcloud asset 검색 실패(종료 코드 {result.returncode}): "
                + "\n".join(tail)
            )
        return tuple(
            residue.ResidueItem(
                cloud="gcp", kind=asset["assetType"], native_id=asset["name"]
            )
            for asset in _parse_assets(result.stdout)
            if residue.is_named_with_prefix(
                asset["name"].rsplit("/", 1)[-1], name_prefix
            )
        )


def _parse_assets(stdout: str) -> list[dict[str, str]]:
    """JSON 형식 gcloud 결과(자원 객체 목록)를 읽는다."""
    try:
        assets = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise residue.ResidueScanError(
            f"gcloud asset 결과가 JSON 이 아니다(위치 {error.pos})"
        ) from None
    if not isinstance(assets, list) or not all(
        isinstance(asset, dict) and {"name", "assetType"} <= asset.keys()
        for asset in assets
    ):
        raise residue.ResidueScanError(
            "gcloud asset 결과가 name·assetType 을 가진 객체 목록이 아니다"
        )
    return assets
