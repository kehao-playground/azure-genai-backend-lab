"""Conversation storage behind a Protocol (Day 7).

The store is the seam where a persistent backend (Azure Cosmos DB, Azure
Database for PostgreSQL) plugs in later; the orchestration layer only sees
the Protocol. The in-memory implementation is deliberately demo-grade:
process-local, gone on restart, invisible to other replicas — which is also
why an unknown ``conversation_id`` is a normal condition, not a bug.

Contract for every implementation (review r01 findings 2, 3, 6; r04 1-2):

- ``append`` is **conditional**: the caller presents the ``revision`` it read
  (the number of committed turns); on mismatch the store raises
  :class:`ConversationConflictError` and commits nothing. This is the
  version/ETag token a multi-replica persistent adapter enforces natively;
  under the orchestrator's per-process lock a conflict can only mean a
  cross-replica race this demo does not support.
- ``append`` is all-or-nothing: everything that can fail (validation,
  copying) happens before the first mutation; a turn is committed completely
  or not at all.
- Handed-out state never aliases internal state in either direction.
"""

import copy
from collections.abc import Sequence
from typing import Protocol

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import Conversation, ReplayItem


class ConversationConflictError(Exception):
    """The presented revision is stale: another writer committed first."""

    def __init__(self, conversation_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"conversation {conversation_id}: expected revision {expected}, found {actual}"
        )
        self.conversation_id = conversation_id
        self.expected = expected
        self.actual = actual


class ConversationStore(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def append(
        self,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
        expected_revision: int,
    ) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}
        self._replay_items: dict[str, list[ReplayItem]] = {}
        self._revisions: dict[str, int] = {}

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
            revision=self._revisions.get(conversation_id, 0),
        )

    async def append(
        self,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
        expected_revision: int,
    ) -> None:
        current = self._revisions.get(conversation_id, 0)
        if expected_revision != current:
            raise ConversationConflictError(conversation_id, expected_revision, current)
        # Prepare-then-publish: everything that can fail (the deep copy)
        # happens before the first mutation, so a failed append leaves the
        # log untouched instead of half a two-representation turn.
        prepared_turns = list(turns)
        prepared_replay = copy.deepcopy(list(replay_items))
        self._messages.setdefault(conversation_id, []).extend(prepared_turns)
        self._replay_items.setdefault(conversation_id, []).extend(prepared_replay)
        self._revisions[conversation_id] = current + 1


def build_conversation_store(settings: Settings) -> ConversationStore:
    """Composition point for the storage backend (in-memory only today)."""
    return InMemoryConversationStore()
