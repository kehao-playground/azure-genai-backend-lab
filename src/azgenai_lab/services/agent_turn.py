"""Conversation-integrated agent turns (Day 18).

Mirrors ConversationChatService's rhythm — resolve id → lock → load → scope →
budget → run → commit → release — with the Day 18 decisions applied: one run
is one turn (aggregate usage commits atomically, a mid-run failure leaves no
trace), the budget gate is a single pre-run check (an admitted run may
overshoot the threshold by up to the whole run's provider-reported usage),
and the framework's fallback vocabulary was already stripped by the adapter.
"""

import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from azgenai_lab.core.audit import (
    AgentAuditTerminalSnapshot,
    AuditAttribution,
    build_audit_attribution,
)
from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import (
    AgentRunUpstreamError,
    AgentStorageCommitError,
    StorageError,
)
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.models.chat import Message, TokenUsage
from azgenai_lab.models.conversation import Conversation, ReplayItem
from azgenai_lab.models.principal import Principal
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.agent_framework import (
    AgentHistoryTurn,
    AgentRunError,
    AgentRunResult,
    AgentService,
    AgentToolCall,
    build_agent_service,
    validate_task,
)
from azgenai_lab.services.agent_tools import build_agent_tool_deps
from azgenai_lab.services.conversation import (
    ConversationNotFoundError,
    TokenBudgetExceededError,
)
from azgenai_lab.services.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

IncompleteReason = Literal["tool_call_limit", "iteration_limit"]

# Additive extension of the Day 6 incomplete vocabulary: two limits, two
# names — the client rule for both is "the answer is keepable (and may be
# empty)". Merging them would erase which guardrail fired.
_STOP_TO_REASON: dict[str, IncompleteReason] = {
    "function_call_limit": "tool_call_limit",
    "iteration_limit": "iteration_limit",
}


@dataclass(frozen=True)
class AgentTurnResult:
    """Router-facing result: field transport only, no semantics left over."""

    answer: str
    status: Literal["completed", "incomplete"]
    incomplete_reason: IncompleteReason | None
    model_call_count: int
    tool_calls: tuple[AgentToolCall, ...]
    usage: TokenUsage | None
    # Carried straight through from AgentRunResult (Day 22) so the /agent
    # finalizer's success event describes the same trace as the response,
    # not a re-derivation of it.
    audit_snapshot: AgentAuditTerminalSnapshot


def project_history(messages: Sequence[Message]) -> tuple[AgentHistoryTurn, ...]:
    """App-owned projection of the client-visible transcript. Never parses
    the opaque provider replay items: those are /chat's wire fidelity, not
    conversation content. Non-conversation roles (system/tool) are skipped."""
    turns: list[AgentHistoryTurn] = []
    for message in messages:
        if message.role in ("user", "assistant"):
            # The membership test guarantees the literal; Message.role is a
            # pattern-validated str, which mypy cannot narrow on its own.
            role = cast(Literal["user", "assistant"], message.role)
            turns.append(AgentHistoryTurn(role=role, text=message.content))
    return tuple(turns)


