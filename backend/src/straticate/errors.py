"""Application errors and the consistent JSON error envelope.

Every error response from the API uses the shape::

    {"error": {"code": "...", "message": "...", "detail": {...}}}
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_HTTP_ERROR_CODES = {
    404: "not_found",
    405: "method_not_allowed",
}


class ApplicationError(Exception):
    """Base class for expected application-level failures.

    Raising this from any endpoint produces the standard error envelope
    with the given HTTP status.

    Args:
        code: Stable machine-readable error code (e.g. ``"job_not_found"``).
        message: Human-readable description of the failure.
        status_code: HTTP status to respond with (default 400).
        detail: Optional structured context included in the envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail: dict[str, Any] = detail or {}


def error_response(
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
) -> JSONResponse:
    """Build a :class:`JSONResponse` carrying the standard error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "detail": jsonable_encoder(detail) if detail is not None else {},
            }
        },
    )


async def _handle_application_error(request: Request, exc: Exception) -> JSONResponse:
    """Map :class:`ApplicationError` to its envelope and HTTP status."""
    assert isinstance(exc, ApplicationError)
    return error_response(exc.status_code, exc.code, exc.message, exc.detail)


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Map Starlette/FastAPI ``HTTPException`` (e.g. 404) to the envelope."""
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    return error_response(exc.status_code, code, str(exc.detail))


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Map request validation failures (422) to the envelope.

    The pydantic error list is included under ``detail.errors``.
    """
    assert isinstance(exc, RequestValidationError)
    return error_response(
        422,
        "validation_error",
        "Request validation failed.",
        {"errors": exc.errors()},
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Map any unhandled exception to a 500 envelope without leaking details.

    The traceback is logged server-side; the response body contains only the
    generic ``internal_error`` envelope.
    """
    logger.exception("Unhandled error while processing %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "An internal server error occurred.")


def register_error_handlers(app: FastAPI) -> None:
    """Register the exception handlers that enforce the error envelope."""
    app.add_exception_handler(ApplicationError, _handle_application_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
