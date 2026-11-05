from controlplane.domain.models import Runtime
from controlplane.execution.catalog_worker import CatalogGcpWorker
from controlplane.execution.cli import main
from controlplane.execution.gcp.runtime import GcpRuntime
from controlplane.execution.gcp.settings import GcpSettings
from controlplane.providers.gcp.models import GcpDeploymentRequest

if __name__ == "__main__":
    raise SystemExit(
        main(
            settings_type=GcpSettings,
            runtime_type=GcpRuntime,
            worker_type=CatalogGcpWorker,
            request_type=GcpDeploymentRequest,
            queue_runtime=Runtime.GCP_CLOUD_RUN,
        )
    )
