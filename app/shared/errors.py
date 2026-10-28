"""RFC 9457 problem+json error handling.

Every error response leaves the API in the same shape: type, title, status,
code, request_id, and field_errors for validation failures. Stack traces and
SQL never reach the client (report 21 §5, report 24 §1).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.shared.context import get_request_id

logger = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

_STATUS_TITLES: dict[int, str] = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    410: "Gone",
    422: "Unprocessable entity",
    429: "Too many requests",
    500: "Internal server error",
    503: "Service unavailable",
}


class AppError(Exception):
    """An application error whose message is safe to show to the client."""

    def __init__(
        self,
        *,
        code: str,
        title: str,
        status_code: int = 400,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.code = code
        self.title = title
        self.status_code = status_code
        self.detail = detail


def problem_response(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str | None = None,
    field_errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    """Build one problem+json response."""
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "code": code,
        "request_id": get_request_id(),
    }
    if detail is not None:
        body["detail"] = detail
    if field_errors:
        body["field_errors"] = field_errors
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        return await _handle_unexpected(request, exc)
    return problem_response(
        status_code=exc.status_code,
        code=f"http_{exc.status_code}",
        title=_STATUS_TITLES.get(exc.status_code, "Error"),
        detail=exc.detail,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):
        return await _handle_unexpected(request, exc)
    field_errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": str(error["msg"]),
            "code": str(error["type"]),
        }
        for error in exc.errors()
    ]
    return problem_response(
        status_code=422,
        code="validation_error",
        title="Request validation failed",
        detail="One or more fields are invalid.",
        field_errors=field_errors,
    )


async def _handle_app_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, AppError):
        return await _handle_unexpected(request, exc)
    return problem_response(
        status_code=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
    )


async def _handle_unexpected(request: Request, exc: Exception) -> Response:
    logger.exception("unhandled error", exc_info=exc)
    return problem_response(
        status_code=500,
        code="internal_error",
        title="Internal server error",
        detail="The request could not be completed.",
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the problem+json handlers on the application."""
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(Exception, _handle_unexpected)
