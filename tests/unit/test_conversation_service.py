"""Turn-commit contract of the conversation orchestrator (Day 7).

A turn enters the history atomically — visible transcript plus provider
replay items — and only when the reply is one the client keeps: non-streaming
success, stream ``completed``, or stream ``incomplete``/``max_output_tokens``.
Failed or discarded turns leave no trace. Turns on one conversation are
serialized (review r01 finding 2); storage failures map to ``StorageError``
(finding 3); empty non-streaming replies are upstream failures, never a 200
with a ghost id (finding 4).
"""

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from azgenai_lab.core.errors import StorageError, UpstreamServiceError
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.services.azure_openai import (
    ChatResult,
    ChatStreamEvent,
    StreamDone,
    TextDelta,
    _fake_output_item,
)
from azgenai_lab.services.conversation import (
    ConversationChatService,
    ConversationNotFoundError,
)
from azgenai_lab.services.conversation_store import InMemoryConversationStore


class SpyChatService:
    """Records the replay context it receives; replies from a script."""

    def __init__(
        self,
        reply: str = "pong",
        stream_script: list[ChatStreamEvent | Exception] | None = None,
        open_error: Exception | None = None,
    ) -> None:
        self.received: list[list[ReplayItem]] = []
        self._reply = reply
        self._script = stream_script or []
        self._open_error = open_error

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        self.received.append(list(items))
        if self._open_error is not None:
            raise self._open_error
        replay = (_fake_output_item(self._reply),) if self._reply else ()
        return ChatResult(message=self._reply, model="spy", replay_items=replay)

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        self.received.append(list(items))
        if self._open_error is not None:
            raise self._open_error

        async def stream() -> AsyncIterator[ChatStreamEvent]:
            for item in self._script:
                if isinstance(item, Exception):
                    raise item
                yield item

        return stream()


class FailingStore(InMemoryConversationStore):
    async def append(
        self,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
    ) -> None:
        raise RuntimeError("disk on fire")


def make_service(
    spy: SpyChatService,
) -> tuple[ConversationChatService, InMemoryConversationStore]:
    store = InMemoryConversationStore()
    return ConversationChatService(spy, store), store


def done(replay_text: str | None = "pong", **kwargs: object) -> StreamDone:
    replay = (_fake_output_item(replay_text),) if replay_text is not None else ()
    return StreamDone(replay_items=replay, **kwargs)  # type: ignore[arg-type]


async def history(store: InMemoryConversationStore, conversation_id: str) -> list[tuple[str, str]]:
    conversation = await store.get(conversation_id)
    if conversation is None:
        return []
    return [(m.role, m.content) for m in conversation.messages]


async def replay(store: InMemoryConversationStore, conversation_id: str) -> list[ReplayItem]:
    conversation = await store.get(conversation_id)
    if conversation is None:
        return []
    return conversation.replay_items


async def test_first_turn_issues_an_id_and_commits_transcript_and_replay() -> None:
    spy = SpyChatService(reply="hello there")
    service, store = make_service(spy)

    conversation_id, result = await service.complete("hi", None)

    assert result.message == "hello there"
    assert spy.received == [[{"role": "user", "content": "hi"}]]
    assert await history(store, conversation_id) == [
        ("user", "hi"),
        ("assistant", "hello there"),
    ]
    assert await replay(store, conversation_id) == [
        {"role": "user", "content": "hi"},
        _fake_output_item("hello there"),
    ]


async def test_follow_up_turn_replays_prior_output_items_verbatim() -> None:
    spy = SpyChatService()
    service, _ = make_service(spy)
    conversation_id, _ = await service.complete("one", None)

    await service.complete("two", conversation_id)

    # The second call must resend the full replay context: user item, the
    # response's output item (reasoning context travels here), new user item.
    assert spy.received[1] == [
        {"role": "user", "content": "one"},
        _fake_output_item("pong"),
        {"role": "user", "content": "two"},
    ]


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


async def test_empty_reply_is_an_upstream_failure_not_a_ghost_id() -> None:
    spy = SpyChatService(reply="")
    service, store = make_service(spy)

    with pytest.raises(UpstreamServiceError):
        await service.complete("hi", None)

    assert store._messages == {}  # nothing committed, no id issued to anyone


