import logging

import pytest

from azgenai_lab.core.config import get_settings
from azgenai_lab.models.search import SearchMode
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.retrieval import Retriever, build_retriever

DOC = {
    "chunk_id": "doc-a-0000",
    "parent_id": "doc-a",
    "title": "Doc A",
    "heading_path": "Doc A > Intro",
    "content": "hybrid search combines keyword and vector retrieval",
    "tenant_id": "t1",
    "allowed_groups": [],
}


async def test_retrieve_embeds_question_and_runs_hybrid_search() -> None:
    search = FakeSearchClient([DOC])
    retriever = Retriever(FakeEmbeddingClient(), search, top=5)
    result = await retriever.retrieve("hybrid search")
    assert result.mode is SearchMode.HYBRID
    assert search.last_top == 5
    assert [hit.chunk_id for hit in result.hits] == ["doc-a-0000"]


async def test_retrieve_returns_empty_result_when_nothing_matches() -> None:
    retriever = Retriever(FakeEmbeddingClient(), FakeSearchClient([]), top=5)
    result = await retriever.retrieve("anything")
    assert result.hits == ()


def test_build_retriever_uses_settings_top() -> None:
    retriever = build_retriever(get_settings())
    assert retriever._top == get_settings().rag_top


async def test_retrieve_logs_embed_query_stage_with_duration_and_dims(
    caplog: pytest.LogCaptureFixture,
) -> None:
    retriever = Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5)
    with caplog.at_level(logging.INFO, logger="azgenai_lab.services.retrieval"):
        await retriever.retrieve("a sensitive question that must never be logged")

    record = next(r for r in caplog.records if r.name == "azgenai_lab.services.retrieval")
    message = record.getMessage()
    assert message.startswith("rag stage=embed_query")
    assert "duration_ms=" in message
    assert "vector_dims=" in message
    assert "a sensitive question that must never be logged" not in message
