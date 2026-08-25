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

**What this harness can and cannot reach** (Day 30 review r04): being
implementation-blind is exactly what bounds it. It observes the store through
``get``, so it proves post-return coherence, pre-mutation rejection, and — via
the concurrent reader below — that a commit is not published in two visible
steps. It cannot reach inside a backend to fail a write half way through: no
Protocol-only test can inject a fault into someone else's transaction. A
persistent adapter therefore satisfies this suite **and** owes its own
backend-specific transactional fault injection; passing here is necessary, not
sufficient. ``TestHarnessRejectsKnownBadStores`` keeps that boundary honest by
proving what the suite does catch.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence

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


async def _observations_during(
    store: ConversationStore, action: Callable[[], Awaitable[None]]
) -> list[tuple[int, int]]:
    """Poll ``get`` while ``action`` runs; return every (revision, total) seen.

    A store that publishes the turn and its usage in two awaits gives the reader
    a window in between. This is how the harness sees that window without
    knowing anything about the backend.
    """
    seen: list[tuple[int, int]] = []
    stop = False

    async def reader() -> None:
        while not stop:
            conversation = await store.get(TENANT, CONVERSATION)
            if conversation is not None:
                seen.append((conversation.revision, conversation.total_tokens))
            await asyncio.sleep(0)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    try:
        await action()
    finally:
        stop = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return seen


async def test_no_reader_ever_sees_the_turn_without_its_usage(
    store: ConversationStore,
) -> None:
    """Requirement: the commit is one visible step, not two.

    Post-return coherence is not enough. If a store publishes turn and revision
    first and usage second, a concurrent reader sees a committed turn whose cost
    is missing — and admission reads committed totals, so that reader would let
    through a turn the budget had already been spent on.
    """
    seen = await _observations_during(store, lambda: _commit(store, 0, 42, scope=()))
    leaked = [obs for obs in seen if obs[0] >= 1 and obs[1] != 42]
    assert not leaked, f"turn visible without its usage: {leaked}"


class TestHarnessRejectsKnownBadStores:
    """Proof that the requirements above are load-bearing.

    A suite that never rejects anything is decoration. These build stores that
    satisfy the Protocol and every signature, and assert the requirements fail —
    so a future adapter cannot pass by accident, and so the boundary between
    "this harness caught it" and "your backend must test this itself" stays
    visible.
    """

    async def test_a_store_publishing_usage_after_the_turn_is_caught(self) -> None:
        """The r04 adversarial adapter: two-step publish, Protocol-conforming."""

        class PublishesUsageLate(InMemoryConversationStore):
            async def append(  # type: ignore[override]
                self,
                tenant_id: str,
                conversation_id: str,
                turns: Sequence[Message],
                replay_items: Sequence[ReplayItem],
                expected_revision: int,
                usage_tokens: int,
                *,
                first_turn_authorization_group_ids: tuple[str, ...] | None,
            ) -> None:
                await super().append(
                    tenant_id,
                    conversation_id,
                    turns,
                    replay_items,
                    expected_revision,
                    0,
                    first_turn_authorization_group_ids=first_turn_authorization_group_ids,
                )
                await asyncio.sleep(0)
                key = (tenant_id, conversation_id)
                self._token_totals[key] = self._token_totals.get(key, 0) + usage_tokens

        leaky = PublishesUsageLate()
        seen = await _observations_during(leaky, lambda: _commit(leaky, 0, 42, scope=()))
        leaked = [obs for obs in seen if obs[0] >= 1 and obs[1] != 42]
        assert leaked, "the concurrent-visibility requirement failed to catch a two-step publish"

        after = await leaky.get(TENANT, CONVERSATION)
        assert after is not None
        assert after.total_tokens == 42, (
            "the leaky store must still be post-return coherent — otherwise this "
            "test would be catching the wrong defect"
        )

    async def test_a_store_accepting_negative_usage_is_caught(self) -> None:
        """The non-negative requirement must reject, not merely describe."""

        class AcceptsNegativeUsage(InMemoryConversationStore):
            async def append(  # type: ignore[override]
                self,
                tenant_id: str,
                conversation_id: str,
                turns: Sequence[Message],
                replay_items: Sequence[ReplayItem],
                expected_revision: int,
                usage_tokens: int,
                *,
                first_turn_authorization_group_ids: tuple[str, ...] | None,
            ) -> None:
                key = (tenant_id, conversation_id)
                current = self._revisions.get(key, 0)
                self._messages.setdefault(key, []).extend(list(turns))
                self._replay_items.setdefault(key, []).extend(list(replay_items))
                if current == 0 and first_turn_authorization_group_ids is not None:
                    self._scopes[key] = first_turn_authorization_group_ids
                self._revisions[key] = current + 1
                self._token_totals[key] = self._token_totals.get(key, 0) + usage_tokens

        permissive = AcceptsNegativeUsage()
        await _commit(permissive, 0, 100, scope=())
        await _commit(permissive, 1, -100)
        conversation = await permissive.get(TENANT, CONVERSATION)
        assert conversation is not None
        assert conversation.total_tokens == 0, (
            "this adapter is supposed to be broken; if it is not, the requirement "
            "test above proves nothing"
        )
