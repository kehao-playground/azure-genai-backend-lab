import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response

CORRELATION_ID_HEADER = "X-Correlation-Id"

# Readable from anywhere below the middleware (e.g. the LLM adapter's
# per-call log line) without threading the id through every signature.
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


async def correlation_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    token = correlation_id_var.set(correlation_id)
    try:
        response = await call_next(request)
    finally:
        correlation_id_var.reset(token)
    response.headers[CORRELATION_ID_HEADER] = correlation_id
    return response
