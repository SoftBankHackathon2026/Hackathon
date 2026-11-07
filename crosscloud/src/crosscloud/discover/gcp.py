"""GCP 한 프로젝트·리전의 구성을 Inventory 로 읽는다(최소판).

읽는 것: Cloud Run 서비스, Cloud SQL 인스턴스, Secret Manager 메타, Artifact
Registry 저장소, 전역 전달 규칙(외부 LB). 모두 REST 의 list 호출이고 Secret
값(`:access`)은 부르지 않는다. 지원하지 않는 형태는 Omission 으로, 권한이
없는 영역은 permission_denied Omission 으로 남긴다.

  reader = gcp.GcpInventoryReader(project="p", region="asia-northeast3")
  inventory = reader.read()
"""

import datetime
from collections.abc import Callable, Iterator
from typing import Any

import google.auth
from google.auth.transport import requests as auth_requests

from crosscloud import discover, model
from crosscloud.discover import env

# GET 한 번을 맡는 전송. (URL, 쿼리) 를 받아 JSON 본문(dict)을 돌려준다.
GetJson = Callable[[str, dict[str, str]], dict[str, Any]]

_HTTP_FORBIDDEN = 403


class GcpPermissionDeniedError(Exception):
    """GCP API 가 권한 부족(403)으로 거절했다."""


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def authorized_get_json() -> GetJson:
    """기본 자격 증명(ADC)으로 GET 하는 전송을 만든다.

    Returns:
        403 이면 GcpPermissionDeniedError, 그 밖의 실패 응답이면
        requests.HTTPError 를 올리는 GetJson.
    """
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = auth_requests.AuthorizedSession(credentials)

    def get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
        response = session.get(url, params=params, timeout=30)
        if response.status_code == _HTTP_FORBIDDEN:
            raise GcpPermissionDeniedError(url)
        response.raise_for_status()
        return response.json()

    return get_json


