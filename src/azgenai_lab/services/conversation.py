"""Conversation orchestration: history in, one turn out (Day 7).

Owning conversation state (``store=False``) means this layer decides what
enters the history. The rule is turn-commit: the user message and the
assistant reply are appended together, only after the LLM call produced a
usable reply — a failed turn leaves no trace, so retrying it cannot
duplicate or corrupt history.

For streams the turn commits when the terminal event arrives and the Day 6
contract says the client keeps the text: ``completed`` and ``incomplete``
with reason ``max_output_tokens`` commit; ``content_filter`` / ``other``
(the client must discard the text) and mid-stream errors do not. A client
disconnect aborts the turn uncommitted.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import Message
from azgenai_lab.services.azure_openai import (
    ChatResult,
    ChatService,
    ChatStreamEvent,
    StreamDone,
    TextDelta,
    build_chat_service,
)
from azgenai_lab.services.conversation_store import ConversationStore, build_conversation_store


class ConversationNotFoundError(Exception):
    """The client referenced a conversation this service does not hold."""

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"unknown conversation_id: {conversation_id}")
        self.conversation_id = conversation_id


class ConversationChatService:
    def __init__(self, chat_service: ChatService, store: ConversationStore) -> None:
        self._chat_service = chat_service
        self._store = store

    async def _resolve_history(self, conversation_id: str | None) -> tuple[str, list[Message]]:
        if conversation_id is None:
            # The id is issued here, but the conversation exists only once its
            # first turn commits — a failed first call leaves nothing behind.
            return str(uuid4()), []
        conversation = await self._store.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation_id, conversation.messages

    async def complete(self, message: str, conversation_id: str | None) -> tuple[str, ChatResult]:
        resolved_id, history = await self._resolve_history(conversation_id)
        user_message = Message(role="user", content=message)
        result = await self._chat_service.complete([*history, user_message])
        # An empty reply is nothing worth keeping (and Message forbids empty
        # content); return it to the client but don't commit the turn.
        if result.message:
            await self._store.append(
                resolved_id,
                [user_message, Message(role="assistant", content=result.message)],
            )
        return resolved_id, result

    async def open_stream(
        self, message: str, conversation_id: str | None
    ) -> tuple[str, AsyncIterator[ChatStreamEvent]]:
        resolved_id, history = await self._resolve_history(conversation_id)
        user_message = Message(role="user", content=message)
        # Eager await preserved: pre-stream failures raise here, before any
        # byte reaches the client — the Day 6 two-phase boundary passes
        # through this layer intact.
        events = await self._chat_service.open_stream([*history, user_message])
        return resolved_id, self._commit_on_done(resolved_id, user_message, events)

    async def _commit_on_done(
        self,
        conversation_id: str,
        user_message: Message,
        events: AsyncIterator[ChatStreamEvent],
    ) -> AsyncIterator[ChatStreamEvent]:
        parts: list[str] = []
        async for event in events:
            if isinstance(event, StreamDone):
                text = "".join(parts)
                keeps_text = event.status == "completed" or (
                    event.incomplete_reason == "max_output_tokens"
                )
                if text and keeps_text:
                    # Commit before the terminal is delivered: when the client
                    # sees message.done, the history it implies already exists.
                    await self._store.append(
                        conversation_id,
                        [user_message, Message(role="assistant", content=text)],
                    )
                yield event
                return
            if isinstance(event, TextDelta):
                parts.append(event.text)
            yield event


def build_conversation_service(settings: Settings) -> ConversationChatService:
    """Composition point: the chat adapter wrapped with app-owned state."""
    return ConversationChatService(build_chat_service(settings), build_conversation_store(settings))
