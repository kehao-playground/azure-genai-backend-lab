from typing import Any

import pydantic
import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.services.conversation_store import (
    ConversationConflictError,
    InMemoryConversationStore,
    build_conversation_store,
)


def user(text: str) -> Message:
    return Message(role="user", content=text)


def assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


def items(*texts: str) -> list[ReplayItem]:
    return [{"role": "user", "content": text} for text in texts]


def test_build_returns_the_in_memory_store() -> None:
    store = build_conversation_store(Settings(_env_file=None))

    assert isinstance(store, InMemoryConversationStore)


async def test_get_unknown_conversation_returns_none() -> None:
    assert await InMemoryConversationStore().get("never-issued") is None


async def test_append_then_get_roundtrip() -> None:
    store = InMemoryConversationStore()

    await store.append(
        "c1", [user("hi"), assistant("hello")], items("hi", "hello"), 0, usage_tokens=0
    )

    conversation = await store.get("c1")
    assert conversation is not None
    assert conversation.id == "c1"
    assert [(m.role, m.content) for m in conversation.messages] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]
    assert conversation.replay_items == items("hi", "hello")
    assert conversation.revision == 1


async def test_append_extends_existing_history_in_order() -> None:
    store = InMemoryConversationStore()
    await store.append("c1", [user("one"), assistant("1")], items("one"), 0, usage_tokens=0)

    await store.append("c1", [user("two"), assistant("2")], items("two"), 1, usage_tokens=0)

    conversation = await store.get("c1")
    assert conversation is not None
    assert [m.content for m in conversation.messages] == ["one", "1", "two", "2"]
    assert conversation.replay_items == items("one", "two")
    assert conversation.revision == 2


async def test_stale_revision_is_rejected_and_commits_nothing() -> None:
    store = InMemoryConversationStore()
    await store.append("c1", [user("one"), assistant("1")], items("one"), 0, usage_tokens=0)

    # A writer that read revision 0 lost the race: reject, don't interleave.
    with pytest.raises(ConversationConflictError):
        await store.append("c1", [user("two"), assistant("2")], items("two"), 0, usage_tokens=0)

    conversation = await store.get("c1")
    assert conversation is not None
    assert len(conversation.messages) == 2
    assert conversation.revision == 1


class Uncopyable:
    def __deepcopy__(self, memo: dict[int, Any]) -> "Uncopyable":
        raise RuntimeError("copy failed")


async def test_failed_append_is_all_or_nothing() -> None:
    store = InMemoryConversationStore()
    poisoned: list[ReplayItem] = [{"role": "user", "content": Uncopyable()}]

    with pytest.raises(RuntimeError):
        await store.append("c1", [user("hello"), assistant("hi")], poisoned, 0, usage_tokens=0)

    # Nothing may survive a failed append — no transcript half, no revision bump.
    assert await store.get("c1") is None


async def test_get_hands_out_a_copy_not_the_internal_state() -> None:
    store = InMemoryConversationStore()
    await store.append("c1", [user("hi"), assistant("hello")], items("hi"), 0, usage_tokens=0)

    leaked = await store.get("c1")
    assert leaked is not None
    leaked.messages.append(user("mutation"))
    leaked.replay_items[0]["content"] = "rewritten"

    fresh = await store.get("c1")
    assert fresh is not None
    assert len(fresh.messages) == 2
    assert fresh.replay_items[0]["content"] == "hi"


async def test_messages_are_frozen_so_aliases_cannot_rewrite_history() -> None:
    store = InMemoryConversationStore()
    appended = user("hi")
    await store.append("c1", [appended, assistant("hello")], items("hi"), 0, usage_tokens=0)

    with pytest.raises(pydantic.ValidationError):
        appended.content = "rewritten"  # type: ignore[misc]

    conversation = await store.get("c1")
    assert conversation is not None
    assert conversation.messages[0].content == "hi"


async def test_appended_replay_items_are_copied_not_aliased() -> None:
    store = InMemoryConversationStore()
    caller_items = items("hi")
    await store.append("c1", [user("hi"), assistant("hello")], caller_items, 0, usage_tokens=0)

    caller_items[0]["content"] = "rewritten"

    conversation = await store.get("c1")
    assert conversation is not None
    assert conversation.replay_items[0]["content"] == "hi"
