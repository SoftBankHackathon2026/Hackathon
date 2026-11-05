from fastapi import APIRouter, Request, Response

from controlplane.api.deps import ContainerDep, session_identity
from controlplane.api.session import set_session_cookie
from controlplane.core.config import AuthMode
from controlplane.domain.models import Runtime
from controlplane.providers.capabilities import ProviderCapabilities
from controlplane.repositories.accounts import SESSION_COOKIE
from controlplane.schemas.system import Access, Health

router = APIRouter()


@router.get("/healthz")
def health(container: ContainerDep) -> Health:
    return Health(
        status="ok",
        mode=container.auth_mode if container.execution_configured else "preview",
        execution_configured=container.execution_configured,
    )


@router.get("/api/v1/access")
def access(request: Request, response: Response, container: ContainerDep) -> Access:
    user = None
    if container.auth_mode == AuthMode.LOGIN and container.execution_configured:
        user = session_identity(request, container)
    if user:
        set_session_cookie(request, response, request.cookies[SESSION_COOKIE])
    return Access(
        auth_mode=container.auth_mode,
        execution_configured=container.execution_configured,
        user=user,
    )


@router.get("/api/v1/providers")
def providers(container: ContainerDep) -> list[ProviderCapabilities]:
    return [container.registry.capabilities(runtime) for runtime in Runtime]
