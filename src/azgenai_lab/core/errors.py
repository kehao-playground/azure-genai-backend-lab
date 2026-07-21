import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """The one error shape every non-2xx response uses (Day 3 contract).

    ``correlation_id`` is non-optional: the correlation middleware runs outside
    every exception handler, so an envelope without it is a bug, not a case.
    """

    error: ErrorDetail
    correlation_id: str


class UpstreamError(Exception):
    """Backend dependency failure translated into the client-facing error
    contract — the LLM upstream and, since Day 7, conversation storage.

    Adapters raise these instead of leaking SDK exceptions, so the API layer
    never imports the SDK. ``upstream_detail`` may contain endpoint or
    deployment details — it goes to the log, never into the response.
    """

    status_code: int = 502
    code: str = "upstream_error"
    message: str = "The upstream LLM service failed."

    def __init__(self, upstream_detail: str | None = None) -> None:
        super().__init__(self.message)
        self.upstream_detail = upstream_detail


class ConfigurationError(UpstreamError):
    """Our deployment is broken (bad key, wrong deployment name) — not the client's fault."""

    status_code = 500
    code = "configuration_error"
    message = "The service is misconfigured; this request cannot succeed."


class ContentFilteredError(UpstreamError):
    """The prompt was blocked by the content filter — a client-owned upstream 4xx."""

    status_code = 400
    code = "content_filtered"
    message = "The message was blocked by the content filter."


class InvalidInputError(UpstreamError):
    """The upstream model rejected the input itself (e.g. context length) — client-owned."""

    status_code = 400
    code = "invalid_input"
    message = "The upstream model rejected the input (for example, it exceeds the context window)."


class StorageError(UpstreamError):
    """Conversation storage failed — our dependency, not the client's fault.

    Raised after inference has already happened (and been billed): the reply
    exists but could not be committed. Mapping it through the shared error
    machinery keeps both contracts intact — HTTP 500 envelope before a
    response is returned, SSE ``error`` terminal after a 200 (review r01
    finding 3). Retrying such a failure repeats inference and billing.
    """

    status_code = 500
    code = "storage_error"
    message = "Conversation storage failed; the turn was not saved."


class UpstreamThrottledError(UpstreamError):
    """Upstream capacity is exhausted — our quota problem, not the client's request rate."""

    status_code = 503
    code = "upstream_throttled"
    message = "Upstream capacity is exhausted; retry later."


class UpstreamTimeoutError(UpstreamError):
    status_code = 504
    code = "upstream_timeout"
    message = "The upstream LLM call timed out."


class UpstreamServiceError(UpstreamError):
    status_code = 502
    code = "upstream_error"
    message = "The upstream LLM service failed."


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if isinstance(exc.detail, dict):
        error = exc.detail
    else:
        error = {"code": "http_error", "message": str(exc.detail)}
    correlation_id: str = request.state.correlation_id
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error, "correlation_id": correlation_id},
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    message = "; ".join(
        f"{'.'.join(str(loc) for loc in error['loc'] if loc != 'body')}: {error['msg']}"
        for error in exc.errors()
    )
    correlation_id: str = request.state.correlation_id
    return JSONResponse(
        status_code=422,
        content={
            "error": {"code": "validation_error", "message": message},
            "correlation_id": correlation_id,
        },
    )


async def upstream_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, UpstreamError)
    correlation_id: str = request.state.correlation_id
    logger.warning(
        "upstream failure code=%s correlation_id=%s detail=%s",
        exc.code,
        correlation_id,
        exc.upstream_detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message},
            "correlation_id": correlation_id,
        },
    )
