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

# A representative ACL expression. These tests are about request *shape*, so
# the exact clause does not matter here — how it is built is test_acl.py's
# subject, and that it is derived from the principal is test_azure_search.py's.
FILTER = "tenant_id eq 't1' and not allowed_groups/any()"


def test_keyword_sends_no_vector_at_all() -> None:
    # Requiring a vector here would make every keyword query pay for an
    # embedding it never uses, and would pollute the latency the live session
    # measures for this mode.
    body = build_search_body("refund window", mode=SearchMode.KEYWORD, top=5, filter=FILTER)
    assert body["search"] == "refund window"
    assert "vectorQueries" not in body
    assert body["top"] == 5


def test_vector_keeps_the_query_text_but_does_not_send_it_as_search() -> None:
    body = build_search_body(
        "refund window", VECTOR, mode=SearchMode.VECTOR, top=5, filter=FILTER
    )
    assert "search" not in body
    assert body["vectorQueries"] == [
        {"kind": "vector", "vector": VECTOR, "fields": VECTOR_FIELD, "k": DEFAULT_VECTOR_K}
    ]
    assert body["vectorFilterMode"] == "preFilter"
    assert body["filter"] == FILTER


def test_hybrid_sends_both_sides() -> None:
    body = build_search_body(
        "refund window", VECTOR, mode=SearchMode.HYBRID, top=5, filter=FILTER
    )
    assert body["search"] == "refund window"
    assert body["vectorQueries"][0]["k"] == DEFAULT_VECTOR_K
    assert body["vectorFilterMode"] == "preFilter"
    assert body["filter"] == FILTER
    assert "queryType" not in body


def test_semantic_adds_query_type_and_configuration() -> None:
    body = build_search_body(
        "refund window", VECTOR, mode=SearchMode.HYBRID_SEMANTIC, top=5, filter=FILTER
    )
    assert body["queryType"] == "semantic"
    assert body["semanticConfiguration"] == SEMANTIC_CONFIGURATION_NAME


def test_vector_k_is_explicit_and_independent_of_top() -> None:
    # k, top and the semantic ranker's 50 are three different numbers.
    body = build_search_body(
        "q", VECTOR, mode=SearchMode.VECTOR, top=5, vector_k=3, filter=FILTER
    )
    assert body["vectorQueries"][0]["k"] == 3
    assert body["top"] == 5


def test_the_filter_is_always_sent_and_has_no_default() -> None:
    # An unfiltered body is not a shape this builder can produce: omitting the
    # argument is a TypeError, not a query that quietly reads every tenant.
    with pytest.raises(TypeError):
        build_search_body("q", mode=SearchMode.KEYWORD, top=5)  # type: ignore[call-arg]
    body = build_search_body("q", mode=SearchMode.KEYWORD, top=5, filter=FILTER)
    assert body["filter"] == FILTER


def test_a_filterless_mode_still_carries_no_vector_filter_mode() -> None:
    # `vectorFilterMode` describes when the filter is applied to an ANN
    # search. KEYWORD runs no vector query, so sending it there would name a
    # stage that does not exist in that request.
    body = build_search_body("q", mode=SearchMode.KEYWORD, top=5, filter=FILTER)
    assert "vectorFilterMode" not in body


@pytest.mark.parametrize(
    "mode", [SearchMode.VECTOR, SearchMode.HYBRID, SearchMode.HYBRID_SEMANTIC]
)
def test_vector_modes_require_a_vector(mode: SearchMode) -> None:
    with pytest.raises(ValueError, match="requires a query vector"):
        validate_search_arguments("q", None, mode=mode, top=5)


@pytest.mark.parametrize("mode", list(SearchMode))
def test_every_mode_requires_non_empty_query_text(mode: SearchMode) -> None:
    # Spec §2.4: query_text is required in *every* mode. VECTOR retains it for
    # logging and comparison even though it is not sent as `search`, so an
    # empty one is a caller bug there too.
    vector = None if mode is SearchMode.KEYWORD else VECTOR
    with pytest.raises(ValueError, match="non-empty"):
        validate_search_arguments("   ", vector, mode=mode, top=5)


def test_wrong_width_vector_is_rejected_before_the_request_is_built() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        validate_search_arguments("q", [0.1, 0.2], mode=SearchMode.VECTOR, top=5)


def test_keyword_mode_tolerates_a_vector_being_supplied() -> None:
    # A caller that already has a vector may pass it; keyword simply will not
    # send it. Rejecting would force callers to branch on mode.
    body = build_search_body("q", VECTOR, mode=SearchMode.KEYWORD, top=5, filter=FILTER)
    assert "vectorQueries" not in body
