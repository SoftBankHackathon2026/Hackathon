from controlplane.core.secrets import DatabaseSettings
from controlplane.domain.models import DeploymentRequest
from controlplane.repositories.artifacts import is_built_image_approved


class ProviderSettings(DatabaseSettings):
    def built_image_allowed(self, request: DeploymentRequest) -> bool:
        return is_built_image_approved(
            self.database_url.get_secret_value(),
            request.application,
            request.environment_id,
            request.source.image,
        )
