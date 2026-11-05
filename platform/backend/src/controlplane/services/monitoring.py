from collections.abc import Callable, Hashable
from enum import StrEnum
from typing import Any

import anyio
from starlette.concurrency import run_in_threadpool

from controlplane.core.errors import (
    InvalidRequest,
    OperationNotFound,
    PlatformError,
    ProviderReadFailed,
)
from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable
from controlplane.providers.interface import AppLogs
from controlplane.providers.telemetry import TraceDetail, TraceKind, TraceList, Traffic
from controlplane.schemas.accounts import Identity
from controlplane.services.deployments import DeploymentService
from controlplane.services.read_cache import ReadCache

DEFAULT_MAX_LOG_MINUTES = 360
READ_THREADS = 16
LOG_FAILURE = "클라우드 로그를 불러오지 못했습니다. 잠시 후 다시 시도하세요."
TELEMETRY_FAILURE = "클라우드 지표를 불러오지 못했습니다. 잠시 후 다시 시도하세요."
TRACE_MISSING = "이 앱의 trace가 없습니다. 수집 직후라면 잠시 후 다시 조회하세요."


class ReadKind(StrEnum):
    LOGS = "logs"
    TRAFFIC = "traffic"
    TRACES = "traces"
    TRACE = "trace"


READ_TTL_SECONDS = {
    ReadKind.LOGS: 2,
    ReadKind.TRAFFIC: 3,
    ReadKind.TRACES: 3,
    ReadKind.TRACE: 10,
}


class MonitoringService:
    def __init__(self, deployments: DeploymentService) -> None:
        self._deployments = deployments
        self._cache = ReadCache()
        self._threads = anyio.CapacityLimiter(READ_THREADS)

    def forget_environment(self, environment_id: str) -> None:
        self._cache.forget(lambda key: key[1] == environment_id)

    async def _read(
        self,
        kind: ReadKind,
        key: tuple[Hashable, ...],
        environment_id: str,
        open_provider: Callable[[str, Identity | None], Any],
        identity: Identity | None,
        read: Callable[[Any], Any],
        failure: str,
    ) -> Any:
        try:
            provider = await run_in_threadpool(open_provider, environment_id, identity)
            return await self._cache.get(
                (kind, environment_id, *key),
                READ_TTL_SECONDS[kind],
                lambda: anyio.to_thread.run_sync(read, provider, limiter=self._threads),
            )
        except PlatformError:
            raise
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        except (ProviderRejected, ProviderUnavailable):
            raise ProviderReadFailed(failure) from None

    async def logs(
        self,
        environment_id: str,
        identity: Identity | None,
        *,
        minutes: int,
        query: str | None,
        limit: int,
    ) -> AppLogs:
        def read_logs(provider: Any) -> AppLogs:
            if minutes > getattr(provider, "max_log_minutes", DEFAULT_MAX_LOG_MINUTES):
                raise ValueError("이 환경의 로그 조회 가능 기간을 넘었습니다.")
            return provider.app_logs(minutes=minutes, filter_text=query, limit=limit)

        return await self._read(
            ReadKind.LOGS,
            (minutes, query, limit),
            environment_id,
            self._deployments.log_provider,
            identity,
            read_logs,
            LOG_FAILURE,
        )

    async def _telemetry(
        self,
        kind: ReadKind,
        key: tuple[Hashable, ...],
        environment_id: str,
        identity: Identity | None,
        read: Callable[[Any], Any],
    ) -> Any:
        return await self._read(
            kind,
            key,
            environment_id,
            self._deployments.telemetry,
            identity,
            read,
            TELEMETRY_FAILURE,
        )

    async def traffic(
        self, environment_id: str, identity: Identity | None, *, minutes: int
    ) -> Traffic:
        return await self._telemetry(
            ReadKind.TRAFFIC,
            (minutes,),
            environment_id,
            identity,
            lambda provider: provider.traffic(minutes=minutes),
        )

    async def traces(
        self, environment_id: str, identity: Identity | None, *, minutes: int, kind: TraceKind
    ) -> TraceList:
        return await self._telemetry(
            ReadKind.TRACES,
            (minutes, kind),
            environment_id,
            identity,
            lambda provider: provider.traces(minutes=minutes, kind=kind),
        )

    async def trace(
        self, environment_id: str, identity: Identity | None, trace_id: str
    ) -> TraceDetail:
        found = await self._telemetry(
            ReadKind.TRACE,
            (trace_id,),
            environment_id,
            identity,
            lambda provider: provider.trace(trace_id),
        )
        if found is None:
            raise OperationNotFound(TRACE_MISSING)
        return found
