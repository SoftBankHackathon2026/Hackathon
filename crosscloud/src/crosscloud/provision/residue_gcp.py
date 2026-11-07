"""GCP 잔여 검사 — 우리가 만드는 자원 종류마다 서비스 API 를 직접 조회한다.

잔여 검사는 보조 확인이다. 정리됐다는 1차 근거는 terraform destroy 뒤 state 가
빈 것이다(crosscloud.provision.terraform). 여기서는 state 밖에 남은 자원(생성
응답 유실 등)을 이름 접두사로 한 번 더 찾는다.

crosscloud GCP 모듈(registry·target_base·target_app)과 감사 버킷이 만드는 자원
종류마다 목록을 하나씩 조회한다. Cloud Asset 검색은 색인이라 늦게 반영돼 쓰지
않는다.

*   gcloud 목록 명령 11개: `--filter=<필드>~<접두사>` 로 서버 쪽에서 좁히고,
    위치를 받는 명령은 모든 위치를 본다(Cloud Run 은 --region 생략,
    Artifact Registry 는 --location=all). 부분 실패를 성공으로 보지 않도록
    compute 부분 오류 허용을 끄고, 표준 오류에 경고·도달 불가 문구가 있으면
    오류로 본다(내용은 싣지 않는다).
*   Cloud SQL: `gcloud sql instances list` 는 응답의 REGION_UNREACHABLE 경고를
    버린다. 그래서 SQL Admin REST 목록을 curl 로 직접 받아 warnings 를 본다.
    접근 토큰은 `gcloud auth print-access-token` 으로 받아 curl 표준 입력의
    헤더로만 넘긴다(명령줄·예외에 남기지 않는다).

이름의 마지막 조각(서비스 계정은 이메일 앞부분)이 접두사 자체이거나 `접두사-`
로 시작하는 것만 잔여다. 목록 하나라도 실패하면 잔여 0 이 아니라 오류다.

한계: Secret Manager 목록은 최종 일관성이라 막 만든 비밀이 잠시 빠질 수 있다.
새 자원 종류를 모듈에 더하면 _LISTINGS 에도 더해야 한다 — 이 표에 없는 종류는
보지 않는다.

전제: 각 서비스 API 가 켜져 있고, 호출 신원에 각 목록 권한이 있다(프로젝트
Viewer 로 충분하다). curl 이 PATH 에 있다.
"""

import dataclasses
import json
import os
import subprocess
import urllib.parse
from collections.abc import Callable
from typing import Any

from crosscloud.provision import residue

SERVICE_ACCOUNT = "iam.googleapis.com/ServiceAccount"
CLOUD_SQL_INSTANCE = "sqladmin.googleapis.com/Instance"

_SQL_ADMIN_URL = "https://sqladmin.googleapis.com/v1/projects/{}/instances"

# 실패 예외에 담는 gcloud 표준 오류 끝 줄 수. 목록 조회라 비밀 입력이 없다.
_STDERR_TAIL_LINES = 10

# 목록이 일부만 왔다는 뜻의 표준 오류 문구(소문자). gcloud 는 이때도 종료
# 코드 0 을 낼 수 있다(Cloud Run 미응답 리전, Artifact Registry 5xx 위치 등).
_PARTIAL_RESULT_MARKERS = (
    "warning",
    "did not succeed",
    "did not respond",
    "unreachable",
    "incomplete",
)


@dataclasses.dataclass(frozen=True)
class _Listing:
    """자원 종류 하나를 조회하는 gcloud 목록 명령.

    Attributes:
        kind: ResidueItem.kind 에 넣는 자원 종류(Cloud Asset 형식 이름).
        words: gcloud 명령 단어(list 앞까지).
        flags: 고정 플래그(모든 위치 조회 등).
        filter_field: 서버 쪽 filter 를 걸 필드.
        name_path: 목록 항목에서 이름을 읽는 점 구분 경로.
    """

    kind: str
    words: tuple[str, ...]
    flags: tuple[str, ...] = ()
    filter_field: str = "name"
    name_path: str = "name"


