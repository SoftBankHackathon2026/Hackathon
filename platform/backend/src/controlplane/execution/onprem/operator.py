"""Oneport operator CLI; shares durable enqueue/display with the native CLI."""

from controlplane.domain.models import Runtime
from controlplane.execution.cli import main
from controlplane.execution.onprem.runtime import ArgoRuntime
from controlplane.execution.onprem.settings import ArgoSettings
from controlplane.execution.onprem.worker import ArgoWorker
from controlplane.providers.onprem.models import ArgoDeploymentRequest

if __name__ == "__main__":
    raise SystemExit(
        main(
            settings_type=ArgoSettings,
            runtime_type=ArgoRuntime,
            worker_type=ArgoWorker,
            request_type=ArgoDeploymentRequest,
            queue_runtime=Runtime.ONPREM_ARGO,
        )
    )
