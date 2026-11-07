"""대상 앱 배포를 대상 DB(B) 프로필로 다시 배포한다 — 예열(설계 4.18 (e)).

같은 배포의 env 만 바꾼다. 접속 URL Secret 참조를 대상 프로필 Secret 으로,
TLS env(PGSSLMODE·DB_SSL_CA)를 대상 DB 것으로 갈고 새 리비전을 띄운다.
이미지·나머지 env 는 그대로다. 클라우드 호출은 얇게 둔다.

  * Cloud Run: Admin API v2 로 서비스를 읽고 고쳐 PATCH, 작업(operation)이
    끝날 때까지 기다린다. 트래픽은 최신 리비전 100% 로 다시 쓴다.
  * ECS: 같은 family 로 새 작업 정의 리비전을 등록하고 update-service 뒤
    services_stable 을 기다린다. 그다음 PRIMARY 배포가 새 리비전인지 본다.

  spec = target_deploy.load_deploy_spec(path)
  deployer = target_deploy.make_deployer(spec)
  revision = deployer.deploy(target_deploy.TargetProfile(spec.secret_env, env))
"""

import collections.abc
import copy
import dataclasses
import pathlib
import time
from typing import Any, Literal, Protocol

import yaml

_RUN_API = "https://run.googleapis.com/v2/"
_HTTP_TIMEOUT_SECONDS = 30
_POLL_SECONDS = 5.0
_DEPLOY_TIMEOUT_SECONDS = 900.0
_LATEST_ALL = {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}
# describe_task_definition 결과 중 register_task_definition 이 받는 키.
_REGISTERABLE = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "ephemeralStorage",
    "runtimePlatform",
)

Platform = Literal["cloud_run", "ecs"]
# 플랫폼이 사는 클라우드 라벨(원가연 job.source·target).
PLATFORM_CLOUD: dict[str, str] = {"cloud_run": "gcp", "ecs": "aws"}


class DeployError(RuntimeError):
    """재배포가 실패했거나 기한 안에 끝나지 않았다."""


@dataclasses.dataclass(frozen=True)
class SecretEnv:
    """Secret 참조 env 하나. 값이 아니라 참조다.

    Attributes:
        name: env 이름(예: DATABASE_URL).
        secret: Cloud Run 은 Secret Manager 비밀 ID, ECS 는 Secret ARN.
        version: Cloud Run 버전(예: "3"). ECS 는 None(버전은 ARN 에).
    """

    name: str
    secret: str
    version: str | None = None


@dataclasses.dataclass(frozen=True)
class TargetProfile:
    """대상 프로필로 바꿀 env. plain_env 는 비밀이 아닌 (이름, 값)."""

    secret_env: tuple[SecretEnv, ...]
    plain_env: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class DeploySpec:
    """target-deploy.yaml 내용.

    Attributes:
        platform: cloud_run 또는 ecs.
        service: Cloud Run 서비스 전체 이름 또는 ECS 서비스 이름.
        secret_env: 대상 프로필 Secret 참조 env.
        cluster: ECS 클러스터(ECS 만).
        region: ECS 리전(ECS 만).
        container: 고칠 컨테이너 이름. None 이면 첫 컨테이너.
    """

    platform: Platform
    service: str
    secret_env: tuple[SecretEnv, ...]
    cluster: str | None = None
    region: str | None = None
    container: str | None = None


class TargetDeployer(Protocol):
    """대상 앱 배포 하나를 대상 프로필로 다시 배포한다."""

    def service_urls(self) -> tuple[str, ...] | None:
        """배포의 공개 주소들. None 이면 이 플랫폼은 주소를 대조하지 않는다."""

    def deploy(self, profile: TargetProfile) -> str:
        """다시 배포하고 새 리비전(작업 정의 ARN)을 돌려준다."""


