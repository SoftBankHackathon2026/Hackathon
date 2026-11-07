"""GCP 잔여 검사 — 우리가 만드는 자원 종류마다 서비스 API 를 직접 조회한다.

crosscloud GCP 모듈(registry·target_base·target_app)과 감사 버킷이 만드는 자원
종류마다 gcloud 목록 명령을 하나씩 부른다. 서비스 API 직접 조회라 실시간이다.
Cloud Asset 검색은 색인이라 변경을 늦게 반영해, 빈 결과를 잔여 0 근거로 쓸 수
없어 쓰지 않는다.

명령마다 `--filter=<필드>~<접두사>` 로 서버 쪽에서 좁힌 뒤, 이름의 마지막 조각
(서비스 계정은 이메일 앞부분)이 접두사 자체이거나 `접두사-` 로 시작하는 것만
남긴다. 목록 하나라도 실패하면 잔여 0 이 아니라 오류다.

새 자원 종류를 모듈에 더하면 _LISTINGS 에도 더해야 한다 — 이 표에 없는
종류는 잔여 검사가 보지 않는다.

전제: 각 서비스 API 가 프로젝트에 켜져 있고, 호출 신원에 각 목록 권한이
있다(프로젝트 Viewer 로 충분하다).
"""

import dataclasses
import json
import subprocess
from collections.abc import Callable
from typing import Any

from crosscloud.provision import residue

SERVICE_ACCOUNT = "iam.googleapis.com/ServiceAccount"

# 실패 예외에 담는 gcloud 표준 오류 끝 줄 수. 목록 조회라 비밀 입력이 없다.
_STDERR_TAIL_LINES = 10


@dataclasses.dataclass(frozen=True)
class _Listing:
    """자원 종류 하나를 조회하는 gcloud 목록 명령.

    Attributes:
        kind: ResidueItem.kind 에 넣는 자원 종류(Cloud Asset 형식 이름).
        words: gcloud 명령 단어(list 앞까지).
        regional: 리전을 받는 명령이면 그 플래그 이름(--region 등).
        filter_field: 서버 쪽 filter 를 걸 필드.
        name_path: 목록 항목에서 이름을 읽는 점 구분 경로.
    """

    kind: str
    words: tuple[str, ...]
    regional: str | None = None
    filter_field: str = "name"
    name_path: str = "name"


_LISTINGS = (
    _Listing("sqladmin.googleapis.com/Instance", ("sql", "instances")),
    _Listing(
        "run.googleapis.com/Service",
        ("run", "services"),
        regional="--region",
        filter_field="metadata.name",
        name_path="metadata.name",
    ),
    _Listing(
        "artifactregistry.googleapis.com/Repository",
        ("artifacts", "repositories"),
        regional="--location",
    ),
    _Listing("secretmanager.googleapis.com/Secret", ("secrets",)),
    _Listing(
        SERVICE_ACCOUNT,
        ("iam", "service-accounts"),
        filter_field="email",
        name_path="email",
    ),
    _Listing(
        "compute.googleapis.com/ForwardingRule", ("compute", "forwarding-rules")
    ),
    _Listing(
        "compute.googleapis.com/TargetHttpProxy",
        ("compute", "target-http-proxies"),
    ),
    _Listing("compute.googleapis.com/UrlMap", ("compute", "url-maps")),
    _Listing(
        "compute.googleapis.com/BackendService", ("compute", "backend-services")
    ),
    _Listing(
        "compute.googleapis.com/NetworkEndpointGroup",
        ("compute", "network-endpoint-groups"),
    ),
    _Listing("compute.googleapis.com/Address", ("compute", "addresses")),
    _Listing("storage.googleapis.com/Bucket", ("storage", "buckets")),
)


@dataclasses.dataclass(frozen=True)
class GcpResidueScanner:
    """GCP 프로젝트 하나에서 실행 접두사 자원을 찾는다(ResidueScanner 구현).

    Attributes:
        project: GCP 프로젝트 ID.
        region: 리전 단위 자원(Cloud Run·Artifact Registry)을 볼 리전. 모듈의
            region 입력과 같아야 한다.
        gcloud: gcloud 실행 파일.
        run: 명령 실행 함수. 시험에서 바꿔 끼운다.
    """

    project: str
    region: str
    gcloud: str = "gcloud"
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def scan(self, name_prefix: str) -> tuple[residue.ResidueItem, ...]:
        """residue.ResidueScanner.scan 계약을 따른다."""
        residue.check_name_prefix(name_prefix)
        return tuple(
            item
            for listing in _LISTINGS
            for item in self._scan_one(listing, name_prefix)
        )

    def _scan_one(
        self, listing: _Listing, name_prefix: str
    ) -> tuple[residue.ResidueItem, ...]:
        args = [
            self.gcloud,
            *listing.words,
            "list",
            f"--project={self.project}",
        ]
        if listing.regional:
            args.append(f"{listing.regional}={self.region}")
        args += [
            f"--filter={listing.filter_field}~{name_prefix}",
            "--format=json",
        ]
        command = "gcloud " + " ".join(listing.words)
        names = _names(command, self._run(command, args), listing.name_path)
        return tuple(
            residue.ResidueItem(cloud="gcp", kind=listing.kind, native_id=name)
            for name in names
            if residue.is_named_with_prefix(_short_name(name), name_prefix)
        )

    def _run(self, command: str, args: list[str]) -> str:
        result = self.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
            raise residue.ResidueScanError(
                f"{command} 실패(종료 코드 {result.returncode}): "
                + "\n".join(tail)
            )
        return result.stdout


def _names(command: str, stdout: str, name_path: str) -> list[str]:
    """JSON 목록 결과에서 각 항목의 이름(name_path)을 읽는다."""
    try:
        items = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise residue.ResidueScanError(
            f"{command} 결과가 JSON 이 아니다(위치 {error.pos})"
        ) from None
    if not isinstance(items, list):
        raise residue.ResidueScanError(f"{command} 결과가 목록이 아니다")
    names = [_lookup(item, name_path) for item in items]
    if not all(isinstance(name, str) for name in names):
        raise residue.ResidueScanError(
            f"{command} 결과 항목에 문자열 {name_path} 가 없다"
        )
    return names


def _lookup(item: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(item, dict):
            return None
        item = item.get(key)
    return item


def _short_name(name: str) -> str:
    """전체 리소스 이름의 마지막 조각. 서비스 계정 이메일은 @ 앞부분."""
    return name.rsplit("/", 1)[-1].split("@", 1)[0]
