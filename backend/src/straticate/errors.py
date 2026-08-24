"""Application errors and the consistent JSON error envelope.

Every error response from the API uses the shape defined by
:class:`straticate.schemas.ErrorEnvelope`::

    {"error": {"code": "...", "message": "...", "detail": {...}}}

**Including 500s, and including their CORS headers.** Starlette's own
catch-all lives in ``ServerErrorMiddleware``, which wraps everything —
``CORSMiddleware`` included — so an envelope produced there is sent *outside*
the CORS layer and carries no ``Access-Control-Allow-Origin``. A browser then
refuses to let the page read it, and a cross-origin client that can parse every
other error in this contract sees an opaque network failure for the one case it
most needs to report. :class:`ErrorEnvelopeMiddleware` closes that gap by
producing the envelope from inside the CORS layer; see
:func:`straticate.main.create_app` for the ordering that puts it there.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from straticate.schemas import ErrorEnvelope, ErrorInfo

logger = logging.getLogger(__name__)


def _http_error_code(status_code: int) -> str:
    """Derive a stable snake_case error code from an HTTP status.

    Examples: 404 -> ``"not_found"``, 405 -> ``"method_not_allowed"``,
    429 -> ``"too_many_requests"``. Unknown statuses fall back to
    ``"http_error"``.
    """
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return "http_error"


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

    def to_error_info(self) -> ErrorInfo:
        """This error as the contract :class:`~straticate.schemas.ErrorInfo`.

        ``detail`` is passed through ``jsonable_encoder`` (exactly as
        :func:`error_response` does) so every consumer — the HTTP exception
        handler and the job manager's failure path alike — produces the same
        JSON-safe shape.
        """
        return ErrorInfo(code=self.code, message=self.message, detail=jsonable_encoder(self.detail))


def _envelope_response(
    status_code: int,
    error: ErrorInfo,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Wrap an :class:`ErrorInfo` in the envelope as a :class:`JSONResponse`."""
    envelope = ErrorEnvelope(error=error)
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=dict(headers) if headers is not None else None,
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a :class:`JSONResponse` carrying the standard error envelope.

    The body is built from the shared contract schemas
    (:class:`~straticate.schemas.ErrorEnvelope` /
    :class:`~straticate.schemas.ErrorInfo`) so responses and the published
    OpenAPI contract cannot drift apart.
    """
    return _envelope_response(
        status_code,
        ErrorInfo(
            code=code,
            message=message,
            detail=jsonable_encoder(detail) if detail is not None else {},
        ),
        headers=headers,
    )


async def _handle_application_error(request: Request, exc: Exception) -> JSONResponse:
    """Map :class:`ApplicationError` to its envelope and HTTP status."""
    assert isinstance(exc, ApplicationError)
    return _envelope_response(exc.status_code, exc.to_error_info())


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Map Starlette/FastAPI ``HTTPException`` (e.g. 404) to the envelope.

    Response headers attached to the exception (``Allow``, ``Retry-After``,
    ``WWW-Authenticate``, …) are preserved. A string ``detail`` becomes the
    envelope message; a structured ``detail`` is carried in ``detail`` with a
    generic status-phrase message.
    """
    assert isinstance(exc, StarletteHTTPException)
    code = _http_error_code(exc.status_code)
    # FastAPI's HTTPException accepts Any as detail even though Starlette
    # annotates it as str.
    raw_detail: Any = exc.detail
    if isinstance(raw_detail, str):
        message, detail = raw_detail, None
    else:
        try:
            message = HTTPStatus(exc.status_code).phrase
        except ValueError:
            message = f"HTTP {exc.status_code}"
        detail = exc.detail
    return error_response(exc.status_code, code, message, detail, headers=exc.headers)


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


def internal_error_response() -> JSONResponse:
    """The generic ``internal_error`` envelope, leaking nothing about the cause.

    Deliberately silent: it builds a response and logs nothing, so the single
    place an unexpected exception is reported by this application stays
    :func:`_handle_unexpected_error`. :class:`ErrorEnvelopeMiddleware` sends
    this and then re-raises, which is what gets it there.
    """
    return error_response(500, "internal_error", "An internal server error occurred.")


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Map any unhandled exception to a 500 envelope without leaking details.

    The traceback is logged server-side; the response body contains only the
    generic ``internal_error`` envelope.

    This is the application's **only** report of an unexpected exception.
    Registered as the ``Exception`` handler, it runs in Starlette's outermost
    ``ServerErrorMiddleware``, which every such exception reaches — including
    the ones :class:`ErrorEnvelopeMiddleware` has already answered, because it
    re-raises. Its *response* is discarded in that case (one is already on the
    wire, and ``ServerErrorMiddleware`` checks) but the log is not, so moving
    the logging into the middleware would only duplicate it.
    """
    logger.exception("Unhandled error while processing %s %s", request.method, request.url.path)
    return internal_error_response()


class ErrorEnvelopeMiddleware:
    """Turn an unhandled exception into the ``internal_error`` envelope.

    A pure ASGI middleware rather than an exception handler, because *where*
    the envelope is produced is the entire point. Starlette's stack is::

        ServerErrorMiddleware → [user middleware] → ExceptionMiddleware → routes

    An ``Exception`` handler registered on the application runs in
    ``ServerErrorMiddleware``, outside every user middleware — so its response
    never passes back through ``CORSMiddleware`` and never gains
    ``Access-Control-Allow-Origin``. Installed as user middleware *inside*
    CORS, this class catches the same exceptions one layer lower, and its
    response travels out through CORS like any other.

    **The exception is always re-raised**, after the envelope has been sent.
    That is ``ServerErrorMiddleware``'s own contract ("we always continue to
    raise the exception … allows test clients to optionally raise the error
    within the test case") and it is not optional for a test suite: swallowing
    it made ``raise_app_exceptions=True`` inert, so a route that started
    raising ``AttributeError`` quietly returned a 500 envelope and every
    assertion that did not check ``status_code`` still passed.

    Re-raising costs the client nothing. The response has already gone out
    through ``send``, so ``ServerErrorMiddleware``'s own ``response_started``
    flag is set: it will not send a second response, and the envelope reaches
    the caller with the CORS headers this class exists to give it. It does
    still invoke the registered ``Exception`` handler, which is where the
    traceback is logged — hence :func:`internal_error_response` here rather
    than :func:`_handle_unexpected_error`, so one exception produces one
    application log line rather than two. At the edge of the process a real
    server logs it as well, exactly as it does for any unhandled ASGI
    exception.

    If the response has already started there is nothing to salvage either:
    the status line and headers are on the wire, so the exception is re-raised
    with no envelope at all and the connection breaks, which is the honest
    outcome.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Delegate, converting a pre-response exception into the envelope."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def watched_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, watched_send)
        except Exception:
            if not started:
                await internal_error_response()(scope, receive, send)
            raise


def register_error_handlers(app: FastAPI) -> None:
    """Register the exception handlers that enforce the error envelope.

    The ``Exception`` handler is registered as well as
    :class:`ErrorEnvelopeMiddleware`, and is not redundant. It covers anything
    raised *outside* the middleware (in ``CORSMiddleware`` itself, or in
    lifespan-adjacent code), where an enveloped 500 without CORS headers still
    beats a bare ASGI error — and because the middleware re-raises, it is also
    the one place every unexpected exception is logged, whether or not an
    envelope was already sent.
    """
    app.add_exception_handler(ApplicationError, _handle_application_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
