"""Streaming contract at the service boundary (Day 6).

The adapter owns the translation from Responses API typed events to our
domain stream events; upstream vocabulary must never leak past this module.
``open_stream`` is eager: the upstream call is awaited before any event is
yielded, so pre-stream failures raise here — not mid-iteration.
"""

import hashlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from azgenai_lab.core.errors import (
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
)
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.azure_openai import (
    AzureOpenAIChatService,
    ChatStreamEvent,
    FakeChatService,
    StreamDone,
    TextDelta,
)

_PROMPT_TEXT = "You are T."
PROMPT = PromptTemplate(
    name="default_chat",
    version=1,
    description="d",
    text=_PROMPT_TEXT,
    sha256=hashlib.sha256(_PROMPT_TEXT.encode("utf-8")).hexdigest(),
)


async def collect(events: AsyncIterator[ChatStreamEvent]) -> list[ChatStreamEvent]:
    return [event async for event in events]


def user_items(*texts: str) -> list[ReplayItem]:
    return [{"role": "user", "content": text} for text in texts]


async def test_fake_stream_yields_deltas_then_done() -> None:
    events = await collect(await FakeChatService().open_stream(user_items("hello")))

    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert len(deltas) >= 2
    assert "".join(d.text for d in deltas) == "[fake-llm] hello"
    assert isinstance(events[-1], StreamDone)
    assert events[-1].status == "completed"
    assert events[-1].replay_items  # the fake supplies replay context too


async def test_fake_stream_makes_received_history_visible() -> None:
    events = await collect(await FakeChatService().open_stream(user_items("one", "two")))

    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert "".join(d.text for d in deltas) == "[fake-llm] two (history=1)"


class StubUpstreamStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.closed = False

    def __aiter__(self) -> "StubUpstreamStream":
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        event = self._events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    async def close(self) -> None:
        self.closed = True


class StubResponses:
    def __init__(self, stream: StubUpstreamStream | Exception) -> None:
        self._stream = stream
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._stream, Exception):
            raise self._stream
        return self._stream


def make_service(
    stream: StubUpstreamStream | Exception,
) -> tuple[AzureOpenAIChatService, StubResponses]:
    responses = StubResponses(stream)
    client = SimpleNamespace(responses=responses)
    return (
        AzureOpenAIChatService(
            cast(AsyncOpenAI, client), "chat-mini", prompt=PROMPT, max_output_tokens=1000
        ),
        responses,
    )


