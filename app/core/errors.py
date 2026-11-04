"""Shared error envelope: {"error": {"code", "message", "request_id"[, "detail"]}}."""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.security_headers import apply_security_headers

logger = logging.getLogger(__name__)

_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    detail: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or "req_" + uuid4().hex
    error = {"code": code, "message": message, "request_id": request_id}
    if detail is not None:
        error["detail"] = detail
    response = JSONResponse({"error": error}, status_code=status, headers=headers)
    response.headers["X-Request-Id"] = request_id
    return response


async def request_id_middleware(request: Request, call_next) -> Response:
    # Client-supplied X-Request-Id is ignored on purpose.
    request.state.request_id = "req_" + uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc.status, exc.code, exc.message, exc.detail)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo input values back; they may contain secrets.
    return error_response(request, 422, "VALIDATION_FAILED", "Request validation failed")


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else code
    return error_response(request, exc.status_code, code, message, headers=exc.headers)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # Runs in ServerErrorMiddleware, outside the app middleware, so headers are set here.
    response = error_response(request, 500, "INTERNAL_ERROR", "Internal server error")
    logger.error(
        "unhandled error request_id=%s",
        response.headers["X-Request-Id"],
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    apply_security_headers(request.url.path, response)
    return response


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)
