from fastapi import Request, Response

from controlplane.repositories.accounts import SESSION_COOKIE, SESSION_TTL

COOKIE_PATH = "/api"


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=scheme == "https",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
