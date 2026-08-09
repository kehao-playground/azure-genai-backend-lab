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
may already have incurred billable processing; retrying repeats it.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

from azgenai_lab.core.audit import AuditAttribution, ChatCommitSnapshot, build_audit_attribution
from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ChatStorageCommitError, StorageError, UpstreamServiceError
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import Conversation, ReplayItem
from azgenai_lab.models.principal import Principal
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import (
    ChatResult,
    ChatService,
    ChatStreamEvent,
    StreamDone,
    TextDelta,
    build_chat_service,
)
from azgenai_lab.services.conversation_store import ConversationStore


def turn_commits(status: str, incomplete_reason: str | None) -> bool:
    """Single source of the Day 6/7 keep rule: ``completed`` always commits,
    and ``incomplete`` commits only for ``max_output_tokens`` (a client-kept
    partial reply) — ``content_filter``/``other`` are replies the client must
    discard, so the log must not keep them either. ``complete()``,
    ``_commit_on_done()`` and the audit success-event builders' ``committed``
    argument all call this rather than each re-deriving it.
    """
    return status == "completed" or incomplete_reason == "max_output_tokens"


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


class ConversationChatService:
    def __init__(
        self,
        chat_service: ChatService,
        store: ConversationStore,
        token_budget: int | None = None,
        locks: KeyedLock[tuple[str, str]] | None = None,
        audit_attribution: AuditAttribution | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._store = store
        self._token_budget = token_budget
        # Public: the finalizer in api/chat.py reads this to attribute a
        # success event to the prompt/deployment this service was actually
        # built with (the same PromptTemplate instance the adapter holds).
        self.audit_attribution = audit_attribution
        # One turn at a time per (tenant_id, conversation_id). Acquire and
        # release sit in different frames on the streaming path — the release
        # happens when the event iterator finishes, not when open_stream()
        # returns — so the lock is taken explicitly here rather than with a
        # `hold()` block. Keying on the tenant too (not just the
        # conversation id) means two tenants can never serialize behind each
        # other's turns, even if a conversation_id collided across tenants.
        self._locks = locks if locks is not None else KeyedLock()

    async def _load(
        self, principal: Principal, provided_id: str | None, resolved_id: str
    ) -> Conversation:
        if provided_id is None:
            # The id is issued by the caller path, but the conversation exists
            # only once its first turn commits — a failed first call leaves
            # nothing behind. The draft carries the creator's scope so the
            # first commit publishes it atomically with the turn.
            return Conversation(id=resolved_id, authorization_group_ids=principal.group_ids)
        try:
            conversation = await self._store.get(principal.tenant_id, provided_id)
        except Exception as exc:
            raise StorageError(str(exc)) from exc
        if conversation is None:
            # Indistinguishable from a never-issued id: a cross-tenant read
            # of another tenant's conversation_id lands here too, since the
            # store's key space already excludes it (Day 15).
            raise ConversationNotFoundError(provided_id)
        if conversation.authorization_group_ids != principal.group_ids:
            # Scope mismatch is indistinguishable from not-found: the group
            # set is part of session identity (Day 18), and a distinct error
            # would leak the conversation's existence.
            raise ConversationNotFoundError(provided_id)
        return conversation

    def _check_budget(self, conversation: Conversation) -> None:
        # Post-paid ledger, pre-paid gate: the check reads what committed turns
        # actually reported, so it can only fire *between* turns — a single turn
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
        tenant_id: str,
        conversation_id: str,
        turns: list[Message],
        replay_items: list[ReplayItem],
        expected_revision: int,
        usage_tokens: int,
        first_turn_scope: tuple[str, ...] | None,
        audit_snapshot: ChatCommitSnapshot,
    ) -> None:
        try:
            await self._store.append(
                tenant_id,
                conversation_id,
                turns,
                replay_items,
                expected_revision,
                usage_tokens,
                first_turn_authorization_group_ids=first_turn_scope,
            )
        except Exception as exc:
            # Includes ConversationConflictError: under the per-process lock a
            # stale revision can only mean a cross-replica race this demo
            # does not support — a broken deployment, not a client case.
            # Typed (not bare StorageError) so a finalizer can tell this path
            # apart from _load's: the provider already ran by the time this
            # is raised (Day 22) — audit_snapshot is required, not optional,
            # because every call site here has terminal data in hand.
            raise ChatStorageCommitError(str(exc), audit_snapshot=audit_snapshot) from exc

    async def complete(
        self, message: str, conversation_id: str | None, *, principal: Principal
    ) -> tuple[str, ChatResult]:
        resolved_id = conversation_id or str(uuid4())
        lock_key = (principal.tenant_id, resolved_id)
        await self._locks.acquire(lock_key)
        try:
            conversation = await self._load(principal, conversation_id, resolved_id)
            self._check_budget(conversation)
            user_item = _user_item(message)
            result = await self._chat_service.complete([*conversation.replay_items, user_item])
            if result.status == "completed" and not result.message:
                # A 200 with a freshly issued id that resolves to 404 next
                # turn would break the contract; an empty reply is an
                # upstream failure, not a turn.
                raise UpstreamServiceError("upstream returned an empty reply")
            # Same keep/discard rule as the stream terminal (Day 6): completed
            # and incomplete/max_output_tokens commit; content_filter and
            # other are replies the client must discard, so the log must not
            # keep them either — the turn leaves no trace and a first-turn id
            # never comes into existence.
            keeps = turn_commits(result.status, result.incomplete_reason)
            if keeps:
                turns = [Message(role="user", content=message)]
                if result.message:
                    turns.append(Message(role="assistant", content=result.message))
                await self._commit(
                    principal.tenant_id,
                    resolved_id,
                    turns,
                    [user_item, *result.replay_items],
                    expected_revision=conversation.revision,
                    usage_tokens=result.usage.total_tokens if result.usage else 0,
                    first_turn_scope=(
                        conversation.authorization_group_ids if conversation.revision == 0 else None
                    ),
                    audit_snapshot=ChatCommitSnapshot(
                        result.model_version, result.usage, result.status, result.incomplete_reason
                    ),
                )
        finally:
            self._locks.release(lock_key)
        return resolved_id, result

    async def open_stream(
        self, message: str, conversation_id: str | None, *, principal: Principal
    ) -> tuple[str, AsyncIterator[ChatStreamEvent]]:
        resolved_id = conversation_id or str(uuid4())
        lock_key = (principal.tenant_id, resolved_id)
        await self._locks.acquire(lock_key)
        try:
            conversation = await self._load(principal, conversation_id, resolved_id)
            # Budget rejection is pre-stream by design: it raises before any
            # byte reaches the client, so it travels as an HTTP envelope.
            self._check_budget(conversation)
            user_item = _user_item(message)
            # Eager await preserved: pre-stream failures raise here, before
            # any byte reaches the client — the Day 6 two-phase boundary
            # passes through this layer intact.
            events = await self._chat_service.open_stream([*conversation.replay_items, user_item])
        except BaseException:
            self._locks.release(lock_key)
            raise
        first_turn_scope = (
            conversation.authorization_group_ids if conversation.revision == 0 else None
        )
        return resolved_id, self._commit_on_done(
            principal.tenant_id,
            resolved_id,
            message,
            user_item,
            events,
            conversation.revision,
            first_turn_scope,
        )

    async def _commit_on_done(
        self,
        tenant_id: str,
        conversation_id: str,
        message: str,
        user_item: ReplayItem,
        events: AsyncIterator[ChatStreamEvent],
        expected_revision: int,
        first_turn_scope: tuple[str, ...] | None,
    ) -> AsyncIterator[ChatStreamEvent]:
        parts: list[str] = []
        try:
            async for event in events:
                if isinstance(event, StreamDone):
                    keeps_text = turn_commits(event.status, event.incomplete_reason)
                    if keeps_text:
                        text = "".join(parts)
                        turns = [Message(role="user", content=message)]
                        if text:
                            turns.append(Message(role="assistant", content=text))
                        # Commit before the terminal is delivered: when the
                        # client sees message.done, the history it implies
                        # already exists.
                        await self._commit(
                            tenant_id,
                            conversation_id,
                            turns,
                            [user_item, *event.replay_items],
                            expected_revision=expected_revision,
                            usage_tokens=event.usage.total_tokens if event.usage else 0,
                            first_turn_scope=first_turn_scope,
                            audit_snapshot=ChatCommitSnapshot(
                                event.model_version, event.usage,
                                event.status, event.incomplete_reason,
                            ),
                        )
                    yield event
                    return
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                yield event
        finally:
            self._locks.release((tenant_id, conversation_id))

    async def aclose(self) -> None:
        """Close the composed chat service.

        ``ConversationStore`` (in-memory today) holds no external resource —
        it is a process-local dict — so there is nothing to close there;
        a future persistent adapter (Cosmos DB, Postgres) would need to grow
        its own ``aclose()`` and be composed here too.
        """
        await self._chat_service.aclose()


def build_conversation_service(
    settings: Settings,
    *,
    store: ConversationStore,
    locks: KeyedLock[tuple[str, str]],
) -> ConversationChatService:
    """Composition point: the chat adapter wrapped with app-owned state.

    Store and locks are injected, never built here: /chat and /agent share
    one store (or cross-endpoint history is silently invisible) and one lock
    registry (or concurrent turns on one conversation burn a full inference
    before dying as 500 storage_error). Correctness, not convenience.

    The prompt is loaded exactly once here and handed to both the adapter
    (build_chat_service) and the audit attribution (build_audit_attribution) —
    the same PromptTemplate instance, not two loads of the same file, so the
    attribution reports what the adapter actually holds (Day 22).
    """
    prompt = load_prompt("default_chat")
    return ConversationChatService(
        build_chat_service(settings, prompt=prompt),
        store,
        token_budget=settings.conversation_token_budget,
        locks=locks,
        audit_attribution=build_audit_attribution(settings, prompt),
    )
