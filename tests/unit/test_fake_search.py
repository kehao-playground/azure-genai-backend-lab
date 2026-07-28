import pytest
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search import SearchMode
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.azure_search import (
    AzureSearchClient,
    FakeSearchClient,
    build_search_client,
)

VECTOR = [0.1] * EMBEDDING_DIMENSIONS

DOCUMENTS = [
    {
        "chunk_id": "returns-policy-0001",
        "parent_id": "returns-policy",
        "title": "Returns Policy",
        "heading_path": "Returns Policy > Refund window",
        "content": "Customers may return most items within 30 days.",
    },
    {
        "chunk_id": "service-sla-0001",
        "parent_id": "service-sla",
        "title": "Service SLA",
        "heading_path": "Service SLA > Availability targets",
        "content": "Premium tier customers are covered by a 99.9% uptime target.",
    },
]


async def test_fake_matches_lexically_and_ranks_deterministically() -> None:
    result = await FakeSearchClient(DOCUMENTS).search("refund", mode=SearchMode.KEYWORD, top=5)
    assert [hit.chunk_id for hit in result.hits] == ["returns-policy-0001"]


async def test_fake_returns_nothing_for_an_absent_topic() -> None:
    # The no-answer path Day 14 needs: search returns an empty set, and the
    # absence is the caller's to handle — it is not signalled by an error.
    result = await FakeSearchClient(DOCUMENTS).search(
        "parental leave", VECTOR, mode=SearchMode.HYBRID, top=5
    )
    assert result.hits == ()


async def test_fake_enforces_the_same_arguments_the_service_would() -> None:
    # A fake that accepts what the real client rejects is contract drift:
    # the test suite goes green and production raises. Both clients share
    # validate_search_arguments() precisely to stop that.
    with pytest.raises(ValueError, match="requires a query vector"):
        await FakeSearchClient(DOCUMENTS).search("refund", mode=SearchMode.HYBRID, top=5)
    with pytest.raises(ValueError, match="non-empty"):
        await FakeSearchClient(DOCUMENTS).search("  ", VECTOR, mode=SearchMode.VECTOR, top=5)
    with pytest.raises(ValueError, match="dimensions"):
        await FakeSearchClient(DOCUMENTS).search("refund", [0.1], mode=SearchMode.VECTOR, top=5)


async def test_fake_records_the_parameters_it_was_called_with() -> None:
    fake = FakeSearchClient(DOCUMENTS)
    await fake.search(
        "refund",
        VECTOR,
        mode=SearchMode.HYBRID_SEMANTIC,
        top=3,
        filter="tenant_id eq 'acme'",
        vector_k=7,
    )
    assert fake.last_mode is SearchMode.HYBRID_SEMANTIC
    assert fake.last_top == 3
    assert fake.last_vector_k == 7
    assert fake.last_filter == "tenant_id eq 'acme'"


async def test_fake_never_invents_a_reranker_score() -> None:
    # It does not simulate the semantic ranker. A plausible reranker score
    # would be ranking "evidence" that is pure noise.
    result = await FakeSearchClient(DOCUMENTS).search(
        "refund", VECTOR, mode=SearchMode.HYBRID_SEMANTIC, top=5
    )
    assert all(hit.reranker_score is None for hit in result.hits)


def test_composition_point_picks_the_fake_by_default() -> None:
    assert isinstance(build_search_client(Settings(_env_file=None)), FakeSearchClient)


def test_composition_point_picks_the_real_client_when_told_to() -> None:
    settings = Settings(
        _env_file=None,
        use_fake_search=False,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
    )
    assert isinstance(build_search_client(settings), AzureSearchClient)
