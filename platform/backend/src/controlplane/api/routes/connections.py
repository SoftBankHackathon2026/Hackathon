"""Optional connection endpoints; legacy enrollments remain unchanged."""

import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from controlplane.api.deps import check_browser_origin, get_container, session_identity
from controlplane.api.errors import platform_error, validation_error
from controlplane.container import Container
from controlplane.core.config import AuthMode
from controlplane.core.errors import (
    AccessDenied,
    InvalidRequest,
    NotConfigured,
    PlatformError,
    Unauthenticated,
)


class PrivateConnectionRoute(APIRoute):
    """Keep credentials and state out of HTTP caches, including expected errors."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request):
            try:
                response = await handler(request)
            except PlatformError as exc:
                response = await platform_error(request, exc)
            except RequestValidationError as exc:
                response = await validation_error(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


router = APIRouter(tags=["connections"], route_class=PrivateConnectionRoute)
PUBLIC = "/api/v1/connections"
RUNTIME = "/api/v1/connection-runtime"


def service(container: Container):
    if container.connections is None:
        raise NotConfigured("보안 연결 서비스가 아직 설정되지 않았습니다.")
    return container.connections


def actor(request: Request, container: Container):
    if container.auth_mode != AuthMode.LOGIN:
        raise AccessDenied("보안 연결에는 로그인한 사용자와 조직이 필요합니다.")
    check_browser_origin(request, container, required=True)
    identity = session_identity(request, container)
    if identity is None:
        raise Unauthenticated("로그인이 필요합니다.")
    from migration_connection.control.service import Actor

    return Actor(identity.team, identity.email, identity.can_deploy)


def ip(request: Request):
    # X-Forwarded-For is never parsed here. Only a deployment-configured trusted
    # ASGI proxy may establish request.client; do not trust arbitrary headers.
    return request.client.host if request.client else "127.0.0.1"


async def document(request: Request, limit: int = 32768):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise InvalidRequest("요청 자료가 너무 큽니다.")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_float(_):
        raise ValueError("integer JSON only")

    try:
        value = json.loads(
            raw, object_pairs_hook=pairs, parse_float=reject_float, parse_constant=reject_float
        )
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, UnicodeError):
        raise InvalidRequest("요청 자료 형식이 올바르지 않습니다.") from None


def fields(value, required, optional=()):
    if set(value) - set(required) - set(optional) or not set(required) <= set(value):
        raise InvalidRequest("요청 필드를 확인하세요.")


async def invoke(call, *args, **kwargs):
    from migration_connection.control.errors import ConnectionError

    try:
        return await run_in_threadpool(call, *args, **kwargs)
    except ConnectionError as exc:
        return JSONResponse({"detail": exc.code, "code": exc.code}, status_code=exc.status)


@router.get(PUBLIC + "/capabilities")
async def capabilities(request: Request, container: Container = Depends(get_container)):
    if container.connections is None or container.auth_mode != AuthMode.LOGIN:
        return {
            "available": False,
            "reason": "보안 연결 실행 환경이 아직 준비되지 않았습니다.",
            "providers": [],
        }
    identity = session_identity(request, container)
    return await run_in_threadpool(
        container.connections.capabilities, identity.team if identity else None
    )


@router.get(PUBLIC)
async def list_connections(request: Request, container: Container = Depends(get_container)):
    implementation = service(container)
    return await invoke(implementation.list, actor(request, container))


@router.post(PUBLIC, status_code=201)
async def create_connection(request: Request, container: Container = Depends(get_container)):
    implementation = service(container)
    identity = actor(request, container)
    body = await document(request)
    fields(body, ("publicKey", "operationId"))
    limited = await invoke(implementation.limiter.check, ip(request), implementation.clock())
    if isinstance(limited, JSONResponse):
        return limited
    return await invoke(implementation.create, identity, body["publicKey"], body["operationId"])


@router.get(PUBLIC + "/{request_id}")
async def get_connection(
    request_id: str, request: Request, container: Container = Depends(get_container)
):
    implementation = service(container)
    return await invoke(implementation.get, request_id, actor(request, container))


@router.post(PUBLIC + "/{request_id}/challenge")
@router.post(PUBLIC + "/{request_id}/device-challenge")
async def challenge(
    request_id: str, request: Request, container: Container = Depends(get_container)
):
    implementation = service(container)
    body = await document(request, 1024)
    fields(body, ("candidateId",))
    return await invoke(
        implementation.challenge,
        request_id,
        body["candidateId"],
        ip(request),
        device=request.url.path.endswith("/device-challenge"),
    )


@router.post(PUBLIC + "/{request_id}/prove")
async def prove(request_id: str, request: Request, container: Container = Depends(get_container)):
    implementation = service(container)
    body = await document(request)
    fields(body, ("payload", "enrollment_signature", "device_signature"), ("metadata",))
    return await invoke(implementation.prove, request_id, body, ip(request))


@router.post(PUBLIC + "/{request_id}/device")
@router.post(PUBLIC + "/{request_id}/recover")
async def device(request_id: str, request: Request, container: Container = Depends(get_container)):
    implementation = service(container)
    body = await document(request)
    fields(body, ("payload", "signature"))
    if request.url.path.endswith("/recover") and (
        not isinstance(body["payload"], dict) or body["payload"].get("action") != "status"
    ):
        raise InvalidRequest("복구 요청은 상태 확인만 허용합니다.")
    return await invoke(implementation.device, request_id, body, ip(request))


@router.post(PUBLIC + "/{request_id}/{action}")
async def browser_action(
    request_id: str, action: str, request: Request, container: Container = Depends(get_container)
):
    implementation = service(container)
    identity = actor(request, container)
    body = await document(request)
    requirements = {
        "cancel": (),
        "approve": ("candidateDigest", "code"),
        "reject": (),
        "collect": ("discoveredId",),
        "register": ("collectedId", "inventoryDigest", "target"),
        "reconnect": (),
        "revoke": (),
    }
    if action not in requirements:
        raise InvalidRequest("지원하지 않는 연결 작업입니다.")
    fields(body, requirements[action])
    return await invoke(implementation.browser, request_id, identity, action, body, ip=ip(request))


def runtime(request: Request, container: Container):
    implementation = service(container)
    expected = "Bearer " + implementation.runtime_token
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise Unauthenticated("연결 실행 서비스 인증이 필요합니다.")
    return implementation


@router.get(RUNTIME + "/{request_id}/authorize")
async def authorize(
    request_id: str, request: Request, container: Container = Depends(get_container)
):
    implementation = runtime(request, container)
    return await invoke(implementation.authorize, request_id)


@router.post(RUNTIME + "/{request_id}/tunnel")
async def tunnel(request_id: str, request: Request, container: Container = Depends(get_container)):
    implementation = runtime(request, container)
    body = await document(request, 4096)
    fields(
        body,
        ("epoch", "connection_id", "state"),
        ("key_expires_at_ms", "lease_expires_at_ms", "checks"),
    )
    return await invoke(implementation.tunnel, request_id, body)


@router.post(RUNTIME + "/{request_id}/device")
async def relay(request_id: str, request: Request, container: Container = Depends(get_container)):
    implementation = runtime(request, container)
    body = await document(request, 262144)
    fields(body, ("payload", "signature"))
    return await invoke(implementation.device, request_id, body, ip(request), through_tunnel=True)


@router.post(RUNTIME + "/health")
async def runtime_health(request: Request, container: Container = Depends(get_container)):
    implementation = runtime(request, container)
    body = await document(request, 4096)
    return await invoke(implementation.runtime_health, body)


@router.post(RUNTIME + "/auth-check")
@router.post(RUNTIME + "/auth-failure")
async def runtime_auth(request: Request, container: Container = Depends(get_container)):
    implementation = runtime(request, container)
    body = await document(request, 512)
    fields(body, ("source_ip",))
    import ipaddress

    try:
        address = str(ipaddress.ip_address(body["source_ip"]))
    except (ValueError, TypeError):
        raise InvalidRequest("IP 주소를 확인하세요.") from None
    if request.url.path.endswith("/auth-check"):
        outcome = await invoke(implementation.limiter.check, address, implementation.clock())
        return outcome if isinstance(outcome, JSONResponse) else {"allowed": True}
    outcome = await invoke(implementation.limiter.fail, address, implementation.clock())
    return outcome if isinstance(outcome, JSONResponse) else {"blocked_until_ms": outcome}
