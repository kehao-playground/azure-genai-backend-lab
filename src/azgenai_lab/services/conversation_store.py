"""Conversation storage behind a Protocol (Day 7).

The store is the seam where a persistent backend (Azure Cosmos DB, Azure
Database for PostgreSQL) plugs in later; the orchestration layer only sees
the Protocol. The in-memory implementation is deliberately demo-grade:
process-local, gone on restart, invisible to other replicas — which is also
why an unknown ``conversation_id`` is a normal condition, not a bug.

Contract for every implementation (review r01 findings 2, 3, 6):

- ``append`` is all-or-nothing: a turn is committed completely or not at all;
  a partial write is a broken store, not a smaller commit.
- The store does not serialize concurrent turns by itself — the orchestrator
  holds a per-conversation critical section. A persistent adapter must make
  ``append`` atomic and should additionally support conditional writes
  (version/ETag) so multi-replica deployments can reject stale writers.
- Handed-out state never aliases internal state in either direction.
"""

import copy
from collections.abc import Sequence
from typing import Protocol

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import Conversation, ReplayItem


class ConversationStore(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def append(
        self,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
    ) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}
        self._replay_items: dict[str, list[ReplayItem]] = {}

    async def get(self, conversation_id: str) -> Conversation | None:
        messages = self._messages.get(conversation_id)
        if messages is None:
            return None
        # Copies on the way out: Message is frozen so a shallow list copy is
        # enough for the transcript; replay items are plain dicts and need a
        # deep copy to stay unaliased.
        return Conversation(
            id=conversation_id,
            messages=list(messages),
            replay_items=copy.deepcopy(self._replay_items.get(conversation_id, [])),
        )

    async def append(
        self,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
    ) -> None:
        # Deep-copy on the way in: the caller keeps its objects, the log keeps
        # its own. Both extends run back-to-back with no await between them,
        # so the in-memory commit is all-or-nothing.
        self._messages.setdefault(conversation_id, []).extend(turns)
        self._replay_items.setdefault(conversation_id, []).extend(copy.deepcopy(list(replay_items)))


def build_conversation_store(settings: Settings) -> ConversationStore:
    """Composition point for the storage backend (in-memory only today)."""
    return InMemoryConversationStore()
