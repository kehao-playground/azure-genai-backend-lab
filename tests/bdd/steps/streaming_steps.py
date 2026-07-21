import json
from collections.abc import AsyncIterator, Sequence

from behave import given, then, when

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import UpstreamServiceError, UpstreamThrottledError
from azgenai_lab.main import app
from azgenai_lab.models.chat import Message
from azgenai_lab.services.azure_openai import (
    ChatResult,
    ChatStreamEvent,
    StreamDone,
    TextDelta,
)
from azgenai_lab.services.conversation import ConversationChatService
from azgenai_lab.services.conversation_store import InMemoryConversationStore


class ScriptedChatService:
    """Protocol-compatible fake driven by a list of events / exceptions."""

    def __init__(
        self,
        script: list[ChatStreamEvent | Exception],
        open_error: Exception | None = None,
    ) -> None:
        self._script = script
        self._open_error = open_error

    async def complete(self, messages: Sequence[Message]) -> ChatResult:
        return ChatResult(message=f"[scripted] {messages[-1].content}")

    async def open_stream(self, messages: Sequence[Message]) -> AsyncIterator[ChatStreamEvent]:
        if self._open_error is not None:
            raise self._open_error

        async def stream() -> AsyncIterator[ChatStreamEvent]:
            for item in self._script:
                if isinstance(item, Exception):
                    raise item
                yield item

        return stream()


def _override(service: ScriptedChatService) -> None:
    # The orchestrator stays real: only the LLM boundary is scripted.
    wrapped = ConversationChatService(service, InMemoryConversationStore())
    app.dependency_overrides[get_conversation_service] = lambda: wrapped


def _sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        assert lines[0].startswith("event: "), f"frame without event name: {frame!r}"
        assert lines[1].startswith("data: "), f"frame without data: {frame!r}"
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


def _terminals(events: list[tuple[str, dict[str, object]]]) -> list[tuple[str, dict[str, object]]]:
    return [(name, data) for name, data in events if name in ("message.done", "error")]


@given("a valid streaming chat request")
def step_valid_streaming_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello"}


@given("a streaming chat request with an empty message")
def step_empty_streaming_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": ""}


@given("the upstream fails with throttling before the stream starts")
def step_upstream_fails_on_open(context) -> None:  # type: ignore[no-untyped-def]
    _override(ScriptedChatService([], open_error=UpstreamThrottledError("429 from upstream")))


@given("the upstream fails after streaming part of the answer")
def step_upstream_fails_mid_stream(context) -> None:  # type: ignore[no-untyped-def]
    _override(
        ScriptedChatService([TextDelta("par"), UpstreamServiceError("upstream died mid-stream")])
    )


@given('the upstream truncates the stream with reason "{reason}"')
def step_upstream_truncates(context, reason: str) -> None:  # type: ignore[no-untyped-def]
    _override(
        ScriptedChatService(
            [TextDelta("par"), StreamDone(status="incomplete", incomplete_reason=reason)]  # type: ignore[arg-type]
        )
    )


@when("I submit the request to the streaming endpoint")
def step_submit_streaming_request(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/chat/stream", json=context.payload)


@then('the response content type should be "{content_type}"')
def step_response_content_type(context, content_type: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.headers["content-type"].startswith(content_type)


@then('the stream should contain at least {count:d} "{event_name}" events')
def step_stream_contains_events(context, count: int, event_name: str) -> None:  # type: ignore[no-untyped-def]
    events = _sse_events(context.response.text)
    matching = [data for name, data in events if name == event_name]
    assert len(matching) >= count, f"expected >= {count} {event_name}, got {len(matching)}"


@then('the stream should end with exactly one terminal "{event_name}" event')
def step_stream_ends_with_terminal(context, event_name: str) -> None:  # type: ignore[no-untyped-def]
    events = _sse_events(context.response.text)
    terminals = _terminals(events)
    assert len(terminals) == 1, f"expected exactly one terminal event, got {terminals}"
    assert events[-1][0] == event_name, f"stream ended with {events[-1][0]}, not {event_name}"
    context.terminal_data = events[-1][1]


@then('the terminal event should carry status "{status}" and a correlation_id')
def step_terminal_status_and_correlation(context, status: str) -> None:  # type: ignore[no-untyped-def]
    assert context.terminal_data["status"] == status
    assert context.terminal_data["correlation_id"]


@then('the terminal event should carry status "{status}" and reason "{reason}"')
def step_terminal_status_and_reason(context, status: str, reason: str) -> None:  # type: ignore[no-untyped-def]
    assert context.terminal_data["status"] == status
    assert context.terminal_data["incomplete_reason"] == reason


@then("the error event data should use the error envelope shape")
def step_error_event_envelope_shape(context) -> None:  # type: ignore[no-untyped-def]
    data = context.terminal_data
    assert isinstance(data["error"], dict)
    assert data["error"]["code"]
    assert data["error"]["message"]
    assert data["correlation_id"]
