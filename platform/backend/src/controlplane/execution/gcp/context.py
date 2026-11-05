from dataclasses import dataclass
from typing import Any

from controlplane.execution.gcp.states import ReleaseState
from controlplane.execution.reconciler import ReleaseReconciler
from controlplane.providers.gcp.models import GcpDeploymentRequest


@dataclass
class StepContext:
    worker: ReleaseReconciler
    conn: Any
    row: dict
    request: GcpDeploymentRequest
    cp: dict
    initial: bool = False

    @property
    def runtime(self):
        return self.worker.runtime

    @property
    def provider(self):
        return self.runtime.provider

    @property
    def settings(self):
        return self.runtime.settings

    @property
    def state(self) -> str:
        return self.row["state"]

    @property
    def release_id(self) -> str:
        return self.row["id"]

    def move(
        self,
        state: ReleaseState,
        message: str,
        *,
        error_code: str | None = None,
        url: str | None = None,
    ) -> None:
        self.worker._move(
            self.conn, self.row, state, message, self.cp, error_code=error_code, url=url
        )

    def poll(self) -> None:
        self.worker._poll(self.conn, self.row, self.cp)
