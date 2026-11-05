from typing import Any

from fastapi import APIRouter

from controlplane.api.deps import ContainerDep, DeployerIdentity, ViewerIdentity
from controlplane.core.errors import NotConfigured
from controlplane.schemas.catalog import AttachEnvironment, CreateApplication
from controlplane.services.catalog import ApplicationCatalog

router = APIRouter(prefix="/api/v1", tags=["applications"])


def _catalog(container: ContainerDep) -> ApplicationCatalog:
    if not container.registry.catalog:
        raise NotConfigured("앱 저장소가 연결되지 않았습니다.")
    return container.registry.catalog


@router.get("/applications")
def applications(container: ContainerDep, identity: ViewerIdentity) -> list[dict[str, Any]]:
    catalog = container.registry.catalog
    return catalog.list_applications(identity) if catalog else []


@router.get("/targets")
def targets(container: ContainerDep, identity: ViewerIdentity) -> list[dict[str, Any]]:
    catalog = container.registry.catalog
    return catalog.targets() if catalog else []


@router.post("/applications", status_code=201)
def create_application(
    body: CreateApplication, container: ContainerDep, identity: DeployerIdentity
) -> dict[str, Any]:
    return _catalog(container).create(body, identity)


@router.post("/applications/{application_id}/environments", status_code=201)
def attach_environment(
    application_id: str,
    body: AttachEnvironment,
    container: ContainerDep,
    identity: DeployerIdentity,
) -> dict[str, Any]:
    catalog = _catalog(container)
    attached = catalog.attach(application_id, body.target_id, identity)
    return catalog.decorate(container.registry.get(attached["id"]).public_environment())
