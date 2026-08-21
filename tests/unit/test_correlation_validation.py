"""Day 27: the inbound X-Correlation-Id is validated before it is trusted.

Day 5 made the header a contract and Day 22 recorded the gap it left -- the
value is caller-controlled free text, echoed back verbatim. Now that the same
string is copied onto spans, it gets bounded first: exactly one header, 1-128
bytes, ASCII VCHAR.

Rejection never means a 4xx. The error contract is untouched; the backend just
stops trusting the caller's string and uses its own. The cost of that choice
is that the echoed header may differ from the one sent, which
docs/api-conventions.md now states.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace

from azgenai_lab.core.correlation import CORRELATION_ID_MAX_BYTES, accept_correlation_id
from azgenai_lab.core.telemetry import ATTR_CORRELATION_ID, instrumented_httpx_client


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["abc-123"], "abc-123"),
        (["x" * CORRELATION_ID_MAX_BYTES], "x" * CORRELATION_ID_MAX_BYTES),
        (["!"], "!"),  # 0x21, the low end of VCHAR
        (["~"], "~"),  # 0x7E, the high end
    ],
)
def test_accepted_values_are_used_verbatim(raw: list[str], expected: str) -> None:
    assert accept_correlation_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        [],  # absent
        [""],  # empty
        ["x" * (CORRELATION_ID_MAX_BYTES + 1)],  # too long
        ["a b"],  # space is not VCHAR
        ["a\tb"],  # control character
        ["ab\n"],  # trailing newline
        ["中文"],  # multi-byte
        ["a", "b"],  # duplicated header
        [" abc"],  # leading space: rejected, not trimmed
    ],
)
def test_rejected_values_fall_back_to_a_generated_id(raw: list[str]) -> None:
    assert accept_correlation_id(raw) is None


def test_no_normalisation_of_accepted_values() -> None:
    # Trimming would make the echoed value differ from the accepted one in a
    # second, quieter way. Mixed case survives for the same reason.
    assert accept_correlation_id(["AbC-123"]) == "AbC-123"


def test_server_span_carries_the_accepted_correlation_id(
    telemetry_client: TestClient, span_exporter
) -> None:
    telemetry_client.post(
        "/api/v1/chat",
        json={"message": "hi"},
        headers={"X-Correlation-Id": "accepted-value"},
    )

    server = next(
        span
        for span in span_exporter.get_finished_spans()
        if span.kind is trace.SpanKind.SERVER
    )
    # Note what this does and does not prove. It proves the id reaches the
    # server span. It is *not* a middleware-ordering probe: instrument_app
    # wraps build_middleware_stack rather than adding middleware, so the
    # OpenTelemetry span encloses this middleware regardless of registration
    # order -- confirmed by moving the instrument call earlier and watching
    # this stay green.
    assert (server.attributes or {})[ATTR_CORRELATION_ID] == "accepted-value"


def test_span_header_and_response_agree_when_the_header_is_rejected(
    telemetry_client: TestClient, span_exporter
) -> None:
    response = telemetry_client.post(
        "/api/v1/chat",
        json={"message": "hi"},
        headers={"X-Correlation-Id": "has space"},
    )

    server = next(
        span
        for span in span_exporter.get_finished_spans()
        if span.kind is trace.SpanKind.SERVER
    )
    stamped = (server.attributes or {})[ATTR_CORRELATION_ID]
    assert stamped != "has space"
    # One id per request, even when it is not the caller's: the span, the
    # echoed header and the envelope all say the same thing.
    assert response.headers["X-Correlation-Id"] == stamped


async def test_outbound_carries_traceparent_and_not_our_header(
    telemetry_enabled: None, span_exporter
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({key.lower(): value for key, value in request.headers.items()})
        return httpx.Response(200, json={})

    from azgenai_lab.core.config import get_settings
    from azgenai_lab.core.telemetry import configure_telemetry

    configure_telemetry(get_settings())
    client = instrumented_httpx_client(transport=httpx.MockTransport(handler))
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("caller"):
        await client.get("https://example.invalid/thing")
    await client.aclose()

    # W3C trace context is what an upstream understands.
    assert "traceparent" in seen
    # Our id is this backend's own contract, not something Azure OpenAI or AI
    # Search knows. Forwarding it would copy caller-controlled text to one more
    # place for no one's benefit.
    assert "x-correlation-id" not in seen
