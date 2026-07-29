"""The request body is the contract; each mode sends a different shape."""

import pytest

from azgenai_lab.models.search import (
    DEFAULT_VECTOR_K,
    SearchMode,
    validate_search_arguments,
)
from azgenai_lab.models.search_index import (
    EMBEDDING_DIMENSIONS,
    SEMANTIC_CONFIGURATION_NAME,
    VECTOR_FIELD,
)
from azgenai_lab.services.azure_search import build_search_body

VECTOR = [0.1] * EMBEDDING_DIMENSIONS


def test_keyword_sends_no_vector_at_all() -> None:
    # Requiring a vector here would make every keyword query pay for an
    # embedding it never uses, and would pollute the latency the live session
    # measures for this mode.
    body = build_search_body("refund window", mode=SearchMode.KEYWORD, top=5)
    assert body["search"] == "refund window"
    assert "vectorQueries" not in body
    assert body["top"] == 5


def test_vector_keeps_the_query_text_but_does_not_send_it_as_search() -> None:
    body = build_search_body("refund window", VECTOR, mode=SearchMode.VECTOR, top=5)
    assert "search" not in body
    assert body["vectorQueries"] == [
        {"kind": "vector", "vector": VECTOR, "fields": VECTOR_FIELD, "k": DEFAULT_VECTOR_K}
    ]


def test_hybrid_sends_both_sides() -> None:
    body = build_search_body("refund window", VECTOR, mode=SearchMode.HYBRID, top=5)
    assert body["search"] == "refund window"
    assert body["vectorQueries"][0]["k"] == DEFAULT_VECTOR_K
    assert "queryType" not in body


def test_semantic_adds_query_type_and_configuration() -> None:
    body = build_search_body("refund window", VECTOR, mode=SearchMode.HYBRID_SEMANTIC, top=5)
    assert body["queryType"] == "semantic"
    assert body["semanticConfiguration"] == SEMANTIC_CONFIGURATION_NAME


def test_vector_k_is_explicit_and_independent_of_top() -> None:
    # k, top and the semantic ranker's 50 are three different numbers.
    body = build_search_body("q", VECTOR, mode=SearchMode.VECTOR, top=5, vector_k=3)
    assert body["vectorQueries"][0]["k"] == 3
    assert body["top"] == 5


def test_filter_is_included_only_when_given() -> None:
    assert "filter" not in build_search_body("q", mode=SearchMode.KEYWORD, top=5)
    body = build_search_body(
        "q", mode=SearchMode.KEYWORD, top=5, filter="tenant_id eq 'acme'"
    )
    assert body["filter"] == "tenant_id eq 'acme'"


@pytest.mark.parametrize(
    "mode", [SearchMode.VECTOR, SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC]
)
def test_vector_modes_require_a_vector(mode: SearchMode) -> None:
    with pytest.raises(ValueError, match="requires a query vector"):
        validate_search_arguments("q", None, mode=mode)


@pytest.mark.parametrize("mode", list(SearchMode))
def test_every_mode_requires_non_empty_query_text(mode: SearchMode) -> None:
    # Spec §2.4: query_text is required in *every* mode. VECTOR retains it for
    # logging and comparison even though it is not sent as `search`, so an
    # empty one is a caller bug there too.
    vector = None if mode is SearchMode.KEYWORD else VECTOR
    with pytest.raises(ValueError, match="non-empty"):
        validate_search_arguments("   ", vector, mode=mode)


def test_wrong_width_vector_is_rejected_before_the_request_is_built() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        validate_search_arguments("q", [0.1, 0.2], mode=SearchMode.VECTOR)


def test_keyword_mode_tolerates_a_vector_being_supplied() -> None:
    # A caller that already has a vector may pass it; keyword simply will not
    # send it. Rejecting would force callers to branch on mode.
    body = build_search_body("q", VECTOR, mode=SearchMode.KEYWORD, top=5)
    assert "vectorQueries" not in body