class StubOutputItem:
    """Mimics an SDK output item: only model_dump is used at the boundary."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self._payload)


REASONING_ITEM = {"type": "reasoning", "encrypted_content": "opaque-blob"}


def delta(text: str) -> Any:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def completed(output: list[Any] | None = None, usage: Any = None) -> Any:
    return SimpleNamespace(
        type="response.completed", response=SimpleNamespace(output=output or [], usage=usage)
    )


def incomplete(reason: str) -> Any:
    return SimpleNamespace(
        type="response.incomplete",
        response=SimpleNamespace(
            incomplete_details=SimpleNamespace(reason=reason), output=[], usage=None
        ),
    )


async def test_real_stream_is_requested_with_store_false() -> None:
    service, responses = make_service(StubUpstreamStream([delta("pong"), completed()]))

    await collect(await service.open_stream(user_items("ping")))

    call = responses.calls[0]
    assert call["model"] == "chat-mini"
    assert call["input"] == [{"role": "user", "content": "ping"}]
    assert call["store"] is False
    assert call["include"] == ["reasoning.encrypted_content"]
    assert call["max_output_tokens"] == 1000  # Day 9: per-call output cap
    assert call["stream"] is True


async def test_terminal_event_carries_the_reported_usage() -> None:
    from types import SimpleNamespace as NS

    usage = NS(input_tokens=20, output_tokens=7, total_tokens=27)
    service, _ = make_service(StubUpstreamStream([delta("pong"), completed(usage=usage)]))

    events = await collect(await service.open_stream(user_items("ping")))

    assert isinstance(events[-1], StreamDone)
    assert events[-1].usage is not None
    assert events[-1].usage.total_tokens == 27


async def test_terminal_event_carries_the_response_output_as_replay_items() -> None:
    service, _ = make_service(
        StubUpstreamStream([delta("pong"), completed(output=[StubOutputItem(REASONING_ITEM)])])
    )

    events = await collect(await service.open_stream(user_items("ping")))

    assert isinstance(events[-1], StreamDone)
    assert events[-1].replay_items == (REASONING_ITEM,)


async def test_real_stream_translates_deltas_and_completed() -> None:
    service, _ = make_service(StubUpstreamStream([delta("po"), delta("ng"), completed()]))

    events = await collect(await service.open_stream(user_items("ping")))

    assert events == [TextDelta("po"), TextDelta("ng"), StreamDone(status="completed")]


async def test_real_stream_ignores_unknown_event_types() -> None:
    noise = SimpleNamespace(type="response.output_item.added")
    service, _ = make_service(StubUpstreamStream([noise, delta("pong"), completed()]))

    events = await collect(await service.open_stream(user_items("ping")))

    assert events == [TextDelta("pong"), StreamDone(status="completed")]


@pytest.mark.parametrize(
    ("upstream_reason", "our_reason"),
    [
        ("max_output_tokens", "max_output_tokens"),
        ("content_filter", "content_filter"),
        ("something_new", "other"),
    ],
)
async def test_real_stream_maps_incomplete_reasons(upstream_reason: str, our_reason: str) -> None:
    service, _ = make_service(StubUpstreamStream([delta("po"), incomplete(upstream_reason)]))

    events = await collect(await service.open_stream(user_items("ping")))

    assert events[-1] == StreamDone(status="incomplete", incomplete_reason=our_reason)


async def test_pre_stream_throttling_raises_before_iteration() -> None:
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    response = httpx.Response(429, request=request)
    service, _ = make_service(openai.RateLimitError("rate limited", response=response, body=None))

    # The failure must surface at open_stream (before any byte is sent to the
    # client), not on first iteration — this is the two-phase error boundary.
    with pytest.raises(UpstreamThrottledError):
        await service.open_stream(user_items("ping"))


async def test_failed_event_raises_upstream_error_mid_stream() -> None:
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(code="server_error", message="boom")),
    )
    service, _ = make_service(StubUpstreamStream([delta("po"), failed]))

    events = await service.open_stream(user_items("ping"))
    received: list[ChatStreamEvent] = []
    with pytest.raises(UpstreamServiceError):
        async for event in events:
            received.append(event)

    assert received == [TextDelta("po")]


async def test_error_event_raises_upstream_error_mid_stream() -> None:
    error_event = SimpleNamespace(type="error", code="server_error", message="boom")
    service, _ = make_service(StubUpstreamStream([error_event]))

    events = await service.open_stream(user_items("ping"))
    with pytest.raises(UpstreamError):
        await collect(events)


async def test_sdk_exception_mid_stream_is_translated() -> None:
    service, _ = make_service(
        StubUpstreamStream(
            [delta("po"), openai.APIConnectionError(request=httpx.Request("POST", "https://x"))]
        )
    )

    events = await service.open_stream(user_items("ping"))
    with pytest.raises(UpstreamError):
        await collect(events)


async def test_upstream_stream_closed_when_consumer_stops_early() -> None:
    stream = StubUpstreamStream([delta("a"), delta("b"), completed()])
    service, _ = make_service(stream)

    events = await service.open_stream(user_items("ping"))
    async for _ in events:
        break  # client disconnected after the first delta
    await events.aclose()  # type: ignore[attr-defined]

    assert stream.closed


async def test_upstream_stream_closed_after_normal_completion() -> None:
    stream = StubUpstreamStream([delta("a"), completed()])
    service, _ = make_service(stream)

    await collect(await service.open_stream(user_items("ping")))

    assert stream.closed


async def test_fake_stream_marks_prompt_delivery() -> None:
    service = FakeChatService(prompt=PROMPT)
    events = [e async for e in await service.open_stream([{"role": "user", "content": "ping"}])]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "(prompt=default_chat@1)" in text


async def test_real_stream_sends_prompt_as_instructions() -> None:
    service, responses = make_service(StubUpstreamStream([delta("pong"), completed()]))

    await collect(await service.open_stream(user_items("ping")))

    assert responses.calls[0]["instructions"] == PROMPT.text
