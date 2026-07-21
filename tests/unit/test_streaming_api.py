"""SSE contract at the API boundary (Day 6).

The wire vocabulary is ours, not the upstream's: ``message.delta``,
``message.done``, ``error``. Terminal guarantee (enforced by the serializer):
when the stream ends normally, the client receives exactly one terminal event;
EOF without one is itself reported as an ``error`` event.
"""

import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient
from httpx import Response

from azgenai_lab.api.chat import get_chat_service
from azgenai_lab.core.errors import UpstreamServiceError, UpstreamThrottledError
from azgenai_lab.main import app
from azgenai_lab.services.azure_openai import (
    ChatResult,
    ChatStreamEvent,
    StreamDone,
    TextDelta,
)


def sse_events(response: Response) -> list[tuple[str, dict[str, object]]]:
    """Parse ``event:`` / ``data:`` frames; frames end with a blank line."""
    events: list[tuple[str, dict[str, object]]] = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        assert lines[0].startswith("event: "), f"frame without event name: {frame!r}"
        assert lines[1].startswith("data: "), f"frame without data: {frame!r}"
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


class ScriptedChatService:
    """Protocol-compatible fake driven by a list of events / exceptions."""

    def __init__(
        self,
        script: list[ChatStreamEvent | Exception],
        open_error: Exception | None = None,
    ) -> None:
        self._script = script
        self._open_error = open_error

    async def complete(self, message: str) -> ChatResult:
        return ChatResult(message=f"[scripted] {message}")

    async def open_stream(self, message: str) -> AsyncIterator[ChatStreamEvent]:
        if self._open_error is not None:
            raise self._open_error

        async def stream() -> AsyncIterator[ChatStreamEvent]:
            for item in self._script:
                if isinstance(item, Exception):
                    raise item
                yield item

        return stream()


def override(service: ScriptedChatService) -> None:
    app.dependency_overrides[get_chat_service] = lambda: service


def post_stream(client: TestClient) -> Response:
    return client.post("/api/v1/chat/stream", json={"message": "Hello"})


def test_successful_stream_ends_with_exactly_one_done(client: TestClient) -> None:
    response = post_stream(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = sse_events(response)
    deltas = [data for name, data in events if name == "message.delta"]
    assert len(deltas) >= 2
    assert "".join(str(d["text"]) for d in deltas) == "[fake-llm] Hello"

    terminals = [(name, data) for name, data in events if name in ("message.done", "error")]
    assert len(terminals) == 1
    name, data = terminals[0]
    assert name == "message.done"
    assert data["status"] == "completed"
    assert data["correlation_id"] == response.headers["x-correlation-id"]
    assert events[-1][0] == "message.done"


def test_pre_stream_failure_is_a_plain_http_error(client: TestClient) -> None:
    override(ScriptedChatService([], open_error=UpstreamThrottledError("429 from upstream")))

    response = post_stream(client)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "upstream_throttled"
    assert body["correlation_id"]


def test_mid_stream_failure_ends_with_error_event(client: TestClient) -> None:
    override(
        ScriptedChatService([TextDelta("par"), UpstreamServiceError("upstream died mid-stream")])
    )

    response = post_stream(client)

    assert response.status_code == 200
    events = sse_events(response)
    assert events[0] == ("message.delta", {"text": "par"})
    assert events[-1][0] == "error"
    data = events[-1][1]
    error = data["error"]
    assert isinstance(error, dict)
    assert error["code"] == "upstream_error"
    assert error["message"]
    assert data["correlation_id"] == response.headers["x-correlation-id"]
    assert sum(1 for name, _ in events if name in ("message.done", "error")) == 1


def test_eof_without_terminal_is_reported_as_error(client: TestClient) -> None:
    override(ScriptedChatService([TextDelta("par")]))  # upstream EOFs silently

    response = post_stream(client)

    assert response.status_code == 200
    events = sse_events(response)
    assert events[-1][0] == "error"
    error = events[-1][1]["error"]
    assert isinstance(error, dict)
    assert error["code"] == "upstream_error"


def test_no_events_are_emitted_after_the_terminal(client: TestClient) -> None:
    override(ScriptedChatService([StreamDone(status="completed"), TextDelta("late")]))

    response = post_stream(client)

    events = sse_events(response)
    assert events[-1][0] == "message.done"
    assert all(name != "message.delta" for name, _ in events)


def test_incomplete_done_carries_the_reason(client: TestClient) -> None:
    override(
        ScriptedChatService(
            [TextDelta("par"), StreamDone(status="incomplete", incomplete_reason="content_filter")]
        )
    )

    response = post_stream(client)

    events = sse_events(response)
    name, data = events[-1]
    assert name == "message.done"
    assert data["status"] == "incomplete"
    assert data["incomplete_reason"] == "content_filter"


def test_validation_error_uses_the_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/chat/stream", json={"message": ""})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["correlation_id"]


def test_openapi_media_types_match_runtime(client: TestClient) -> None:
    """Semantic contract: only the 200 streams; every error is a JSON envelope.

    The drift check alone can't catch this — it happily preserves a wrong
    schema. This pins media types to what the runtime actually sends
    (review r03: response_class media_type leaked onto the error responses).
    """
    responses = app.openapi()["paths"]["/api/v1/chat/stream"]["post"]["responses"]

    assert set(responses["200"]["content"]) == {"text/event-stream"}
    for status in ("400", "422", "500", "502", "503", "504"):
        content = responses[status]["content"]
        assert set(content) == {"application/json"}, f"{status}: {set(content)}"
        ref = content["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/ErrorEnvelope", f"{status}: {ref}"
