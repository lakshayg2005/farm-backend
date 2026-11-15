"""One error shape for every service:

{"error": {"code": "STOCK_UNAVAILABLE", "message": "...", "correlationId": "...", "details": ...}}
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from farm_common.log import get_correlation_id

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
}


class AppError(Exception):
    """Raise this for any expected business error, e.g. AppError("STOCK_UNAVAILABLE", "...", 409)."""

    def __init__(
        self, code: str, message: str, status_code: int = 400, details: Any | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class NotFound(AppError):
    def __init__(self, message: str = "Resource not found", code: str = "NOT_FOUND") -> None:
        super().__init__(code, message, 404)


class Conflict(AppError):
    def __init__(self, message: str, code: str = "CONFLICT") -> None:
        super().__init__(code, message, 409)


class Unauthorized(AppError):
    def __init__(
        self, message: str = "Authentication required", code: str = "UNAUTHORIZED"
    ) -> None:
        super().__init__(code, message, 401)


class Forbidden(AppError):
    def __init__(self, message: str = "Not allowed", code: str = "FORBIDDEN") -> None:
        super().__init__(code, message, 403)


def error_body(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "correlationId": get_correlation_id(),
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(
        error_body(exc.code, exc.message, exc.details), exc.status_code, headers=headers
    )


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code, "ERROR")
    return JSONResponse(error_body(code, str(exc.detail)), exc.status_code, headers=exc.headers)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
    ]
    return JSONResponse(error_body("VALIDATION_ERROR", "Invalid request", details), 422)


def register_error_handlers(app: FastAPI) -> None:
    """Unexpected crashes (500) are turned into this shape by CorrelationIdMiddleware."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
