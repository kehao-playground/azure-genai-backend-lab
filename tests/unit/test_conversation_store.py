from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import Message
from azgenai_lab.services.conversation_store import (
    InMemoryConversationStore,
    build_conversation_store,
)


def user(text: str) -> Message:
    return Message(role="user", content=text)


def assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


def test_build_returns_the_in_memory_store() -> None:
    store = build_conversation_store(Settings(_env_file=None))

    assert isinstance(store, InMemoryConversationStore)


async def test_get_unknown_conversation_returns_none() -> None:
    assert await InMemoryConversationStore().get("never-issued") is None


async def test_append_then_get_roundtrip() -> None:
    store = InMemoryConversationStore()

    await store.append("c1", [user("hi"), assistant("hello")])

    conversation = await store.get("c1")
    assert conversation is not None
    assert conversation.id == "c1"
    assert [(m.role, m.content) for m in conversation.messages] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


async def test_append_extends_existing_history_in_order() -> None:
    store = InMemoryConversationStore()
    await store.append("c1", [user("one"), assistant("1")])

    await store.append("c1", [user("two"), assistant("2")])

    conversation = await store.get("c1")
    assert conversation is not None
    assert [m.content for m in conversation.messages] == ["one", "1", "two", "2"]


async def test_get_hands_out_a_copy_not_the_internal_state() -> None:
    store = InMemoryConversationStore()
    await store.append("c1", [user("hi"), assistant("hello")])

    leaked = await store.get("c1")
    assert leaked is not None
    leaked.messages.append(user("mutation"))

    fresh = await store.get("c1")
    assert fresh is not None
    assert len(fresh.messages) == 2
