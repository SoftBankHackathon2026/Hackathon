from fastapi import APIRouter, Request, Response

from controlplane.api.deps import LoginAuth
from controlplane.api.session import clear_session_cookie, set_session_cookie
from controlplane.repositories.accounts import SESSION_COOKIE
from controlplane.schemas.accounts import Credentials, Identity, SignUp

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/signup", status_code=201)
def sign_up(body: SignUp, request: Request, response: Response, auth: LoginAuth) -> Identity:
    identity, token = auth.sign_up(body)
    set_session_cookie(request, response, token)
    return identity


@router.post("/login")
def log_in(body: Credentials, request: Request, response: Response, auth: LoginAuth) -> Identity:
    identity, token = auth.log_in(body)
    set_session_cookie(request, response, token)
    return identity


@router.post("/logout", status_code=204)
def log_out(request: Request, response: Response, auth: LoginAuth) -> None:
    auth.log_out(request.cookies.get(SESSION_COOKIE))
    clear_session_cookie(response)
