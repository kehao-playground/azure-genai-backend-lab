import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

CORRELATION_ID_HEADER = "X-Correlation-Id"


async def correlation_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers[CORRELATION_ID_HEADER] = correlation_id
    return response
