"""Least-privilege read-only tools for the Day 17 ops-assistant agent.

Each tool is a closure over app-owned dependencies. The agent chooses
arguments; it can never choose the tenant, the groups, the store, or the
budget — those are fixed at composition (fail-closed, Day 15 / Day 16 R2).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.conversation_store import ConversationStore
from azgenai_lab.services.retrieval import Retriever

# Cost-control constants (spec §2). Byte caps, not char caps: a BPE token is
# >= 1 UTF-8 byte, so byte caps yield token-denominated upper bounds (Day 14).
MAX_SEARCH_HITS = 3
MAX_SNIPPET_CHARS = 1200
MAX_TOOL_RESULT_BYTES = 4800
MAX_REFUSAL_RESULT_BYTES = 160
NEAR_EXHAUSTED_THRESHOLD = 0.8

AgentToolFn = Callable[..., Awaitable[str]]


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate to a UTF-8 byte budget without splitting a code point."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _envelope_bytes(hits: list[dict[str, str]], candidate: dict[str, str]) -> int:
    """UTF-8 byte length of {"hits": [...hits, candidate]}."""
    envelope = json.dumps({"hits": [*hits, candidate]}, ensure_ascii=False)
    return len(envelope.encode("utf-8"))


def _fit_within_budget(
    hits: list[dict[str, str]], candidate: dict[str, str]
) -> dict[str, str] | None:
    """Byte-clamp `candidate`'s snippet so hits + candidate fits the cap.

    Tries the candidate as-is first. If it overflows, repeatedly shrinks the
    snippet by the measured overflow (accounting for JSON-escaping overhead)
    until it fits. Returns None if it does not fit even with an empty
    snippet — the caller stops there rather than adding a mangled hit.
    """
    snippet = candidate["snippet"]
    budget = len(snippet.encode("utf-8"))
    while True:
        overflow = _envelope_bytes(hits, candidate) - MAX_TOOL_RESULT_BYTES
        if overflow <= 0:
            return candidate
        if budget <= 0:
            return None
        budget = max(0, budget - overflow)
        candidate = {**candidate, "snippet": truncate_utf8(snippet, budget)}


def make_search_docs(retriever: Retriever, principal: Principal) -> AgentToolFn:
    async def search_docs(query: str) -> str:
        """Search this backend's operations documentation.

        Returns JSON: {"hits": [{"source", "heading_path", "snippet"}, ...]}.
        An empty "hits" list means no document matched the query. The result
        is always valid JSON no larger than MAX_TOOL_RESULT_BYTES: hits are
        considered in rank order, a hit's snippet is byte-clamped as needed
        to make that hit fit, and a hit that does not fit even with an empty
        snippet is dropped along with every hit that would have followed it.
        """
        result = await retriever.retrieve(query, principal)
        hits: list[dict[str, str]] = []
        for hit in result.hits[:MAX_SEARCH_HITS]:
            candidate = {
                "source": hit.parent_id,
                "heading_path": hit.heading_path,
                "snippet": hit.content[:MAX_SNIPPET_CHARS],
            }
            fitted = _fit_within_budget(hits, candidate)
            if fitted is None:
                break
            hits.append(fitted)
        return json.dumps({"hits": hits}, ensure_ascii=False)

    return search_docs


def make_get_runtime_config(settings: Settings, demo_token_budget: int) -> AgentToolFn:
    async def get_runtime_config() -> str:
        """Report the guardrail configuration this deployment actually applies.

        Returns JSON with output caps, timeouts, the conversation token
        budget, and the agent loop limits. These are the applied values,
        not documentation.
        """
        # Explicit allowlist — never serialize Settings. The budget reported
        # is the single-source demo budget actually applied to this run's
        # conversations (spec §1), so no tool can describe a guardrail the
        # run does not enforce.
        snapshot = {
            "llm_max_output_tokens": settings.llm_max_output_tokens,
            "conversation_token_budget": demo_token_budget,
            "llm_timeout_seconds": settings.llm_timeout_seconds,
            "deployment_name": settings.azure_openai_deployment_name,
            "agent_max_iterations": settings.agent_max_iterations,
            "agent_max_tool_calls": settings.agent_max_tool_calls,
        }
        return truncate_utf8(json.dumps(snapshot), MAX_TOOL_RESULT_BYTES)

    return get_runtime_config


_USAGE_NOT_FOUND = {
    "found": False, "spent_tokens": None, "budget_tokens": None,
    "remaining_tokens": None, "budget_state": None,
}


def make_get_conversation_usage(
    store: ConversationStore, principal: Principal, token_budget: int
) -> AgentToolFn:
    async def get_conversation_usage(conversation_id: str) -> str:
        """Look up a conversation's token spend against its lifetime budget.

        Returns JSON: {"found", "spent_tokens", "budget_tokens",
        "remaining_tokens", "budget_state"}; budget_state is one of
        "fresh", "near_exhausted", "exhausted". found=false (all other
        fields null) means no such conversation is visible to you.
        """
        # Tenant is closure-bound: cross-tenant ids and unknown ids are the
        # same not-found shape, mirroring the API's 404-same-shape rule.
        conversation = await store.get(principal.tenant_id, conversation_id)
        if conversation is None:
            return json.dumps(_USAGE_NOT_FOUND)
        spent = conversation.total_tokens
        if spent >= token_budget:
            state = "exhausted"  # post-paid ledger: one turn can overshoot (Day 9)
        elif spent >= NEAR_EXHAUSTED_THRESHOLD * token_budget:
            state = "near_exhausted"
        else:
            state = "fresh"
        return truncate_utf8(
            json.dumps({
                "found": True, "spent_tokens": spent, "budget_tokens": token_budget,
                "remaining_tokens": max(token_budget - spent, 0), "budget_state": state,
            }),
            MAX_TOOL_RESULT_BYTES,
        )

    return get_conversation_usage


__all__ = [
    "MAX_REFUSAL_RESULT_BYTES",
    "MAX_SEARCH_HITS",
    "MAX_SNIPPET_CHARS",
    "MAX_TOOL_RESULT_BYTES",
    "NEAR_EXHAUSTED_THRESHOLD",
    "AgentToolFn",
    "make_get_conversation_usage",
    "make_get_runtime_config",
    "make_search_docs",
    "truncate_utf8",
]