class GcpInventoryReader:
    """GCP 한 프로젝트·리전의 구성을 읽는다(읽기 전용, 비밀값 미조회)."""

    def __init__(
        self,
        project: str,
        region: str,
        get_json: GetJson | None = None,
        clock: Callable[[], datetime.datetime] = _utc_now,
    ) -> None:
        """읽을 범위와 전송을 정한다.

        Args:
            project: 프로젝트 ID.
            region: 리전(예: asia-northeast3).
            get_json: REST GET 전송. 없으면 authorized_get_json() 을 쓴다.
            clock: 수집 시각을 돌려준다.
        """
        self._project = project
        self._region = region
        self._get_json = get_json or authorized_get_json()
        self._clock = clock

    def read(self) -> model.Inventory:
        """구성을 읽어 Inventory 로 돌려준다.

        Returns:
            읽은 Inventory. IAM 권한·제한 정책·결합은 비어 있다.

        Raises:
            Exception: 권한 부족이 아닌 전송 오류는 그대로 올린다.
        """
        collected_at = self._clock()
        omissions: list[model.Omission] = []
        workloads = tuple(self._guarded("run", self._workloads, omissions))
        resources = tuple(
            resource
            for api, read in (
                ("sqladmin", self._databases),
                ("secretmanager", self._secrets),
                ("artifactregistry", self._registries),
                ("compute", self._load_balancers),
            )
            for resource in self._guarded(api, read, omissions)
        )
        return model.Inventory(
            cloud="gcp",
            collected_at=collected_at,
            resources=resources,
            workloads=workloads,
            grants=(),
            restrictions=(),
            restriction_scans=(),
            couplings=(),
            omissions=tuple(omissions),
            attestation=discover.unattested(workloads),
        )

    def _guarded(
        self,
        api: str,
        read: Callable[[list[model.Omission]], list[Any]],
        omissions: list[model.Omission],
    ) -> list[Any]:
        try:
            return read(omissions)
        except GcpPermissionDeniedError:
            omissions.append(
                model.Omission(f"{api}:*", "permission_denied", None)
            )
            return []

    def _list(self, url: str, key: str) -> Iterator[dict[str, Any]]:
        params: dict[str, str] = {}
        while True:
            page = self._get_json(url, params)
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            params = {"pageToken": token}

    def _workloads(
        self, omissions: list[model.Omission]
    ) -> list[model.Workload]:
        url = (
            "https://run.googleapis.com/v2/projects/"
            f"{self._project}/locations/{self._region}/services"
        )
        workloads = []
        for service in self._list(url, "services"):
            containers = service["template"]["containers"]
            # 사이드카가 있는 서비스는 본선 범위 밖이다.
            if len(containers) != 1:
                omissions.append(
                    model.Omission(
                        f"run:{service['name']}", "unsupported_type", None
                    )
                )
                continue
            workloads.append(_workload(service, containers[0], omissions))
        return workloads

    def _databases(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        url = (
            "https://sqladmin.googleapis.com/v1/projects/"
            f"{self._project}/instances"
        )
        databases = []
        for instance in self._list(url, "items"):
            native_id = f"projects/{self._project}/instances/{instance['name']}"
            version = instance.get("databaseVersion", "")
            if not version.startswith("POSTGRES"):
                omissions.append(
                    model.Omission(
                        f"sqladmin:{native_id}", "unsupported_type", None
                    )
                )
                continue
            settings = instance.get("settings", {})
            flags = {
                flag["name"]: flag.get("value")
                for flag in settings.get("databaseFlags", [])
            }
            databases.append(
                self._resource(
                    "postgres_instance",
                    native_id,
                    region=instance.get("region", self._region),
                    engine="postgres",
                    database_version=version,
                    tier=settings.get("tier"),
                    iam_auth=flags.get("cloudsql.iam_authentication") == "on",
                    public_ip=settings.get("ipConfiguration", {}).get(
                        "ipv4Enabled"
                    ),
                )
            )
        return databases

    def _secrets(self, omissions: list[model.Omission]) -> list[model.Resource]:
        url = (
            "https://secretmanager.googleapis.com/v1/projects/"
            f"{self._project}/secrets"
        )
        return [
            self._resource(
                "secret",
                secret["name"],
                region="global",
                name=secret["name"].rsplit("/", 1)[-1],
            )
            for secret in self._list(url, "secrets")
        ]

    def _registries(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        url = (
            "https://artifactregistry.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._region}/repositories"
        )
        registries = []
        for repository in self._list(url, "repositories"):
            if repository.get("format") != "DOCKER":
                omissions.append(
                    model.Omission(
                        f"artifactregistry:{repository['name']}",
                        "unsupported_type",
                        None,
                    )
                )
                continue
            registries.append(
                self._resource(
                    "container_registry",
                    repository["name"],
                    region=self._region,
                    format="DOCKER",
                )
            )
        return registries

    def _load_balancers(
        self, omissions: list[model.Omission]
    ) -> list[model.Resource]:
        url = (
            "https://compute.googleapis.com/compute/v1/projects/"
            f"{self._project}/global/forwardingRules"
        )
        balancers = []
        for rule in self._list(url, "items"):
            native_id = (
                f"projects/{self._project}/global/forwardingRules/"
                f"{rule['name']}"
            )
            scheme = rule.get("loadBalancingScheme")
            if scheme != "EXTERNAL_MANAGED":
                omissions.append(
                    model.Omission(
                        f"compute:{native_id}", "unsupported_type", None
                    )
                )
                continue
            balancers.append(
                self._resource(
                    "load_balancer",
                    native_id,
                    region="global",
                    load_balancing_scheme=scheme,
                    ip_address=rule.get("IPAddress"),
                )
            )
        return balancers

    def _resource(
        self,
        kind: model.ResourceKind,
        native_id: str,
        region: str,
        **attributes: str | int | bool | None,
    ) -> model.Resource:
        return model.Resource(
            kind=kind,
            cloud="gcp",
            native_id=native_id,
            region=region,
            attributes=tuple(
                model.KeyValue(key, value) for key, value in attributes.items()
            ),
            source_of_truth="api",
        )


def _workload(
    service: dict[str, Any],
    container: dict[str, Any],
    omissions: list[model.Omission],
) -> model.Workload:
    name = service["name"].rsplit("/", 1)[-1]
    env_entries = []
    secret_refs = []
    for variable in container.get("env", []):
        secret = variable.get("valueSource", {}).get("secretKeyRef")
        if secret is None:
            env_entries.append(
                env.classify_env(variable["name"], variable.get("value", ""))
            )
            continue
        ref = model.SecretRef(
            cloud="gcp",
            native_id=secret["secret"],
            version=secret.get("version", "latest"),
        )
        secret_refs.append(ref)
        env_entries.append(env.classify_env(variable["name"], ref))
    return model.Workload(
        name=name,
        cloud="gcp",
        platform="cloud_run",
        native_id=service["name"],
        identity_id=service["template"].get("serviceAccount", ""),
        image=_image(name, container["image"], omissions),
        code_repo=None,
        entries=(
            model.Entry(
                kind="http",
                source_id=service.get("uri", service["name"]),
                attributes=(model.KeyValue("ingress", service.get("ingress")),),
            ),
        ),
        env=tuple(env_entries),
        secret_refs=tuple(secret_refs),
        db_user=None,
        depends_on=tuple(ref.native_id for ref in secret_refs),
    )


def _image(
    name: str, image: str, omissions: list[model.Omission]
) -> model.ImageRef | None:
    # 최소판은 digest 로 고정한 이미지만 받는다. 태그 이미지는 사람이 확인한다.
    if "@" not in image:
        omissions.append(
            model.Omission(f"image:{name}", "unsupported_type", None)
        )
        return None
    repository, digest = image.split("@", 1)
    return model.ImageRef(registry_repo=repository, digest=digest)
