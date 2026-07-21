"""Turn-commit contract of the conversation orchestrator (Day 7).

A turn (user message + assistant reply) enters the history atomically, and
only when the reply is one the client keeps: non-streaming success, stream
``completed``, or stream ``incomplete``/``max_output_tokens``. Failed or
discarded turns leave no trace, so retries cannot duplicate history.
"""

from collections.abc import AsyncIterator, Sequence

import pytest

from azgenai_lab.core.errors import UpstreamServiceError
from azgenai_lab.models.chat import Message
from azgenai_lab.services.azure_openai import (
    ChatResult,
    ChatStreamEvent,
    StreamDone,
    TextDelta,
)
from azgenai_lab.services.conversation import (
    ConversationChatService,
    ConversationNotFoundError,
)
from azgenai_lab.services.conversation_store import InMemoryConversationStore


class SpyChatService:
    """Records the history it receives; replies from a script."""

    def __init__(
        self,
        reply: str = "pong",
        stream_script: list[ChatStreamEvent | Exception] | None = None,
        open_error: Exception | None = None,
    ) -> None:
        self.received: list[list[Message]] = []
        self._reply = reply
        self._script = stream_script or []
        self._open_error = open_error

    async def complete(self, messages: Sequence[Message]) -> ChatResult:
        self.received.append(list(messages))
        if self._open_error is not None:
            raise self._open_error
        return ChatResult(message=self._reply, model="spy")

    async def open_stream(self, messages: Sequence[Message]) -> AsyncIterator[ChatStreamEvent]:
        self.received.append(list(messages))
        if self._open_error is not None:
            raise self._open_error

        async def stream() -> AsyncIterator[ChatStreamEvent]:
            for item in self._script:
                if isinstance(item, Exception):
                    raise item
                yield item

        return stream()


def make_service(
    spy: SpyChatService,
) -> tuple[ConversationChatService, InMemoryConversationStore]:
    store = InMemoryConversationStore()
    return ConversationChatService(spy, store), store


async def history(store: InMemoryConversationStore, conversation_id: str) -> list[tuple[str, str]]:
    conversation = await store.get(conversation_id)
    if conversation is None:
        return []
    return [(m.role, m.content) for m in conversation.messages]


async def test_first_turn_issues_an_id_and_commits_both_messages() -> None:
    spy = SpyChatService(reply="hello there")
    service, store = make_service(spy)

    conversation_id, result = await service.complete("hi", None)

    assert result.message == "hello there"
    assert spy.received == [[Message(role="user", content="hi")]]
    assert await history(store, conversation_id) == [
        ("user", "hi"),
        ("assistant", "hello there"),
    ]


async def test_follow_up_turn_sends_the_full_history() -> None:
    spy = SpyChatService()
    service, store = make_service(spy)
    conversation_id, _ = await service.complete("one", None)

    await service.complete("two", conversation_id)

    assert [m.content for m in spy.received[1]] == ["one", "pong", "two"]
    assert len(await history(store, conversation_id)) == 4


async def test_unknown_conversation_id_raises_without_calling_the_llm() -> None:
    spy = SpyChatService()
    service, _ = make_service(spy)

    with pytest.raises(ConversationNotFoundError):
        await service.complete("hi", "never-issued")

    assert spy.received == []


async def test_failed_turn_leaves_no_trace() -> None:
    spy = SpyChatService()
    service, store = make_service(spy)
    conversation_id, _ = await service.complete("one", None)

    spy._open_error = UpstreamServiceError("boom")
    with pytest.raises(UpstreamServiceError):
        await service.complete("two", conversation_id)

    assert len(await history(store, conversation_id)) == 2  # only the first turn


async def test_empty_reply_is_returned_but_not_committed() -> None:
    spy = SpyChatService(reply="")
    service, store = make_service(spy)

    conversation_id, result = await service.complete("hi", None)

    assert result.message == ""
    assert await history(store, conversation_id) == []


async def test_stream_completed_commits_the_joined_text() -> None:
    spy = SpyChatService(stream_script=[TextDelta("po"), TextDelta("ng"), StreamDone("completed")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    received = [event async for event in events]

    assert received[-1] == StreamDone(status="completed")
    assert await history(store, conversation_id) == [("user", "hi"), ("assistant", "pong")]


async def test_stream_commits_before_the_terminal_is_delivered() -> None:
    spy = SpyChatService(stream_script=[TextDelta("pong"), StreamDone("completed")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    async for event in events:
        if isinstance(event, StreamDone):
            # When the client sees message.done the history must already exist.
            assert len(await history(store, conversation_id)) == 2


async def test_stream_max_output_tokens_commits_the_partial_text() -> None:
    spy = SpyChatService(
        stream_script=[
            TextDelta("par"),
            StreamDone(status="incomplete", incomplete_reason="max_output_tokens"),
        ]
    )
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    _ = [event async for event in events]

    assert await history(store, conversation_id) == [("user", "hi"), ("assistant", "par")]


@pytest.mark.parametrize("reason", ["content_filter", "other"])
async def test_stream_discarded_text_is_not_committed(reason: str) -> None:
    spy = SpyChatService(
        stream_script=[
            TextDelta("par"),
            StreamDone(status="incomplete", incomplete_reason=reason),  # type: ignore[arg-type]
        ]
    )
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    _ = [event async for event in events]

    assert await history(store, conversation_id) == []


async def test_mid_stream_failure_is_not_committed() -> None:
    spy = SpyChatService(stream_script=[TextDelta("par"), UpstreamServiceError("died")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    with pytest.raises(UpstreamServiceError):
        _ = [event async for event in events]

    assert await history(store, conversation_id) == []


async def test_client_disconnect_aborts_the_turn_uncommitted() -> None:
    spy = SpyChatService(stream_script=[TextDelta("a"), TextDelta("b"), StreamDone("completed")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    async for _ in events:
        break  # client went away after the first delta
    await events.aclose()  # type: ignore[attr-defined]

    assert await history(store, conversation_id) == []


async def test_stream_unknown_conversation_id_raises_before_opening_upstream() -> None:
    spy = SpyChatService()
    service, _ = make_service(spy)

    with pytest.raises(ConversationNotFoundError):
        await service.open_stream("hi", "never-issued")

    assert spy.received == []


async def test_stream_follow_up_sends_the_full_history() -> None:
    spy = SpyChatService(stream_script=[TextDelta("pong"), StreamDone("completed")])
    service, _ = make_service(spy)
    conversation_id, _ = await service.complete("one", None)

    _, events = await service.open_stream("two", conversation_id)
    _ = [event async for event in events]

    assert [m.content for m in spy.received[1]] == ["one", "pong", "two"]
