"""Query-side vocabulary: the four retrieval modes and what each one needs.

Retrieval is three stages that fail separately — candidate generation, fusion
and ranking. The modes exist so the same question can be put to one stage at a
time; without that, a bad answer is unattributable.

Nothing here knows how a request is *spelled*. Which JSON fields Azure AI
Search expects, and in what shape, belongs to the adapter in
``services/azure_search.py``. A mode, a hit and an argument contract outlive
any one search service; a field name does not.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS

# The semantic ranker reranks up to 50 candidates from the merged result set,
# so a vector side offering fewer than 50 starves it. That is the reason for
# this default — it is not a claim that 50 *is* the semantic candidate count,
# and it does not control the keyword side at all.
DEFAULT_VECTOR_K = 50

# The documented maximum page size (checked 2026-07): "The default page size is
# 50, while the maximum page size is 1,000. If you specify a value greater than
# 1,000 and there are more than 1,000 results found in your index, only the
# first 1,000 results are returned."
#
# That last clause is why this is enforced rather than left to the service: a
# `top` above the ceiling is not rejected, it is silently honoured as 1,000.
# The caller asked one question and got the answer to another, with a 200 and
# no warning to say so.
MAX_TOP = 1_000

MIN_BOUND = 1

# `vector_k` has a documented *type* — int32 — and no documented bound in
# either direction. That type is still a bound: a value outside int32 cannot
# be the field the reference describes, whatever the service does with it.
# It is also the only ceiling this project may put on `vector_k`. Picking a
# smaller one would mean writing down a number nobody published and then
# citing it — the same trap as an unmeasured constant. `top` is different:
# its ceiling of 1,000 *is* published, which is why the two differ.
INT32_MAX = 2**31 - 1


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"
    HYBRID_SEMANTIC = "hybrid_semantic"


# Which stages a mode asks for, stated once. The adapter reads these to decide
# what to put on the wire; the validator reads them to decide what a caller
# must supply. Two copies of this fact would let the two disagree about a mode.
VECTOR_MODES = frozenset({SearchMode.VECTOR, SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC})
TEXT_QUERY_MODES = frozenset(
    {SearchMode.KEYWORD, SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC}
)


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


def _validate_count(value: object, *, name: str, maximum: int) -> None:
    """Bound one of the "how many" parameters, or say which one was wrong.

    The parameter is typed ``object`` because the values worth guarding
    against are the ones the type system never saw: a JSON body, a CLI
    argument, a dict from a config file. Where a value came from is what
    decides whether it is trustworthy, and by the time it reaches here that
    is no longer visible — so it is checked rather than assumed.

    Two rejections that a range comparison alone does not make:

    * ``bool`` is a subclass of ``int``, so ``True`` passes both
      ``isinstance(value, int)`` and ``value >= 1`` and reaches the wire as
      ``true`` — a JSON boolean where the service documents a number.
    * a positive ``float`` also passes ``>= 1``, and then the two
      implementations diverge: the fake slices a list with it (or raises),
      while the real adapter forwards a JSON float. Fake and real disagreeing
      about what is legal is the one failure this validator exists to
      prevent, so the type is checked, not just the range.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int; got {type(value).__name__} {value!r}")
    if value < MIN_BOUND:
        raise ValueError(f"{name} must be at least {MIN_BOUND}; got {value}")
    if value > maximum:
        raise ValueError(f"{name} must be at most {maximum}; got {value}")


def validate_search_arguments(
    query_text: str,
    query_vector: Sequence[float] | None,
    *,
    mode: SearchMode,
    top: int,
    vector_k: int = DEFAULT_VECTOR_K,
) -> None:
    """The one validator every ``SearchClient`` implementation must apply.

    It lives here, apart from any request builder, so the fake enforces exactly
    the same contract as the real adapter. A fake that accepts calls the
    service would reject turns a green test suite into a production failure.

    ``vector_k`` is bounded in every mode, not only the ones that send it.
    KEYWORD ignores the value, so a nonsensical one there is inert rather than
    harmful — but a caller passing ``-5`` has a bug either way, and a rule that
    fires only in some modes teaches callers to find out which.

    The two counts are annotated ``int`` because that is what a typed caller
    owes this function. They are checked anyway: the callers that matter here
    are the ones the annotations never reached.
    """
    _validate_count(top, name="top", maximum=MAX_TOP)
    _validate_count(vector_k, name="vector_k", maximum=INT32_MAX)
    if not query_text.strip():
        # Required in every mode, including VECTOR: the text is retained for
        # logging and comparison there, and an empty one is a caller bug.
        raise ValueError(f"mode {mode} requires a non-empty query text")
    if mode in VECTOR_MODES:
        if query_vector is None:
            raise ValueError(f"mode {mode} requires a query vector")
        if len(query_vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"query vector has {len(query_vector)} dimensions; "
                f"the index expects {EMBEDDING_DIMENSIONS}"
            )


__all__ = [
    "DEFAULT_VECTOR_K",
    "INT32_MAX",
    "MAX_TOP",
    "MIN_BOUND",
    "TEXT_QUERY_MODES",
    "VECTOR_MODES",
    "SearchHit",
    "SearchMode",
    "SearchResult",
    "validate_search_arguments",
]
