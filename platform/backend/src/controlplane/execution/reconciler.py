"""Shared durable polling and error boundary; Provider workers own their phases."""

import logging

from controlplane.domain.models import Runtime
from controlplane.execution.deletions import tick_deletion
from controlplane.execution.image_preflight import prepare_public_image
from controlplane.providers.contracts import ProviderUnavailable, SubmissionUncertain
from controlplane.repositories.releases import ReleaseStore

LOG = logging.getLogger(__name__)
MAX_TRANSPORT_RETRIES = 5
MAX_BACKOFF_SECONDS = 30


class ReleaseReconciler:
    queue_runtime: Runtime

    def __init__(self, store: ReleaseStore, runtime):
        self.store = store
        self.runtime = runtime

    def tick(self):
        env = self.runtime.settings.environment
        with self.store.worker_lock() as conn:
            if conn is None:
                return
            delete = getattr(self, "_delete", None)
            if delete and tick_deletion(
                conn,
                env.environment_id,
                delete,
                poll_seconds=self.runtime.settings.poll_seconds,
            ):
                return
            row = self.store.next(
                conn,
                runtime=self.queue_runtime,
                application=env.application,
                environment_id=env.environment_id,
            )
            if row is not None:
                self._advance(conn, row)

    def _advance(self, conn, row):
        try:
            if prepare_public_image(self.store, conn, row):
                self._step(conn, row)
        except (ProviderUnavailable, SubmissionUncertain) as exc:
            self._retry_transport(conn, row, exc)
        except Exception as exc:  # noqa: BLE001 -- process boundary, no payload/credential logging
            LOG.error(
                "release=%s state=%s exception=%s", row["id"], row["state"], type(exc).__name__
            )
            self.store.transition(
                conn,
                row,
                "uncertain",
                "요청·권한·외부 상태를 대조해야 합니다. 임의 재실행하지 않습니다.",
                error_code=type(exc).__name__,
            )

    def _retry_transport(self, conn, row, exc):
        cp = dict(row["checkpoint"])
        count = cp.get("transport_retries", 0) + 1
        cp["transport_retries"] = count
        if count <= MAX_TRANSPORT_RETRIES and not row["expired"]:
            delay = min(2**count, MAX_BACKOFF_SECONDS)
            self.store.defer(conn, row, seconds=delay, checkpoint=cp)
        else:
            self.store.transition(
                conn,
                row,
                "uncertain",
                "외부 상태를 확인하지 못했습니다. 운영자 재조회가 필요합니다.",
                checkpoint=cp,
                error_code=type(exc).__name__,
            )

    def _move(self, conn, row, state, message, cp, *, error_code=None, url=None):
        cp["transport_retries"] = 0
        self.store.transition(
            conn, row, state, message, checkpoint=cp, error_code=error_code, url=url
        )

    def _poll(self, conn, row, cp):
        cp["transport_retries"] = 0
        self.store.defer(conn, row, seconds=self.runtime.settings.poll_seconds, checkpoint=cp)

    def _step(self, conn, row):
        raise NotImplementedError
