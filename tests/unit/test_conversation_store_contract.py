"""The portable contract every ``ConversationStore`` implementation must satisfy.

Day 30 review R4: the Protocol expresses signatures, and the suites next to
`InMemoryConversationStore` exercise *that* implementation. Neither states the
behaviour an external adapter would have to reproduce, so a second store could
satisfy the Protocol and strict mypy while quietly breaking the admission and
ledger invariant the token budget depends on.

This file is that statement, written as executable requirements. Every future
adapter (Cosmos DB, PostgreSQL, Redis) registers itself in ``STORE_FACTORIES``
and must pass unchanged. The requirements are deliberately implementation-blind:
they use only the Protocol surface.
"""

from collections.abc import Callable, Sequence

import pytest

from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.services.conversation_store import (
    ConversationConflictError,
    ConversationStore,
    InMemoryConversationStore,
)

StoreFactory = Callable[[], ConversationStore]

# Register every implementation here. One entry today; the point of the file is
# that adding the second one costs nothing but a line.
STORE_FACTORIES: list[tuple[str, StoreFactory]] = [
    ("in-memory", InMemoryConversationStore),
]

TENANT = "tenant-a"
CONVERSATION = "conv-1"


@pytest.fixture(params=[f for _, f in STORE_FACTORIES], ids=[n for n, _ in STORE_FACTORIES])
def store(request: pytest.FixtureRequest) -> ConversationStore:
    factory: StoreFactory = request.param
    return factory()


def _turns(text: str) -> Sequence[Message]:
    return [Message(role="user", content=text), Message(role="assistant", content=f"re: {text}")]


def _replay(text: str) -> Sequence[ReplayItem]:
    return [ReplayItem(role="user", content=text)]


async def _commit(
    store: ConversationStore,
    revision: int,
    usage_tokens: int,
    *,
    text: str = "hello",
    scope: tuple[str, ...] | None = None,
) -> None:
    await store.append(
        TENANT,
        CONVERSATION,
        _turns(text),
        _replay(text),
        revision,
        usage_tokens,
        first_turn_authorization_group_ids=scope if revision == 0 else None,
    )


async def test_ledger_rejects_negative_usage(store: ConversationStore) -> None:
    """Requirement: usage is a count, so a store must refuse to subtract.

    Without this the ledger is not a ledger: admission reads committed totals,
    and a negative append hands back budget that was already spent.
    """
    with pytest.raises(ValueError):
        await _commit(store, 0, -7, scope=())
    assert await store.get(TENANT, CONVERSATION) is None


async def test_rejected_negative_usage_commits_nothing(store: ConversationStore) -> None:
    """Requirement: the rejection is pre-mutation, like every other one."""
    await _commit(store, 0, 100, scope=())
    with pytest.raises(ValueError):
        await _commit(store, 1, -100)
    conversation = await store.get(TENANT, CONVERSATION)
    assert conversation is not None
    assert conversation.revision == 1
    assert conversation.total_tokens == 100


async def test_ledger_is_monotonic_across_commits(store: ConversationStore) -> None:
    """Requirement: the committed total never decreases."""
    totals: list[int] = []
    for revision, usage in enumerate([10, 0, 25, 0, 5]):
        await _commit(store, revision, usage, scope=() if revision == 0 else None)
        conversation = await store.get(TENANT, CONVERSATION)
        assert conversation is not None
        totals.append(conversation.total_tokens)
    assert totals == sorted(totals)
    assert totals[-1] == 40


async def test_turn_and_usage_become_visible_together(store: ConversationStore) -> None:
    """Requirement: atomic visibility — no reader sees the turn without its usage."""
    await _commit(store, 0, 42, scope=())
    conversation = await store.get(TENANT, CONVERSATION)
    assert conversation is not None
    assert conversation.revision == 1
    assert len(conversation.messages) == 2
    assert conversation.total_tokens == 42


async def test_revision_conflict_commits_nothing(store: ConversationStore) -> None:
    """Requirement: a stale revision commits neither the turn nor its usage."""
    await _commit(store, 0, 30, scope=())
    with pytest.raises(ConversationConflictError):
        await _commit(store, 0, 999)
    conversation = await store.get(TENANT, CONVERSATION)
    assert conversation is not None
    assert conversation.revision == 1
    assert conversation.total_tokens == 30


async def test_nonzero_usage_rolls_back_when_the_commit_fails(
    store: ConversationStore,
) -> None:
    """Requirement: rollback is observed with usage that would be visible.

    The existing all-or-nothing coverage injects failure with ``usage_tokens=0``,
    which cannot distinguish "rolled back" from "added zero". This one uses a
    value a leaked partial commit would show.
    """
    await _commit(store, 0, 50, scope=())

    class Exploding(list[ReplayItem]):
        def __iter__(self) -> object:  # type: ignore[override]
            raise RuntimeError("storage backend failed mid-commit")

    with pytest.raises(RuntimeError):
        await store.append(
            TENANT,
            CONVERSATION,
            _turns("second"),
            Exploding(),
            1,
            777,
            first_turn_authorization_group_ids=None,
        )
    conversation = await store.get(TENANT, CONVERSATION)
    assert conversation is not None
    assert conversation.revision == 1
    assert conversation.total_tokens == 50
