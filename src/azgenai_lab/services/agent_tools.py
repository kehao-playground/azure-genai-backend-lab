"""Least-privilege read-only tools for the Day 17 ops-assistant agent.

Each tool is a closure over app-owned dependencies. The agent chooses
arguments; it can never choose the tenant, the groups, the store, or the
budget — those are fixed at composition (fail-closed, Day 15 / Day 16 R2).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.chunking import chunk_markdown
from azgenai_lab.services.conversation_store import ConversationStore
from azgenai_lab.services.document_loader import load_documents
from azgenai_lab.services.embeddings import FakeEmbeddingClient, embed_chunks
from azgenai_lab.services.retrieval import Retriever, build_retriever

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


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run one async call from this module's synchronous composition code.

    `build_agent_toolset` follows this repo's synchronous `build_*`
    convention (composition happens once, with no event loop of its own),
    but seeding the fake retriever must await `embed_chunks` — the same
    batching call `tools/index_corpus.py` awaits when it builds a live
    index. A dedicated thread runs its own event loop for that one call, so
    this also works when `build_agent_toolset` is itself invoked from
    inside a running loop (as in this module's own async tests), where a
    bare `asyncio.run()` would raise "cannot be called from a running event
    loop".
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


async def _seed_index_documents(
    embedding_client: FakeEmbeddingClient, settings: Settings
) -> list[dict[str, Any]]:
    """Load, chunk and embed every sample document into index-document shape.

    Mirrors the per-document loop in `tools/index_corpus.py` (load ->
    chunk -> embed -> `to_index_document`) exactly, with a fake embedding
    client standing in for the live one. Every tenant's corpus is included,
    not just opsdemo's: the real system is one shared index scoped by a
    document-level ACL filter (Day 15), and a fake that only ever held one
    tenant's documents would not exercise that filter at all. Correctness
    still comes from `FakeSearchClient.search`, which enforces the same
    `is_document_visible` policy the real service's ACL filter expresses in
    OData.
    """
    index_documents: list[dict[str, Any]] = []
    for source in load_documents():
        chunks = chunk_markdown(
            source, max_chars=settings.chunk_max_chars, overlap_chars=settings.chunk_overlap_chars
        )
        vectors = await embed_chunks(embedding_client, chunks)
        index_documents.extend(
            chunk.to_index_document(vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
    return index_documents


def _seeded_fake_retriever(settings: Settings) -> Retriever:
    """A `Retriever` over a `FakeSearchClient` seeded with the real corpus.

    In fake mode, `build_search_client` returns an **empty**
    `FakeSearchClient` (Day 13) — a wiring demo over zero documents would
    prove nothing, so this seeds it through the real chunking pipeline
    instead, with `FakeEmbeddingClient` standing in for a live embedding
    call. Its vectors carry no semantics (Day 12), which is fine here:
    `FakeSearchClient` scores lexically in every mode and never reads them.
    """
    embedding_client = FakeEmbeddingClient()
    index_documents = _run_sync(_seed_index_documents(embedding_client, settings))
    return Retriever(embedding_client, FakeSearchClient(index_documents), top=settings.rag_top)


@dataclass
class AgentToolset:
    """Tools plus the ownership handles the composite service must close."""

    tools: tuple[AgentToolFn, ...]
    retriever: Retriever
    conversation_store: ConversationStore


def build_agent_toolset(
    settings: Settings,
    principal: Principal,
    *,
    conversation_store: ConversationStore,
    token_budget: int,
) -> AgentToolset:
    """Bind all three least-privilege tools to one shared set of dependencies.

    `principal`, `conversation_store` and `token_budget` are each fixed once
    here and closed over by every tool that needs them — the agent chooses
    only the arguments the tool signatures expose (query text, a
    conversation id), never the tenant, groups, store or budget (Day 15 /
    Day 16 R2). `token_budget` is one number passed to both
    `make_get_runtime_config` and `make_get_conversation_usage`, so the
    guardrail the config tool reports and the one the usage tool checks
    against can never drift apart. `conversation_store` is the same object
    the caller seeds a demo conversation into, so that conversation is
    visible through `get_conversation_usage`.
    """
    retriever = (
        _seeded_fake_retriever(settings) if settings.use_fake_search else build_retriever(settings)
    )
    tools = (
        make_search_docs(retriever, principal),
        make_get_runtime_config(settings, token_budget),
        make_get_conversation_usage(conversation_store, principal, token_budget),
    )
    return AgentToolset(tools=tools, retriever=retriever, conversation_store=conversation_store)


__all__ = [
    "MAX_REFUSAL_RESULT_BYTES",
    "MAX_SEARCH_HITS",
    "MAX_SNIPPET_CHARS",
    "MAX_TOOL_RESULT_BYTES",
    "NEAR_EXHAUSTED_THRESHOLD",
    "AgentToolFn",
    "AgentToolset",
    "build_agent_toolset",
    "make_get_conversation_usage",
    "make_get_runtime_config",
    "make_search_docs",
    "truncate_utf8",
]
