"""Query-side vocabulary: the four retrieval modes and the request they build.

Retrieval is three stages that fail separately — candidate generation, fusion
and ranking. The modes exist so the same question can be put to one stage at a
time; without that, a bad answer is unattributable.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from azgenai_lab.models.search_index import (
    EMBEDDING_DIMENSIONS,
    SEMANTIC_CONFIGURATION_NAME,
    VECTOR_FIELD,
)

# The semantic ranker reranks up to 50 candidates from the merged result set,
# so a vector side offering fewer than 50 starves it. That is the reason for
# this default — it is not a claim that 50 *is* the semantic candidate count,
# and it does not control the keyword side at all.
DEFAULT_VECTOR_K = 50

# Vectors are never selected back: not human-readable, large, and the field is
# not retrievable anyway.
SELECT_FIELDS = ("chunk_id", "parent_id", "title", "heading_path", "content")


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"
    HYBRID_SEMANTIC = "hybrid_semantic"


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk.

    Two scores, never collapsed. ``score`` is BM25, a cosine-derived score, or
    an RRF score depending on the mode, and RRF scores are an order of
    magnitude smaller than similarity scores by construction.
    ``reranker_score`` is the only one with a published rubric (4.0 answers
    completely ... 0.0 irrelevant), and 0.0 is a verdict, not a missing value.
    """

    chunk_id: str
    parent_id: str
    title: str
    heading_path: str
    content: str
    score: float
    reranker_score: float | None = None


@dataclass(frozen=True)
class SearchResult:
    """Hits plus the parameters that produced them, so a log line or an
    evidence file never has to reconstruct the call."""

    hits: tuple[SearchHit, ...]
    mode: SearchMode
    vector_k: int | None


_VECTOR_MODES = frozenset({SearchMode.VECTOR, SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC})
_TEXT_QUERY_MODES = frozenset(
    {SearchMode.KEYWORD, SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC}
)


def validate_search_arguments(
    query_text: str,
    query_vector: Sequence[float] | None,
    *,
    mode: SearchMode,
) -> None:
    """The one validator every ``SearchClient`` implementation must apply.

    It lives apart from ``build_search_body`` so the fake enforces exactly the
    same contract as the real adapter. A fake that accepts calls the service
    would reject turns a green test suite into a production failure.
    """
    if not query_text.strip():
        # Required in every mode, including VECTOR: the text is retained for
        # logging and comparison there, and an empty one is a caller bug.
        raise ValueError(f"mode {mode} requires a non-empty query text")
    if mode in _VECTOR_MODES:
        if query_vector is None:
            raise ValueError(f"mode {mode} requires a query vector")
        if len(query_vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"query vector has {len(query_vector)} dimensions; "
                f"the index expects {EMBEDDING_DIMENSIONS}"
            )


def build_search_body(
    query_text: str,
    query_vector: Sequence[float] | None = None,
    *,
    mode: SearchMode,
    top: int,
    filter: str | None = None,
    vector_k: int = DEFAULT_VECTOR_K,
) -> dict[str, Any]:
    """Build the Search Documents request body for one mode.

    ``filter`` is a **trusted internal OData expression**. Day 15's tenant
    isolation must not interpolate a user-supplied value into this string; it
    has to be built from a typed authorization context and escaped there.
    """
    validate_search_arguments(query_text, query_vector, mode=mode)

    body: dict[str, Any] = {"top": top, "select": ",".join(SELECT_FIELDS)}
    if mode in _TEXT_QUERY_MODES:
        body["search"] = query_text
    if mode in _VECTOR_MODES:
        assert query_vector is not None  # narrowed by the validator
        body["vectorQueries"] = [
            {
                "kind": "vector",
                "vector": list(query_vector),
                "fields": VECTOR_FIELD,
                "k": vector_k,
            }
        ]
    if mode is SearchMode.HYBRID_SEMANTIC:
        body["queryType"] = "semantic"
        body["semanticConfiguration"] = SEMANTIC_CONFIGURATION_NAME
    if filter is not None:
        body["filter"] = filter
    return body


__all__ = [
    "DEFAULT_VECTOR_K",
    "SELECT_FIELDS",
    "SearchHit",
    "SearchMode",
    "SearchResult",
    "build_search_body",
    "validate_search_arguments",
]