_LISTINGS = (
    _Listing(
        "run.googleapis.com/Service",
        ("run", "services"),
        filter_field="metadata.name",
        name_path="metadata.name",
    ),
    _Listing(
        "artifactregistry.googleapis.com/Repository",
        ("artifacts", "repositories"),
        flags=("--location=all",),
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
        gcloud: gcloud 실행 파일.
        curl: curl 실행 파일.
        run: 명령 실행 함수. 시험에서 바꿔 끼운다.
    """

    project: str
    gcloud: str = "gcloud"
    curl: str = "curl"
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def scan(self, name_prefix: str) -> tuple[residue.ResidueItem, ...]:
        """residue.ResidueScanner.scan 계약을 따른다."""
        residue.check_name_prefix(name_prefix)
        listed = tuple(
            item
            for listing in _LISTINGS
            for item in self._scan_listing(listing, name_prefix)
        )
        return listed + self._scan_cloud_sql(name_prefix)

    def _scan_listing(
        self, listing: _Listing, name_prefix: str
    ) -> tuple[residue.ResidueItem, ...]:
        command = "gcloud " + " ".join(listing.words)
        result = self._execute(
            command,
            [
                self.gcloud,
                *listing.words,
                "list",
                f"--project={self.project}",
                *listing.flags,
                f"--filter={listing.filter_field}~{name_prefix}",
                "--format=json",
            ],
        )
        _reject_partial_result(command, result.stderr)
        items = _load_json(command, result.stdout, list)
        names = _names(command, items, listing.name_path)
        return _matching(listing.kind, names, name_prefix)

    def _scan_cloud_sql(
        self, name_prefix: str
    ) -> tuple[residue.ResidueItem, ...]:
        command = "Cloud SQL 목록"
        token = self._execute(
            command, [self.gcloud, "auth", "print-access-token"], quiet=True
        ).stdout.strip()
        url = _SQL_ADMIN_URL.format(urllib.parse.quote(self.project, safe=""))
        names: list[str] = []
        page_url = url
        while True:
            result = self._execute(
                command,
                [
                    self.curl,
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--header",
                    "@-",
                    page_url,
                ],
                quiet=True,
                input=f"Authorization: Bearer {token}\n",
            )
            page = _load_json(command, result.stdout, dict)
            warnings = page.get("warnings") or []
            if warnings:
                codes = sorted(
                    {
                        str(w.get("code")) if isinstance(w, dict) else "?"
                        for w in warnings
                    }
                )
                raise residue.ResidueScanError(
                    f"{command} 경고 {codes} — 결과가 불완전할 수 있다"
                )
            names += _names(command, page.get("items", []), "name")
            next_token = page.get("nextPageToken")
            if not next_token:
                return _matching(CLOUD_SQL_INSTANCE, names, name_prefix)
            page_url = f"{url}?pageToken={urllib.parse.quote(next_token)}"

    def _execute(
        self, command: str, args: list[str], *, quiet: bool = False, **kwargs
    ) -> subprocess.CompletedProcess[str]:
        """명령을 돌리고 종료 코드가 0 이 아니면 오류를 낸다.

        quiet 면 표준 오류 내용을 예외에 싣지 않는다(토큰이 섞일 수 있는 호출).
        """
        result = self.run(
            args,
            env={**os.environ, "CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR": "false"},
            capture_output=True,
            text=True,
            check=False,
            **kwargs,
        )
        if result.returncode == 0:
            return result
        detail = (
            ""
            if quiet
            else ": "
            + "\n".join(
                result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
            )
        )
        raise residue.ResidueScanError(
            f"{command} 실패(종료 코드 {result.returncode}){detail}"
        )


def _reject_partial_result(command: str, stderr: str) -> None:
    """표준 오류에 부분 결과 문구가 있으면 오류. 문구 내용은 싣지 않는다."""
    lowered = stderr.lower()
    if any(marker in lowered for marker in _PARTIAL_RESULT_MARKERS):
        raise residue.ResidueScanError(
            f"{command} 이 경고를 냈다 — 결과가 불완전할 수 있다"
            "(표준 오류 내용은 싣지 않음)"
        )


def _load_json(command: str, text: str, expected: type) -> Any:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise residue.ResidueScanError(
            f"{command} 결과가 JSON 이 아니다(위치 {error.pos})"
        ) from None
    if not isinstance(document, expected):
        raise residue.ResidueScanError(
            f"{command} 결과가 {expected.__name__} 형식이 아니다"
        )
    return document


def _names(command: str, items: Any, name_path: str) -> list[str]:
    """목록 항목마다 이름(name_path)을 읽는다."""
    if not isinstance(items, list):
        raise residue.ResidueScanError(f"{command} 항목이 목록이 아니다")
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


def _matching(
    kind: str, names: list[str], name_prefix: str
) -> tuple[residue.ResidueItem, ...]:
    return tuple(
        residue.ResidueItem(cloud="gcp", kind=kind, native_id=name)
        for name in names
        if residue.is_named_with_prefix(_short_name(name), name_prefix)
    )


def _short_name(name: str) -> str:
    """전체 리소스 이름의 마지막 조각. 서비스 계정 이메일은 @ 앞부분."""
    return name.rsplit("/", 1)[-1].split("@", 1)[0]