class CloudRunDeployer:
    """Cloud Run 서비스 재배포. session 은 google.auth AuthorizedSession."""

    def __init__(
        self,
        session: Any,
        service: str,
        *,
        container: str | None = None,
        sleep: collections.abc.Callable[[float], None] = time.sleep,
        clock: collections.abc.Callable[[], float] = time.monotonic,
        timeout_seconds: float = _DEPLOY_TIMEOUT_SECONDS,
    ) -> None:
        """서비스 전체 이름(projects/…/services/…)을 받는다."""
        self._session = session
        self._service = service
        self._container = container
        self._sleep = sleep
        self._clock = clock
        self._timeout = timeout_seconds

    def service_urls(self) -> tuple[str, ...]:
        """서비스의 uri 와 urls(결정적 주소 포함)."""
        service = self._get(self._service)
        return tuple(
            url for url in (service.get("uri"), *service.get("urls", [])) if url
        )

    def deploy(self, profile: TargetProfile) -> str:
        """Env 를 바꿔 PATCH 하고, 작업이 끝나면 최신 준비 리비전을 돌려준다.

        Raises:
            DeployError: 작업이 오류로 끝났거나 기한을 넘겼을 때.
        """
        service = self._get(self._service)
        template = service["template"]
        # 이름을 고정한 리비전이면 새 리비전을 만들 수 없으니 이름을 비운다.
        template.pop("revision", None)
        container = _pick(template["containers"], self._container)
        env = [
            *(
                {
                    "name": item.name,
                    "valueSource": {
                        "secretKeyRef": {
                            "secret": item.secret,
                            "version": item.version or "latest",
                        }
                    },
                }
                for item in profile.secret_env
            ),
            *({"name": n, "value": v} for n, v in profile.plain_env),
        ]
        container["env"] = _merge_by_name(container.get("env", []), env)
        # 옛 리비전(A 프로필)에 남은 분할이 없게 새 리비전에 100% 를 준다.
        service["traffic"] = [dict(_LATEST_ALL)]
        response = self._session.patch(
            _RUN_API + self._service,
            json=service,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        self._wait(response.json())
        return self._get(self._service).get("latestReadyRevision", "")

    def _get(self, name: str) -> dict[str, Any]:
        response = self._session.get(
            _RUN_API + name, timeout=_HTTP_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()

    def _wait(self, operation: dict[str, Any]) -> None:
        deadline = self._clock() + self._timeout
        while not operation.get("done"):
            if self._clock() >= deadline:
                raise DeployError("Cloud Run 재배포가 시간 안에 끝나지 않았다")
            self._sleep(_POLL_SECONDS)
            operation = self._get(operation["name"])
        if "error" in operation:
            message = operation["error"].get("message", "unknown")
            raise DeployError(f"Cloud Run 재배포 실패: {message}")


class EcsDeployer:
    """ECS 서비스 재배포. client 는 boto3 ecs 클라이언트."""

    def __init__(
        self,
        client: Any,
        cluster: str,
        service: str,
        *,
        container: str | None = None,
        waiter_delay: int = 15,
        waiter_attempts: int = 60,
    ) -> None:
        """클러스터와 서비스 이름을 받는다."""
        self._client = client
        self._cluster = cluster
        self._service = service
        self._container = container
        self._waiter = {"Delay": waiter_delay, "MaxAttempts": waiter_attempts}

    def service_urls(self) -> None:
        """ALB 를 따라가야 해서 대조하지 않는다(신원 확인이 대신 잡는다)."""
        return None

    def deploy(self, profile: TargetProfile) -> str:
        """같은 family 새 리비전을 등록하고 서비스를 갱신해 안정을 기다린다.

        Raises:
            ValueError: Secret 에 version 을 줬을 때(ECS 는 ARN 에 담는다).
            DeployError: 안정 뒤 PRIMARY 배포가 새 작업 정의가 아닐 때.
        """
        if any(item.version for item in profile.secret_env):
            raise ValueError("ECS Secret env 는 version 대신 ARN 을 쓴다")
        (service,) = self._client.describe_services(
            cluster=self._cluster, services=[self._service]
        )["services"]
        current = self._client.describe_task_definition(
            taskDefinition=service["taskDefinition"]
        )["taskDefinition"]
        definition = {
            key: copy.deepcopy(current[key])
            for key in _REGISTERABLE
            if key in current
        }
        container = _pick(definition["containerDefinitions"], self._container)
        container["secrets"] = _merge_by_name(
            container.get("secrets", []),
            [
                {"name": s.name, "valueFrom": s.secret}
                for s in profile.secret_env
            ],
        )
        container["environment"] = _merge_by_name(
            container.get("environment", []),
            [{"name": n, "value": v} for n, v in profile.plain_env],
        )
        registered = self._client.register_task_definition(**definition)
        new_arn = registered["taskDefinition"]["taskDefinitionArn"]
        self._client.update_service(
            cluster=self._cluster,
            service=self._service,
            taskDefinition=new_arn,
        )
        self._client.get_waiter("services_stable").wait(
            cluster=self._cluster,
            services=[self._service],
            WaiterConfig=self._waiter,
        )
        # 회로 차단기가 옛 작업 정의로 되돌려도 서비스는 안정이 된다.
        primary = self._primary_task_definition()
        if primary != new_arn:
            raise DeployError(f"ECS 가 {primary} 로 되돌아갔다(새 {new_arn})")
        return new_arn

    def _primary_task_definition(self) -> str | None:
        (service,) = self._client.describe_services(
            cluster=self._cluster, services=[self._service]
        )["services"]
        return next(
            (
                d.get("taskDefinition")
                for d in service.get("deployments", [])
                if d.get("status") == "PRIMARY"
            ),
            None,
        )


def load_deploy_spec(path: pathlib.Path) -> DeploySpec:
    """target-deploy.yaml 을 읽는다.

    Raises:
        ValueError: 플랫폼·서비스·Secret env 가 없거나 플랫폼에 안 맞을 때.
    """
    document = yaml.safe_load(path.read_text()) or {}
    platform = document.get("platform")
    if platform not in ("cloud_run", "ecs"):
        raise ValueError("platform 은 cloud_run 또는 ecs")
    if not document.get("service"):
        raise ValueError("service 가 없다")
    raw_env = document.get("secret_env") or {}
    if not raw_env:
        raise ValueError("secret_env 가 비었다(대상 프로필 Secret 참조)")
    secret_env = tuple(
        SecretEnv(name, str(ref["secret"]), _optional(ref.get("version")))
        for name, ref in raw_env.items()
    )
    if platform == "ecs" and not (
        document.get("cluster") and document.get("region")
    ):
        raise ValueError("ecs 는 cluster·region 이 있어야 한다")
    return DeploySpec(
        platform=platform,
        service=str(document["service"]),
        secret_env=secret_env,
        cluster=_optional(document.get("cluster")),
        region=_optional(document.get("region")),
        container=_optional(document.get("container")),
    )


def make_deployer(spec: DeploySpec) -> TargetDeployer:
    """기본 자격 증명으로 실제 클라우드 deployer 를 만든다."""
    if spec.platform == "ecs":
        import boto3  # 쓰는 플랫폼의 SDK 만 불러온다.

        client = boto3.client("ecs", region_name=spec.region)
        return EcsDeployer(
            client, spec.cluster, spec.service, container=spec.container
        )
    import google.auth
    from google.auth.transport import requests as auth_requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = auth_requests.AuthorizedSession(credentials)
    return CloudRunDeployer(session, spec.service, container=spec.container)


def _pick(containers: list[dict[str, Any]], name: str | None) -> dict[str, Any]:
    if name is None:
        return containers[0]
    for container in containers:
        if container.get("name") == name:
            return container
    raise DeployError(f"컨테이너 {name} 이 없다")


def _merge_by_name(
    current: list[dict[str, Any]], updates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """같은 이름은 그 자리에서 바꾸고, 없던 이름은 뒤에 붙인다."""
    by_name = {item["name"]: item for item in updates}
    merged = [by_name.pop(item["name"], item) for item in current]
    return merged + list(by_name.values())


def _optional(value: Any) -> str | None:
    return None if value in (None, "") else str(value)
