from http import HTTPStatus


class PlatformError(Exception):
    status = HTTPStatus.BAD_REQUEST

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class RequestConflict(PlatformError, ValueError):
    status = HTTPStatus.CONFLICT


class UnsupportedOperation(PlatformError, ValueError):
    status = HTTPStatus.CONFLICT


class OperationNotFound(PlatformError, ValueError):
    status = HTTPStatus.NOT_FOUND


class InvalidRequest(PlatformError, ValueError):
    status = HTTPStatus.UNPROCESSABLE_ENTITY


class NotConfigured(PlatformError):
    status = HTTPStatus.SERVICE_UNAVAILABLE


class ProviderReadFailed(PlatformError):
    status = HTTPStatus.SERVICE_UNAVAILABLE


class Unauthenticated(PlatformError):
    status = HTTPStatus.UNAUTHORIZED


class AccessDenied(PlatformError):
    status = HTTPStatus.FORBIDDEN


class TooManyRequests(PlatformError):
    status = HTTPStatus.TOO_MANY_REQUESTS
