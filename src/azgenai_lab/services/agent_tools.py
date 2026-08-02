"""Least-privilege read-only tools for the Day 17 ops-assistant agent.

Each tool is a closure over app-owned dependencies. The agent chooses
arguments; it can never choose the tenant, the groups, the store, or the
budget — those are fixed at composition (fail-closed, Day 15 / Day 16 R2).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from azgenai_lab.models.principal import Principal
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


def make_search_docs(retriever: Retriever, principal: Principal) -> AgentToolFn:
    async def search_docs(query: str) -> str:
        """Search this backend's operations documentation.

        Returns JSON: {"hits": [{"source", "heading_path", "snippet"}, ...]}.
        An empty "hits" list means no document matched the query.
        """
        result = await retriever.retrieve(query, principal)
        hits = [
            {
                "source": hit.parent_id,
                "heading_path": hit.heading_path,
                "snippet": hit.content[:MAX_SNIPPET_CHARS],
            }
            for hit in result.hits[:MAX_SEARCH_HITS]
        ]
        return truncate_utf8(
            json.dumps({"hits": hits}, ensure_ascii=False), MAX_TOOL_RESULT_BYTES
        )

    return search_docs


__all__ = [
    "MAX_REFUSAL_RESULT_BYTES",
    "MAX_SEARCH_HITS",
    "MAX_SNIPPET_CHARS",
    "MAX_TOOL_RESULT_BYTES",
    "NEAR_EXHAUSTED_THRESHOLD",
    "AgentToolFn",
    "make_search_docs",
    "truncate_utf8",
]
