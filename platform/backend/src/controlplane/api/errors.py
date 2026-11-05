from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from controlplane.core.errors import PlatformError

AUTH_INPUT_HINT = "이메일 형식, 비밀번호(8자 이상), 팀 이름(40자 이하)을 확인하세요."
INPUT_HINT = "입력 형식을 확인하세요. 인증정보는 요청에 넣지 마세요."


async def platform_error(request: Request, exc: PlatformError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail = AUTH_INPUT_HINT if request.url.path.startswith("/api/v1/auth/") else INPUT_HINT
    return JSONResponse(status_code=422, content={"detail": detail})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(PlatformError, platform_error)
    app.add_exception_handler(RequestValidationError, validation_error)
