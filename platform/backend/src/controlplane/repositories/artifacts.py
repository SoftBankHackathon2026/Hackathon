import psycopg

from controlplane.builds.store import approved_image
from controlplane.demo_deployments.artifacts import approved_demo_image


def is_built_image_approved(dsn: str, application: str, environment_id: str, image: str) -> bool:
    try:
        return approved_image(dsn, application, environment_id, image) or approved_demo_image(
            dsn, application, environment_id, image
        )
    except psycopg.Error:
        return False
