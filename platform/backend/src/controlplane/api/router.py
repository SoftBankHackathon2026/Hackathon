from fastapi import APIRouter

from controlplane.api.routes import (
    applications,
    auth,
    builds,
    environments,
    migrations,
    monitoring,
    releases,
    runs,
    system,
    team,
)

api_router = APIRouter()
for module in (
    system,
    auth,
    team,
    builds,
    runs,
    applications,
    environments,
    migrations,
    monitoring,
    releases,
    migrations,
):
    api_router.include_router(module.router)
