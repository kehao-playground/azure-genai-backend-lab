import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response
from opentelemetry import trace

from azgenai_lab.core.telemetry import ATTR_CORRELATION_ID

CORRELATION_ID_HEADER = "X-Correlation-Id"

CORRELATION_ID_MAX_BYTES = 128
# RFC 7230's VCHAR: printable ASCII, no space, no control characters. Chosen
# over "whatever the caller sent" because Day 27 copies this string onto every
# server and application span, and a value that reaches a second store should
# be bounded in both length and alphabet before it gets there.
_VCHAR = frozenset(chr(code) for code in range(0x21, 0x7F))


def accept_correlation_id(raw: list[str]) -> str | None:
    """Return the caller's correlation id if it is usable, else None.

    Never rejects the request: the Day 5 error contract is unchanged. The cost
    of that choice is stated in the contract instead -- the `X-Correlation-Id`
    echoed back may differ from the one sent.

    Deliberately does not normalise. Trimming would make the echoed value
    differ from the accepted one in a second, quieter way.
    """
    # Exactly one. A duplicated header is representable at the ASGI layer, and
    # picking one of two silently is how a caller ends up correlating against
    # an id nobody sent.
    if len(raw) != 1:
        return None
    value = raw[0]
    if not 1 <= len(value.encode("utf-8")) <= CORRELATION_ID_MAX_BYTES:
        return None
    if not all(char in _VCHAR for char in value):
        return None
    return value

# Readable from anywhere below the middleware (e.g. the LLM adapter's
# per-call log line) without threading the id through every signature.
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
# Monotonic request start for audit duration_ms. Also stamped on
# request.state.audit_start because streaming finalizers outlive this
# ContextVar's scope (call_next returns before the body iterates).
request_start_var: ContextVar[float | None] = ContextVar("request_start", default=None)


async def correlation_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    # getlist, not get: `get` would quietly pick one of a duplicated pair.
    correlation_id = (
        accept_correlation_id(request.headers.getlist(CORRELATION_ID_HEADER))
        or str(uuid.uuid4())
    )
    # Stamped on the server span the OpenTelemetry middleware opened around
    # this one. That middleware always encloses this one -- it is installed by
    # wrapping build_middleware_stack, not by add_middleware -- so this runs
    # inside a recording span whenever telemetry is on. The guard covers the
    # far commoner case of telemetry being off entirely.
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute(ATTR_CORRELATION_ID, correlation_id)
    request.state.correlation_id = correlation_id
    start = time.perf_counter()
    request.state.audit_start = start
    token = correlation_id_var.set(correlation_id)
    start_token = request_start_var.set(start)
    try:
        response = await call_next(request)
    finally:
        request_start_var.reset(start_token)
        correlation_id_var.reset(token)
    response.headers[CORRELATION_ID_HEADER] = correlation_id
    return response