async def test_store_failure_maps_to_storage_error() -> None:
    spy = SpyChatService()
    service = ConversationChatService(spy, FailingStore())

    with pytest.raises(StorageError):
        await service.complete("hi", None)


async def test_concurrent_turns_on_one_conversation_are_serialized() -> None:
    spy = SpyChatService()
    service, store = make_service(spy)
    conversation_id, _ = await service.complete("first", None)

    # Fire both turns concurrently; without the per-conversation lock both
    # would read the same 2-item snapshot (r01 finding 2's barrier probe).
    await asyncio.gather(
        service.complete("A", conversation_id),
        service.complete("B", conversation_id),
    )

    lengths = sorted(len(received) for received in spy.received[1:])
    assert lengths == [3, 5]  # the second turn saw the first turn's commit
    assert len(await history(store, conversation_id)) == 6


async def test_stream_completed_commits_transcript_and_replay() -> None:
    spy = SpyChatService(stream_script=[TextDelta("po"), TextDelta("ng"), done(status="completed")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    received = [event async for event in events]

    assert isinstance(received[-1], StreamDone)
    assert await history(store, conversation_id) == [("user", "hi"), ("assistant", "pong")]
    assert await replay(store, conversation_id) == [
        {"role": "user", "content": "hi"},
        _fake_output_item("pong"),
    ]


async def test_stream_commits_before_the_terminal_is_delivered() -> None:
    spy = SpyChatService(stream_script=[TextDelta("pong"), done(status="completed")])
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
            done(replay_text="par", status="incomplete", incomplete_reason="max_output_tokens"),
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
            done(replay_text="par", status="incomplete", incomplete_reason=reason),
        ]
    )
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    _ = [event async for event in events]

    assert await history(store, conversation_id) == []
    assert await replay(store, conversation_id) == []


async def test_mid_stream_failure_is_not_committed() -> None:
    spy = SpyChatService(stream_script=[TextDelta("par"), UpstreamServiceError("died")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    with pytest.raises(UpstreamServiceError):
        _ = [event async for event in events]

    assert await history(store, conversation_id) == []


async def test_stream_store_failure_surfaces_as_storage_error() -> None:
    spy = SpyChatService(stream_script=[TextDelta("pong"), done(status="completed")])
    service = ConversationChatService(spy, FailingStore())

    _, events = await service.open_stream("hi", None)
    with pytest.raises(StorageError):
        _ = [event async for event in events]


async def test_disconnect_before_terminal_aborts_the_turn_uncommitted() -> None:
    spy = SpyChatService(stream_script=[TextDelta("a"), TextDelta("b"), done(status="completed")])
    service, store = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    async for _ in events:
        break  # client went away after the first delta
    await events.aclose()  # type: ignore[attr-defined]

    assert await history(store, conversation_id) == []


async def test_stream_releases_the_lock_after_disconnect() -> None:
    spy = SpyChatService(stream_script=[TextDelta("a"), done(status="completed")])
    service, _ = make_service(spy)

    conversation_id, events = await service.open_stream("hi", None)
    async for _ in events:
        break
    await events.aclose()  # type: ignore[attr-defined]

    # A follow-up turn must not deadlock on the abandoned stream's lock.
    async with asyncio.timeout(1):
        await service.complete("next", None)
        with pytest.raises(ConversationNotFoundError):
            await service.complete("next", conversation_id)


async def test_stream_unknown_conversation_id_raises_before_opening_upstream() -> None:
    spy = SpyChatService()
    service, _ = make_service(spy)

    with pytest.raises(ConversationNotFoundError):
        await service.open_stream("hi", "never-issued")

    assert spy.received == []


async def test_stream_follow_up_replays_prior_output_items() -> None:
    spy = SpyChatService(stream_script=[TextDelta("pong"), done(status="completed")])
    service, _ = make_service(spy)
    conversation_id, _ = await service.complete("one", None)

    _, events = await service.open_stream("two", conversation_id)
    _ = [event async for event in events]

    assert spy.received[1] == [
        {"role": "user", "content": "one"},
        _fake_output_item("pong"),
        {"role": "user", "content": "two"},
    ]
