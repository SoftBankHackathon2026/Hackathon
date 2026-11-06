import secrets
from typing import Annotated

from fastapi import Depends, Header, Request

from controlplane.container import Container
from controlplane.core.config import AuthMode
from controlplane.core.errors import AccessDenied, NotConfigured, OperationNotFound, Unauthenticated
from controlplane.cost.service import CostService
from controlplane.repositories.accounts import SESSION_COOKIE
from controlplane.schemas.accounts import Identity
from controlplane.services.auth import AuthService
from controlplane.services.builds import BuildService
from controlplane.services.deployments import DeploymentService
from controlplane.services.enrollments import EnrollmentService
from controlplane.services.environments import EnvironmentService
from controlplane.services.fleet import FleetService
from controlplane.services.ingest import IngestService
from controlplane.services.migrations import MigrationService
from controlplane.services.monitoring import MonitoringService
from controlplane.services.runs import DemoRunService
from controlplane.services.security import SecurityService

EXECUTION_NOT_CONFIGURED = "실행 환경이 등록되지 않았습니다."
NOT_LOGIN_MODE = "로그인 모드가 아닙니다."


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_deployments(container: ContainerDep) -> DeploymentService:
    return container.deployments


def get_environments(container: ContainerDep) -> EnvironmentService:
    return container.environments


def get_monitoring(container: ContainerDep) -> MonitoringService:
    return container.monitoring


def get_builds(container: ContainerDep) -> BuildService:
    return container.builds


def get_runs(container: ContainerDep) -> DemoRunService:
    return container.runs


def get_migrations(container: ContainerDep) -> MigrationService:
    return container.migrations


def get_fleet(container: ContainerDep) -> FleetService:
    return container.fleet


def get_security(container: ContainerDep) -> SecurityService:
    return container.security


def get_costs(container: ContainerDep) -> CostService:
    return container.costs


def require_ingest(
    container: ContainerDep, authorization: str = Header(default="")
) -> IngestService:
    if container.ingest is None or container.ingest_token is None:
        raise NotConfigured("마이그레이션 보고 수신이 설정되지 않았습니다.")
    expected = "Bearer " + container.ingest_token.get_secret_value()
    if not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise Unauthenticated("보고용 토큰이 필요합니다.")
    return container.ingest


def get_enrollments(container: ContainerDep) -> EnrollmentService:
    if container.enrollments is None:
        raise NotConfigured(
            "환경 연결에는 플랫폼 DB 가 필요합니다. ONEPORT_DATABASE_URL_FILE 을 설정하세요."
        )
    return container.enrollments


MigrationsDep = Annotated[MigrationService, Depends(get_migrations)]
EnrollmentsDep = Annotated[EnrollmentService, Depends(get_enrollments)]
FleetDep = Annotated[FleetService, Depends(get_fleet)]
SecurityDep = Annotated[SecurityService, Depends(get_security)]
IngestDep = Annotated[IngestService, Depends(require_ingest)]
CostsDep = Annotated[CostService, Depends(get_costs)]
DeploymentsDep = Annotated[DeploymentService, Depends(get_deployments)]
EnvironmentsDep = Annotated[EnvironmentService, Depends(get_environments)]
MonitoringDep = Annotated[MonitoringService, Depends(get_monitoring)]
BuildsDep = Annotated[BuildService, Depends(get_builds)]
RunsDep = Annotated[DemoRunService, Depends(get_runs)]


def check_browser_origin(request: Request, container: Container, *, required: bool) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in container.http.allowed_origins:
        raise AccessDenied("허용되지 않은 요청 출처입니다.")
    if required and request.method == "POST" and not origin:
        raise AccessDenied("등록된 플랫폼 화면에서 실행하세요.")


def session_identity(request: Request, container: Container) -> Identity | None:
    if container.auth is None:
        return None
    return container.auth.identity(request.cookies.get(SESSION_COOKIE))


def _check_operator_token(container: Container, authorization: str) -> None:
    expected = "Bearer " + container.operator_token.get_secret_value()
    if not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise Unauthenticated("운영자 접속 토큰이 필요합니다.")


def require_operator(
    request: Request, container: ContainerDep, authorization: str = Header(default="")
) -> Identity | None:
    if not container.execution_configured:
        raise NotConfigured(EXECUTION_NOT_CONFIGURED)
    if container.auth_mode == AuthMode.OPERATOR:
        _check_operator_token(container, authorization)
    check_browser_origin(
        request, container, required=container.auth_mode in {AuthMode.DEMO, AuthMode.LOGIN}
    )
    if container.auth_mode != AuthMode.LOGIN:
        return None
    identity = session_identity(request, container)
    if identity is None:
        raise Unauthenticated("로그인이 필요합니다.")
    return identity


OperatorIdentity = Annotated[Identity | None, Depends(require_operator)]


def require_deployer(identity: OperatorIdentity) -> Identity | None:
    if identity and not identity.can_deploy:
        raise AccessDenied("보기 전용 계정입니다. 팀 관리자에게 배포 권한을 요청하세요.")
    return identity


DeployerIdentity = Annotated[Identity | None, Depends(require_deployer)]


def require_team_owner(identity: OperatorIdentity) -> Identity:
    if identity is None:
        raise OperationNotFound(NOT_LOGIN_MODE)
    if identity.role != "owner":
        raise AccessDenied("팀 관리자만 할 수 있습니다.")
    return identity


OwnerIdentity = Annotated[Identity, Depends(require_team_owner)]


def optional_viewer(
    request: Request, container: ContainerDep, authorization: str = Header(default="")
) -> Identity | None:
    if container.auth_mode != AuthMode.LOGIN or not container.execution_configured:
        return None
    return require_operator(request, container, authorization)


ViewerIdentity = Annotated[Identity | None, Depends(optional_viewer)]


def require_enroller(
    request: Request, container: ContainerDep, authorization: str = Header(default="")
) -> Identity | None:
    """환경 연결 명령을 만드는 권한. 배포 Provider 가 없는 대시보드 전용 구성에서도 쓴다.

    operator 모드는 운영자 토큰이 설정돼 있어야 하고, demo·login 모드는 등록된 화면 Origin 이 필요하다.
    login 모드는 배포 권한(owner·member)이 있는 팀원만 만들 수 있다.
    """
    if container.auth_mode == AuthMode.OPERATOR:
        if container.operator_token is None:
            raise AccessDenied("운영자 토큰이 설정되지 않아 환경 연결 명령을 만들 수 없습니다.")
        _check_operator_token(container, authorization)
    check_browser_origin(
        request, container, required=container.auth_mode in {AuthMode.DEMO, AuthMode.LOGIN}
    )
    if container.auth_mode != AuthMode.LOGIN:
        return None
    identity = session_identity(request, container)
    if identity is None:
        raise Unauthenticated("로그인이 필요합니다.")
    if not identity.can_deploy:
        raise AccessDenied("보기 전용 계정입니다. 팀 관리자에게 권한을 요청하세요.")
    return identity


EnrollerIdentity = Annotated[Identity | None, Depends(require_enroller)]


def require_login_mode(request: Request, container: ContainerDep) -> AuthService:
    if container.auth_mode != AuthMode.LOGIN:
        raise OperationNotFound(NOT_LOGIN_MODE)
    if container.auth is None:
        raise NotConfigured(EXECUTION_NOT_CONFIGURED)
    check_browser_origin(request, container, required=True)
    return container.auth


LoginAuth = Annotated[AuthService, Depends(require_login_mode)]