class AgentTurnService:
    def __init__(
        self,
        agent_service: AgentService,
        store: ConversationStore,
        token_budget: int | None = None,
        locks: KeyedLock[tuple[str, str]] | None = None,
        audit_attribution: AuditAttribution | None = None,
    ) -> None:
        self._agent_service = agent_service
        self._store = store
        self._token_budget = token_budget
        # Shared with ConversationChatService at composition: one turn at a
        # time per (tenant_id, conversation_id) ACROSS endpoints, or two
        # concurrent turns both run a full (billed) inference and the loser
        # dies at commit as 500 storage_error.
        self._locks: KeyedLock[tuple[str, str]] = (
            locks if locks is not None else KeyedLock()
        )
        # Public, same as ConversationChatService's: the /agent finalizer
        # reads this directly (Day 22).
        self.audit_attribution = audit_attribution
        self._closed = False

    @asynccontextmanager
    async def _stage(self, name: str) -> AsyncIterator[None]:
        start = time.perf_counter()
        try:
            yield
        except BaseException as exc:
            logger.info(
                "agent_turn_stage stage=%s outcome=error duration_ms=%.1f exception=%s",
                name,
                (time.perf_counter() - start) * 1000,
                type(exc).__name__,
            )
            raise
        logger.info(
            "agent_turn_stage stage=%s outcome=success duration_ms=%.1f",
            name,
            (time.perf_counter() - start) * 1000,
        )

    async def run_turn(
        self, task: str, conversation_id: str | None, *, principal: Principal
    ) -> tuple[str, AgentTurnResult]:
        validate_task(task)
        resolved_id = conversation_id or str(uuid4())
        lock_key = (principal.tenant_id, resolved_id)
        await self._locks.acquire(lock_key)
        try:
            async with self._stage("load"):
                conversation = await self._load(principal, conversation_id, resolved_id)
                self._check_budget(conversation)
            history = project_history(conversation.messages)
            async with self._stage("run"):
                try:
                    result = await self._agent_service.run(
                        task, history, principal=principal
                    )
                except AgentRunError as exc:
                    # One run = one turn: a mid-run failure has been billed
                    # upstream but carries no usage (the framework attaches its
                    # aggregate only to a returned response) — nothing enters the
                    # ledger, the gap is disclosed, Cost Management stays the
                    # authority (Day 9 / Day 17). The exception's own degraded
                    # snapshot (built by the adapter, at the point of failure)
                    # rides along so the /agent finalizer's error event still
                    # reports whatever trace exists.
                    raise AgentRunUpstreamError(
                        str(exc), audit_snapshot=exc.audit_snapshot
                    ) from exc
                if result.stop_reason == "natural" and not result.answer:
                    # Same contract as /chat's completed-empty reply: an upstream
                    # failure, not a turn — a 200 with a freshly issued id that
                    # 404s next turn would break the contract. Only the two limit
                    # stops may return an empty (keepable) answer. The run itself
                    # succeeded, so result.audit_snapshot is not degraded — real
                    # executions/tools, model_calls, stop_reason="natural", usage
                    # are all in scope here and must not be discarded in favor of
                    # the generic fallback's honest-but-needlessly-empty None's
                    # (review finding, Day 22 fix round 1).
                    raise AgentRunUpstreamError(
                        "agent returned an empty final answer",
                        audit_snapshot=result.audit_snapshot,
                    )
            async with self._stage("commit"):
                await self._commit(
                    principal.tenant_id, resolved_id, conversation, task, result
                )
            status: Literal["completed", "incomplete"]
            if result.stop_reason == "natural":
                status, reason = "completed", None
            else:
                status, reason = "incomplete", _STOP_TO_REASON[result.stop_reason]
            return resolved_id, AgentTurnResult(
                answer=result.answer,
                status=status,
                incomplete_reason=reason,
                model_call_count=result.model_call_count,
                tool_calls=result.tool_calls,
                usage=result.usage,
                audit_snapshot=result.audit_snapshot,
            )
        finally:
            # Every exit — 404, budget, run failure, commit failure,
            # cancellation — releases; a leaked entry blocks the
            # conversation forever.
            self._locks.release(lock_key)

    async def _load(
        self, principal: Principal, provided_id: str | None, resolved_id: str
    ) -> Conversation:
        if provided_id is None:
            return Conversation(
                id=resolved_id, authorization_group_ids=principal.group_ids
            )
        try:
            conversation = await self._store.get(principal.tenant_id, provided_id)
        except Exception as exc:
            raise StorageError(str(exc)) from exc
        if conversation is None:
            raise ConversationNotFoundError(provided_id)
        if conversation.authorization_group_ids != principal.group_ids:
            # Same shape as unknown/cross-tenant: scope is session identity.
            raise ConversationNotFoundError(provided_id)
        return conversation

    def _check_budget(self, conversation: Conversation) -> None:
        # Single pre-run check (Day 18 decision #2): reads committed turns
        # only — an admitted run can overshoot by its entire usage; the
        # complete ceiling remains Day 17's 6 × context window.
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
        conversation: Conversation,
        task: str,
        result: AgentRunResult,
    ) -> None:
        turns = [Message(role="user", content=task)]
        replay: list[ReplayItem] = [{"role": "user", "content": task}]
        if result.answer:
            turns.append(Message(role="assistant", content=result.answer))
            # App-owned plain text: the framework owns the run's inner
            # transcript (tool calls, reasoning items); the conversation
            # replays only the final text — a disclosed projection, not the
            # encrypted-reasoning fidelity the chat adapter can capture.
            replay.append({"role": "assistant", "content": result.answer})
        first_turn_scope = (
            conversation.authorization_group_ids if conversation.revision == 0 else None
        )
        try:
            await self._store.append(
                tenant_id,
                conversation_id,
                turns,
                replay,
                conversation.revision,
                result.usage.total_tokens if result.usage else 0,
                first_turn_authorization_group_ids=first_turn_scope,
            )
        except Exception as exc:
            # The run already succeeded and returned a terminal snapshot
            # (result.audit_snapshot) — carry it through so a commit failure's
            # audit event still reports what the run actually did, the same
            # discipline as ChatStorageCommitError (Day 22).
            raise AgentStorageCommitError(
                str(exc), audit_snapshot=result.audit_snapshot
            ) from exc

    async def aclose(self) -> None:
        """Idempotent delegate: the adapter owns the model client and the
        retriever; the wrapper guarantees the adapter is asked to close at
        most once, even if a first close raised (house style: the flag flips
        before the await, exactly like the real adapter's own guard)."""
        if self._closed:
            return
        self._closed = True
        await self._agent_service.aclose()


def build_agent_turn_service(
    settings: Settings,
    *,
    store: ConversationStore,
    locks: KeyedLock[tuple[str, str]],
) -> AgentTurnService:
    """Composition point (see build_conversation_service for why store and
    locks are injected, never built here).

    The prompt is loaded exactly once here and handed to both the adapter
    (build_agent_service) and the audit attribution (build_audit_attribution)
    — same PromptTemplate instance, not two loads of the same file, mirroring
    build_conversation_service's Day 22 discipline for /chat.
    """
    prompt = load_prompt("ops_agent")
    deps = build_agent_tool_deps(
        settings,
        conversation_store=store,
        token_budget=settings.conversation_token_budget,
    )
    return AgentTurnService(
        build_agent_service(settings, deps, prompt=prompt),
        store,
        token_budget=settings.conversation_token_budget,
        locks=locks,
        audit_attribution=build_audit_attribution(settings, prompt),
    )
