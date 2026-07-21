import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Upstream LLM failure translated into the client-facing error contract.

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
    """The prompt was blocked by the content filter — the one upstream 4xx the client owns."""

    status_code = 400
    code = "content_filtered"
    message = "The message was blocked by the content filter."


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
    correlation_id = getattr(request.state, "correlation_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error, "correlation_id": correlation_id},
    )


async def upstream_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, UpstreamError)
    correlation_id = getattr(request.state, "correlation_id", None)
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
