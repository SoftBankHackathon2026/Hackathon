from controlplane.domain.models import Runtime
from controlplane.execution.cli import main
from controlplane.execution.gcp_bridge.worker import GcpRuntime, GcpWorker
from controlplane.providers.gcp_bridge.models import GcpRequest, GcpSettings

if __name__ == "__main__":
    raise SystemExit(
        main(
            settings_type=GcpSettings,
            runtime_type=GcpRuntime,
            worker_type=GcpWorker,
            request_type=GcpRequest,
            queue_runtime=Runtime.GCP_CLOUD_RUN,
        )
    )
