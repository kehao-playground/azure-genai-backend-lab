"""Conversation storage behind a Protocol (Day 7).

The store is the seam where a persistent backend (Azure Cosmos DB, Azure
Database for PostgreSQL) plugs in later; the orchestration layer only sees
the Protocol. The in-memory implementation is deliberately demo-grade:
process-local, gone on restart, invisible to other replicas — which is also
why an unknown ``conversation_id`` is a normal condition, not a bug.
"""

from collections.abc import Sequence
from typing import Protocol

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import Conversation


class ConversationStore(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None: ...

    async def append(self, conversation_id: str, turns: Sequence[Message]) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}

    async def get(self, conversation_id: str) -> Conversation | None:
        messages = self._messages.get(conversation_id)
        if messages is None:
            return None
        # Hand out a copy: callers must not be able to mutate stored history.
        return Conversation(id=conversation_id, messages=list(messages))

    async def append(self, conversation_id: str, turns: Sequence[Message]) -> None:
        self._messages.setdefault(conversation_id, []).extend(turns)


def build_conversation_store(settings: Settings) -> ConversationStore:
    """Composition point for the storage backend (in-memory only today)."""
    return InMemoryConversationStore()
