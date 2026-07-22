"""Conversation orchestration: history in, one turn out (Day 7).

Owning conversation state (``store=False``) means this layer decides what
enters the history. The rule is turn-commit: the user input and the reply
context are appended together, only after the LLM call produced a reply the
client keeps — a failed turn leaves no trace, so retrying it cannot
duplicate or corrupt history. A turn is committed at two fidelities: the
visible transcript (``Message``) and the provider replay items that the next
request must resend verbatim, including encrypted reasoning items.

For streams the turn commits when the terminal event arrives and the Day 6
contract says the client keeps the text: ``completed`` and ``incomplete``
with reason ``max_output_tokens`` commit; ``content_filter`` / ``other``
(the client must discard the text) and mid-stream errors do not. A client
disconnect **before the upstream terminal is consumed** aborts the turn
uncommitted; once the terminal is consumed the commit happens whether or not
``message.done`` provably reached the client — no transport can prove
delivery across a dying socket. The one-way invariant is: if the client
received ``message.done``, the history it implies already exists.

Turns on the same conversation are serialized with a per-conversation lock:
read → inference → commit is one critical section, so parallel requests
cannot both build on the same stale snapshot and record a causally false
history. The commit is additionally conditional — it presents the revision
read at the start of the turn — which is the contract a multi-replica
persistent adapter enforces natively; in-process, a conflict would mean the
serialization invariant broke, so it maps to :class:`StorageError`. Lock
entries are reference-counted and removed once the last waiter is done, so
probing unknown ids cannot grow the registry.

Storage failures surface as :class:`StorageError` (HTTP 500 envelope before
a response is out, SSE ``error`` terminal after a 200). By then inference
has already been billed; retrying repeats it.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import StorageError, UpstreamServiceError
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import Conversation, ReplayItem
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


class TokenBudgetExceededError(Exception):
    """The conversation's lifetime token budget is exhausted (Day 9).

    Raised *before* inference: the whole point of the guardrail is that an
    exhausted conversation costs nothing further upstream. This is a policy
    rejection owned by this service, not an upstream failure — the client's
    remedy is to start a new conversation, not to retry this one.
    """

    def __init__(self, conversation_id: str, spent: int, budget: int) -> None:
        super().__init__(f"conversation {conversation_id}: spent {spent} of {budget} tokens")
        self.conversation_id = conversation_id
        self.spent = spent
        self.budget = budget


def _user_item(message: str) -> ReplayItem:
    return {"role": "user", "content": message}


class _LockEntry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


class ConversationChatService:
    def __init__(
        self,
        chat_service: ChatService,
        store: ConversationStore,
        token_budget: int | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._store = store
        self._token_budget = token_budget
        # Reference-counted per-conversation locks: an entry exists only while
        # a request holds or awaits it, so probing unknown ids cannot grow the
        # registry unboundedly (review r04 finding 3).
        self._locks: dict[str, _LockEntry] = {}

    async def _acquire(self, conversation_id: str) -> None:
        entry = self._locks.get(conversation_id)
        if entry is None:
            entry = _LockEntry()
            self._locks[conversation_id] = entry
        entry.refs += 1
        try:
            await entry.lock.acquire()
        except BaseException:
            # A waiter cancelled in the queue never reaches the caller's
            # _release(): drop its reference here, without releasing a lock
            # it never acquired (review r06 finding 1).
            entry.refs -= 1
            if entry.refs == 0:
                del self._locks[conversation_id]
            raise

    def _release(self, conversation_id: str) -> None:
        entry = self._locks[conversation_id]
        entry.lock.release()
        entry.refs -= 1
        if entry.refs == 0:
            del self._locks[conversation_id]

    async def _load(self, provided_id: str | None, resolved_id: str) -> Conversation:
        if provided_id is None:
            # The id is issued by the caller path, but the conversation exists
            # only once its first turn commits — a failed first call leaves
            # nothing behind.
            return Conversation(id=resolved_id)
        try:
            conversation = await self._store.get(provided_id)
        except Exception as exc:
            raise StorageError(str(exc)) from exc
        if conversation is None:
            raise ConversationNotFoundError(provided_id)
        return conversation

    def _check_budget(self, conversation: Conversation) -> None:
        # Post-paid ledger, pre-paid gate: the check reads what committed turns
        # actually billed, so it can only fire *between* turns — a single turn
        # can still overshoot the line by up to one call's worth of tokens
        # (bounded by max_output_tokens plus the history the turn replays).
        if self._token_budget is None:
            return
        if conversation.total_tokens >= self._token_budget:
            raise TokenBudgetExceededError(
                conversation.id, conversation.total_tokens, self._token_budget
            )

    async def _commit(
        self,
        conversation_id: str,
        turns: list[Message],
        replay_items: list[ReplayItem],
        expected_revision: int,
        usage_tokens: int,
    ) -> None:
        try:
            await self._store.append(
                conversation_id, turns, replay_items, expected_revision, usage_tokens
            )
        except Exception as exc:
            # Includes ConversationConflictError: under the per-process lock a
            # stale revision can only mean a cross-replica race this demo
            # does not support — a broken deployment, not a client case.
            raise StorageError(str(exc)) from exc

    async def complete(self, message: str, conversation_id: str | None) -> tuple[str, ChatResult]:
        resolved_id = conversation_id or str(uuid4())
        await self._acquire(resolved_id)
        try:
            conversation = await self._load(conversation_id, resolved_id)
            self._check_budget(conversation)
            user_item = _user_item(message)
            result = await self._chat_service.complete([*conversation.replay_items, user_item])
            if result.status == "completed" and not result.message:
                # A 200 with a freshly issued id that resolves to 404 next
                # turn would break the contract; an empty reply is an
                # upstream failure, not a turn (review r01 finding 4).
                raise UpstreamServiceError("upstream returned an empty reply")
            # Same keep/discard rule as the stream terminal (Day 6): completed
            # and incomplete/max_output_tokens commit; content_filter and
            # other are replies the client must discard, so the log must not
            # keep them either — the turn leaves no trace and a first-turn id
            # never comes into existence.
            keeps = result.status == "completed" or result.incomplete_reason == "max_output_tokens"
            if keeps:
                turns = [Message(role="user", content=message)]
                if result.message:
                    turns.append(Message(role="assistant", content=result.message))
                await self._commit(
                    resolved_id,
                    turns,
                    [user_item, *result.replay_items],
                    expected_revision=conversation.revision,
                    usage_tokens=result.usage.total_tokens if result.usage else 0,
                )
        finally:
            self._release(resolved_id)
        return resolved_id, result

    async def open_stream(
        self, message: str, conversation_id: str | None
    ) -> tuple[str, AsyncIterator[ChatStreamEvent]]:
        resolved_id = conversation_id or str(uuid4())
        await self._acquire(resolved_id)
        try:
            conversation = await self._load(conversation_id, resolved_id)
            # Budget rejection is pre-stream by design: it raises before any
            # byte reaches the client, so it travels as an HTTP envelope.
            self._check_budget(conversation)
            user_item = _user_item(message)
            # Eager await preserved: pre-stream failures raise here, before
            # any byte reaches the client — the Day 6 two-phase boundary
            # passes through this layer intact.
            events = await self._chat_service.open_stream([*conversation.replay_items, user_item])
        except BaseException:
            self._release(resolved_id)
            raise
        return resolved_id, self._commit_on_done(
            resolved_id, message, user_item, events, conversation.revision
        )

    async def _commit_on_done(
        self,
        conversation_id: str,
        message: str,
        user_item: ReplayItem,
        events: AsyncIterator[ChatStreamEvent],
        expected_revision: int,
    ) -> AsyncIterator[ChatStreamEvent]:
        parts: list[str] = []
        try:
            async for event in events:
                if isinstance(event, StreamDone):
                    keeps_text = event.status == "completed" or (
                        event.incomplete_reason == "max_output_tokens"
                    )
                    if keeps_text:
                        text = "".join(parts)
                        turns = [Message(role="user", content=message)]
                        if text:
                            turns.append(Message(role="assistant", content=text))
                        # Commit before the terminal is delivered: when the
                        # client sees message.done, the history it implies
                        # already exists.
                        await self._commit(
                            conversation_id,
                            turns,
                            [user_item, *event.replay_items],
                            expected_revision=expected_revision,
                            usage_tokens=event.usage.total_tokens if event.usage else 0,
                        )
                    yield event
                    return
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                yield event
        finally:
            self._release(conversation_id)


def build_conversation_service(settings: Settings) -> ConversationChatService:
    """Composition point: the chat adapter wrapped with app-owned state."""
    return ConversationChatService(
        build_chat_service(settings),
        build_conversation_store(settings),
        token_budget=settings.conversation_token_budget,
    )
