from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import HTTPError as Urllib3Error

from controlplane.core.errors import (
    AccessDenied,
    InvalidRequest,
    OperationNotFound,
    ProviderReadFailed,
)
from controlplane.domain.fleet import EnvironmentConfig
from controlplane.domain.topology import LOG_LAYERS
from controlplane.integrations.kube import api_client
from controlplane.schemas.migrations import PodLogLine, PodLogs
from controlplane.services.fleet import FleetService
from controlplane.services.log_redaction import redact_lines

TAIL_CHOICES = (200, 500, 1000)
LOG_BYTES = 512 * 1024
LOG_TIMEOUT = (3, 10)

LogReader = Callable[..., str]


def read_cluster_log(
    env: EnvironmentConfig, namespace: str, pod: str, container: str, *, tail: int, previous: bool
) -> str:
    api = api_client(env)
    try:
        return client.CoreV1Api(api).read_namespaced_pod_log(
            pod,
            namespace,
            container=container,
            tail_lines=tail,
            timestamps=True,
            previous=previous,
            limit_bytes=LOG_BYTES,
            _request_timeout=LOG_TIMEOUT,
        )
    finally:
        api.close()


def simulated_log(
    env: EnvironmentConfig, namespace: str, pod: str, container: str, *, tail: int, previous: bool
) -> str:
    start = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    messages = [
        f"starting {container} in {namespace}",
        "connected to postgres://app:demo-password@postgres:5432/app",
        "GET /healthz 200 2ms",
        "WARN slow query took 812ms",
        "GET /api/orders 200 14ms",
        "ERROR upstream timeout after 5000ms",
        "GET /healthz 200 1ms",
    ]
    lines = [
        f"{(start + timedelta(seconds=15 * i)).isoformat().replace('+00:00', 'Z')} "
        f"{messages[i % len(messages)]}"
        for i in range(60)
    ]
    return "\n".join(lines[-tail:]) + "\n"


def parse_line(line: str) -> PodLogLine:
    stamp, _, message = line.partition(" ")
    try:
        return PodLogLine(at=datetime.fromisoformat(stamp.replace("Z", "+00:00")), message=message)
    except ValueError:
        return PodLogLine(at=None, message=line)


class PodLogService:
    def __init__(
        self,
        fleet: FleetService,
        reader: LogReader = read_cluster_log,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fleet = fleet
        self._reader = reader
        self._clock = clock

    def logs(
        self,
        env_id: str,
        component_id: str,
        pod_name: str,
        *,
        container: str | None = None,
        tail: int = TAIL_CHOICES[0],
        previous: bool = False,
    ) -> PodLogs:
        found = self._fleet.environment(env_id)
        if found is None:
            raise OperationNotFound("환경을 찾지 못했어요.")
        config, view = found
        component = next((c for c in view.components if c.id == component_id), None)
        if component is None:
            raise OperationNotFound("구성 요소를 찾지 못했어요.")
        if component.layer not in LOG_LAYERS or not component.logs or not component.namespace:
            raise AccessDenied(
                "이 구성 요소는 로그를 볼 수 없어요. 앱·데이터·이전 도구만 열어 둬요."
            )
        pod = next((p for p in component.pods or [] if p.name == pod_name), None)
        if pod is None:
            raise OperationNotFound("파드를 찾지 못했어요. 목록이 바뀌었을 수 있어요.")
        names = [c.name for c in pod.containers]
        chosen = container or (names[0] if names else None)
        if chosen is None or chosen not in names:
            raise OperationNotFound("컨테이너를 찾지 못했어요.")
        if tail not in TAIL_CHOICES:
            raise InvalidRequest("줄 수는 200, 500, 1000 중에서 고르세요.")
        raw = self._read(config, component.namespace, pod_name, chosen, tail, previous)
        lines, redacted = redact_lines(raw.splitlines())
        parsed = [parse_line(line) for line in lines if line]
        return PodLogs(
            env_id=env_id,
            component_id=component_id,
            pod=pod_name,
            container=chosen,
            containers=names,
            previous=previous,
            tail=tail,
            lines=parsed,
            truncated=len(raw.encode()) >= LOG_BYTES,
            redacted=redacted,
            fetched_at=self._clock(),
        )

    def _read(
        self,
        config: EnvironmentConfig,
        namespace: str,
        pod: str,
        container: str,
        tail: int,
        previous: bool,
    ) -> str:
        try:
            return self._reader(config, namespace, pod, container, tail=tail, previous=previous)
        except ApiException as exc:
            if exc.status == 400 and previous:
                raise OperationNotFound("재시작 전 컨테이너 로그가 없어요.") from None
            if exc.status == 404:
                raise OperationNotFound("파드가 더 이상 없어요. 목록이 곧 새로 고쳐져요.") from None
            if exc.status == 403:
                raise ProviderReadFailed(
                    "이 환경의 읽기 계정에 로그 권한(pods/log)이 없어요."
                ) from None
            raise ProviderReadFailed("클러스터에서 로그를 읽지 못했어요.") from None
        except (ConfigException, OSError, Urllib3Error):
            raise ProviderReadFailed(
                "클러스터에 연결하지 못했어요. 잠시 후 다시 시도하세요."
            ) from None
