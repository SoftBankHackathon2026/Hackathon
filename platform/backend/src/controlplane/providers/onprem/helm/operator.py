from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.execution.cli import main
from controlplane.providers.onprem.helm.catalog_worker import CatalogHelmWorker
from controlplane.providers.onprem.helm.runtime import HelmRuntime
from controlplane.providers.onprem.helm.settings import HelmSettings

if __name__ == "__main__":
    raise SystemExit(
        main(
            settings_type=HelmSettings,
            runtime_type=HelmRuntime,
            worker_type=CatalogHelmWorker,
            request_type=DeploymentRequest,
            queue_runtime=Runtime.ONPREM_HELM,
        )
    )
