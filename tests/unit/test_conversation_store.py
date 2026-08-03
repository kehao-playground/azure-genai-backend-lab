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


TENANT_ID = "t1"


async def test_get_unknown_conversation_returns_none() -> None:
    assert await InMemoryConversationStore().get(TENANT_ID, "never-issued") is None


async def test_append_then_get_roundtrip() -> None:
    store = InMemoryConversationStore()

    await store.append(
        TENANT_ID,
        "c1",
        [user("hi"), assistant("hello")],
        items("hi", "hello"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    conversation = await store.get(TENANT_ID, "c1")
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
    await store.append(
        TENANT_ID,
        "c1",
        [user("one"), assistant("1")],
        items("one"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    await store.append(
        TENANT_ID,
        "c1",
        [user("two"), assistant("2")],
        items("two"),
        1,
        usage_tokens=0,
        first_turn_authorization_group_ids=None,
    )

    conversation = await store.get(TENANT_ID, "c1")
    assert conversation is not None
    assert [m.content for m in conversation.messages] == ["one", "1", "two", "2"]
    assert conversation.replay_items == items("one", "two")
    assert conversation.revision == 2


async def test_stale_revision_is_rejected_and_commits_nothing() -> None:
    store = InMemoryConversationStore()
    await store.append(
        TENANT_ID,
        "c1",
        [user("one"), assistant("1")],
        items("one"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    # A writer that read revision 0 lost the race: reject, don't interleave.
    with pytest.raises(ConversationConflictError):
        await store.append(
            TENANT_ID,
            "c1",
            [user("two"), assistant("2")],
            items("two"),
            0,
            usage_tokens=0,
            first_turn_authorization_group_ids=(),
        )

    conversation = await store.get(TENANT_ID, "c1")
    assert conversation is not None
    assert len(conversation.messages) == 2
    assert conversation.revision == 1


async def test_get_is_scoped_to_the_tenant_that_created_the_conversation() -> None:
    store = InMemoryConversationStore()
    await store.append(
        TENANT_ID,
        "c1",
        [user("hi"), assistant("hello")],
        items("hi"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    # Same conversation_id, different tenant: indistinguishable from absence.
    assert await store.get("t2", "c1") is None
    # The owning tenant still sees it.
    assert await store.get(TENANT_ID, "c1") is not None


async def test_append_does_not_cross_tenant_boundaries() -> None:
    store = InMemoryConversationStore()
    await store.append(
        TENANT_ID,
        "c1",
        [user("t1-turn")],
        items("t1-turn"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    # A different tenant appending under the same conversation_id at
    # revision 0 starts an independent history, not a continuation.
    await store.append(
        "t2",
        "c1",
        [user("t2-turn")],
        items("t2-turn"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    t1_conversation = await store.get(TENANT_ID, "c1")
    t2_conversation = await store.get("t2", "c1")
    assert t1_conversation is not None
    assert t2_conversation is not None
    assert [m.content for m in t1_conversation.messages] == ["t1-turn"]
    assert [m.content for m in t2_conversation.messages] == ["t2-turn"]


class Uncopyable:
    def __deepcopy__(self, memo: dict[int, Any]) -> "Uncopyable":
        raise RuntimeError("copy failed")


async def test_failed_append_is_all_or_nothing() -> None:
    store = InMemoryConversationStore()
    poisoned: list[ReplayItem] = [{"role": "user", "content": Uncopyable()}]

    with pytest.raises(RuntimeError):
        await store.append(
            TENANT_ID,
            "c1",
            [user("hello"), assistant("hi")],
            poisoned,
            0,
            usage_tokens=0,
            first_turn_authorization_group_ids=(),
        )

    # Nothing may survive a failed append — no transcript half, no revision bump.
    assert await store.get(TENANT_ID, "c1") is None


async def test_get_hands_out_a_copy_not_the_internal_state() -> None:
    store = InMemoryConversationStore()
    await store.append(
        TENANT_ID,
        "c1",
        [user("hi"), assistant("hello")],
        items("hi"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    leaked = await store.get(TENANT_ID, "c1")
    assert leaked is not None
    leaked.messages.append(user("mutation"))
    leaked.replay_items[0]["content"] = "rewritten"

    fresh = await store.get(TENANT_ID, "c1")
    assert fresh is not None
    assert len(fresh.messages) == 2
    assert fresh.replay_items[0]["content"] == "hi"


async def test_messages_are_frozen_so_aliases_cannot_rewrite_history() -> None:
    store = InMemoryConversationStore()
    appended = user("hi")
    await store.append(
        TENANT_ID,
        "c1",
        [appended, assistant("hello")],
        items("hi"),
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    with pytest.raises(pydantic.ValidationError):
        appended.content = "rewritten"  # type: ignore[misc]

    conversation = await store.get(TENANT_ID, "c1")
    assert conversation is not None
    assert conversation.messages[0].content == "hi"


async def test_appended_replay_items_are_copied_not_aliased() -> None:
    store = InMemoryConversationStore()
    caller_items = items("hi")
    await store.append(
        TENANT_ID,
        "c1",
        [user("hi"), assistant("hello")],
        caller_items,
        0,
        usage_tokens=0,
        first_turn_authorization_group_ids=(),
    )

    caller_items[0]["content"] = "rewritten"

    conversation = await store.get(TENANT_ID, "c1")
    assert conversation is not None
    assert conversation.replay_items[0]["content"] == "hi"


async def test_scope_round_trips_including_empty() -> None:
    store = InMemoryConversationStore()
    await store.append(
        "t1", "c1", [Message(role="user", content="hi")], [], 0, 10,
        first_turn_authorization_group_ids=("g1", "g2"),
    )
    loaded = await store.get("t1", "c1")
    assert loaded is not None
    assert loaded.authorization_group_ids == ("g1", "g2")

    await store.append(
        "t1", "c2", [Message(role="user", content="hi")], [], 0, 10,
        first_turn_authorization_group_ids=(),
    )
    loaded2 = await store.get("t1", "c2")
    assert loaded2 is not None
    assert loaded2.authorization_group_ids == ()


async def test_first_append_without_scope_rejected_no_mutation() -> None:
    store = InMemoryConversationStore()
    with pytest.raises(ValueError):
        await store.append(
            "t1", "c1", [Message(role="user", content="hi")], [], 0, 10,
            first_turn_authorization_group_ids=None,
        )
    assert await store.get("t1", "c1") is None


async def test_continuation_carrying_scope_rejected_even_if_identical() -> None:
    store = InMemoryConversationStore()
    await store.append(
        "t1", "c1", [Message(role="user", content="hi")], [], 0, 10,
        first_turn_authorization_group_ids=("g1",),
    )
    with pytest.raises(ValueError):
        await store.append(
            "t1", "c1", [Message(role="user", content="more")], [], 1, 10,
            first_turn_authorization_group_ids=("g1",),
        )
    loaded = await store.get("t1", "c1")
    assert loaded is not None
    assert loaded.revision == 1  # zero mutation
    assert len(loaded.messages) == 1
    assert loaded.authorization_group_ids == ("g1",)


async def test_revision_conflict_precedes_scope_validation() -> None:
    store = InMemoryConversationStore()
    await store.append(
        "t1", "c1", [Message(role="user", content="hi")], [], 0, 10,
        first_turn_authorization_group_ids=("g1",),
    )
    # Wrong revision AND illegally-carried scope: the conflict must win.
    with pytest.raises(ConversationConflictError):
        await store.append(
            "t1", "c1", [Message(role="user", content="more")], [], 5, 10,
            first_turn_authorization_group_ids=("g1",),
        )
