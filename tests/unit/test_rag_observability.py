"""Day 14 review finding 5: the RAG pipeline's stages must be joinable on
correlation_id, and the augmentation stage (which Day 13's search line and
Day 8/9's LLM lines don't cover) must log its own shape without leaking
question text or chunk content.
"""

import logging
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi.testclient import TestClient

from azgenai_lab.api.rag import get_rag_service
from azgenai_lab.core.correlation import correlation_id_var
from azgenai_lab.core.errors import UpstreamServiceError
from azgenai_lab.main import app
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.search import SearchMode, SearchResult
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import ChatResult, ChatStreamEvent, FakeChatService
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import EmbeddingRejectedError, FakeEmbeddingClient
from azgenai_lab.services.rag import RagService
from azgenai_lab.services.retrieval import Retriever

DOC = {
    "chunk_id": "doc-a-0000",
    "parent_id": "doc-a",
    "title": "Doc A",
    "heading_path": "Doc A > Intro",
    "content": "alpha beta",
    "tenant_id": "t1",
    "allowed_groups": [],
}


def test_rag_stage_log_carries_request_correlation_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        FakeChatService(prompt=load_prompt("rag_answer")),
    )
    app.dependency_overrides[get_rag_service] = lambda: service
    try:
        with caplog.at_level(logging.INFO, logger="azgenai_lab.services.rag"):
            response = client.post(
                "/api/v1/rag",
                json={"question": "what is alpha?"},
                headers={"X-Correlation-Id": "cid-rag-stage"},
            )
    finally:
        app.dependency_overrides.pop(get_rag_service, None)

    assert response.status_code == 200
    record = next(r for r in caplog.records if r.name == "azgenai_lab.services.rag")
    assert record.correlation_id == "cid-rag-stage"


async def test_rag_stage_log_has_counts_and_lengths_not_question_or_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        FakeChatService(prompt=load_prompt("rag_answer")),
    )
    token = correlation_id_var.set("cid-content-check")
    try:
        with caplog.at_level(logging.INFO, logger="azgenai_lab.services.rag"):
            await service.answer("does alpha do beta things?")
    finally:
        correlation_id_var.reset(token)

    record = next(r for r in caplog.records if r.name == "azgenai_lab.services.rag")
    message = record.getMessage()

    # Shape: counts, ids, and lengths only.
    assert "hits=1" in message
    assert "chunk_ids=doc-a-0000" in message
    assert "content_chars=10" in message  # len("alpha beta")
    assert "question_chars=" in message
    assert "context_chars=" in message

    # Redaction: neither the question text nor the chunk content may appear.
    assert "does alpha do beta things?" not in message
    assert "alpha beta" not in message


async def test_rag_stage_log_on_no_answer_path_has_zero_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(Retriever(FakeEmbeddingClient(), FakeSearchClient([]), top=5), None)  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger="azgenai_lab.services.rag"):
        result = await service.answer("anything with no corpus match")

    assert result.status == "no_answer"
    record = next(r for r in caplog.records if r.name == "azgenai_lab.services.rag")
    message = record.getMessage()
    assert "hits=0" in message
    assert "chunk_ids=" in message
    assert "anything with no corpus match" not in message


async def test_rag_answered_path_logs_generation_and_total_duration_correlation_joined(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        FakeChatService(prompt=load_prompt("rag_answer")),
    )
    token = correlation_id_var.set("cid-duration-answered")
    try:
        with caplog.at_level(logging.INFO, logger="azgenai_lab.services.rag"):
            result = await service.answer("alpha")
    finally:
        correlation_id_var.reset(token)

    assert result.status == "answered"
    rag_records = [r for r in caplog.records if r.name == "azgenai_lab.services.rag"]
    assert all(r.correlation_id == "cid-duration-answered" for r in rag_records)

    generation_record = next(r for r in rag_records if "stage=generation" in r.getMessage())
    assert "duration_ms=" in generation_record.getMessage()

    complete_record = next(r for r in rag_records if "stage=complete" in r.getMessage())
    complete_message = complete_record.getMessage()
    assert "total_ms=" in complete_message
    assert "status=answered" in complete_message

    for record in rag_records:
        message = record.getMessage()
        assert "alpha" not in message
        assert "alpha beta" not in message


async def test_rag_no_answer_path_logs_total_duration_without_generation_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(Retriever(FakeEmbeddingClient(), FakeSearchClient([]), top=5), None)  # type: ignore[arg-type]

    token = correlation_id_var.set("cid-duration-no-answer")
    try:
        with caplog.at_level(logging.INFO, logger="azgenai_lab.services.rag"):
            result = await service.answer("anything with no corpus match")
    finally:
        correlation_id_var.reset(token)

    assert result.status == "no_answer"
    rag_records = [r for r in caplog.records if r.name == "azgenai_lab.services.rag"]
    assert all(r.correlation_id == "cid-duration-no-answer" for r in rag_records)

    complete_record = next(r for r in rag_records if "stage=complete" in r.getMessage())
    complete_message = complete_record.getMessage()
    assert "total_ms=" in complete_message
    assert "status=no_answer" in complete_message

    assert not any("stage=generation" in r.getMessage() for r in rag_records)
    for record in rag_records:
        assert "anything with no corpus match" not in record.getMessage()


class _RaisingEmbeddingClient:
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingRejectedError("simulated embedding failure")

    async def aclose(self) -> None:
        pass


class _RaisingSearchClient:
    async def search(
        self,
        query_text: str,
        query_vector: Sequence[float] | None = None,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top: int,
        filter: str | None = None,
        vector_k: int = 50,
    ) -> SearchResult:
        raise UpstreamServiceError("simulated search failure")

    async def aclose(self) -> None:
        pass


class _RaisingChatService:
    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        raise UpstreamServiceError("simulated generation failure")

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        raise AssertionError("this test never streams")

    async def aclose(self) -> None:
        pass


async def test_embed_stage_failure_logs_error_outcome_and_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(
        Retriever(_RaisingEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        FakeChatService(prompt=load_prompt("rag_answer")),
    )
    token = correlation_id_var.set("cid-embed-fail")
    try:
        with caplog.at_level(logging.INFO), pytest.raises(EmbeddingRejectedError):
            await service.answer("does alpha do beta things?")
    finally:
        correlation_id_var.reset(token)

    retrieval_records = [r for r in caplog.records if r.name == "azgenai_lab.services.retrieval"]
    embed_record = next(r for r in retrieval_records if "stage=embed_query" in r.getMessage())
    assert "outcome=error" in embed_record.getMessage()
    assert "duration_ms=" in embed_record.getMessage()
    assert "EmbeddingRejectedError" in embed_record.getMessage()
    assert embed_record.correlation_id == "cid-embed-fail"

    rag_records = [r for r in caplog.records if r.name == "azgenai_lab.services.rag"]
    complete_record = next(r for r in rag_records if "stage=complete" in r.getMessage())
    complete_message = complete_record.getMessage()
    assert "outcome=error" in complete_message
    assert "total_ms=" in complete_message
    assert "failed_stage=retrieve" in complete_message

    for record in caplog.records:
        assert "does alpha do beta things?" not in record.getMessage()
        assert "alpha beta" not in record.getMessage()


async def test_search_stage_failure_logs_error_outcome_and_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), _RaisingSearchClient(), top=5),
        FakeChatService(prompt=load_prompt("rag_answer")),
    )
    token = correlation_id_var.set("cid-search-fail")
    try:
        with caplog.at_level(logging.INFO), pytest.raises(UpstreamServiceError):
            await service.answer("does alpha do beta things?")
    finally:
        correlation_id_var.reset(token)

    retrieval_records = [r for r in caplog.records if r.name == "azgenai_lab.services.retrieval"]
    search_record = next(r for r in retrieval_records if "stage=search" in r.getMessage())
    assert "outcome=error" in search_record.getMessage()
    assert "duration_ms=" in search_record.getMessage()
    assert "UpstreamServiceError" in search_record.getMessage()
    assert search_record.correlation_id == "cid-search-fail"

    rag_records = [r for r in caplog.records if r.name == "azgenai_lab.services.rag"]
    complete_record = next(r for r in rag_records if "stage=complete" in r.getMessage())
    complete_message = complete_record.getMessage()
    assert "outcome=error" in complete_message
    assert "total_ms=" in complete_message
    assert "failed_stage=retrieve" in complete_message

    for record in caplog.records:
        assert "does alpha do beta things?" not in record.getMessage()
        assert "alpha beta" not in record.getMessage()


async def test_generation_stage_failure_logs_error_outcome_and_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        _RaisingChatService(),
    )
    token = correlation_id_var.set("cid-generation-fail")
    try:
        with caplog.at_level(logging.INFO), pytest.raises(UpstreamServiceError):
            await service.answer("does alpha do beta things?")
    finally:
        correlation_id_var.reset(token)

    rag_records = [r for r in caplog.records if r.name == "azgenai_lab.services.rag"]
    generation_record = next(r for r in rag_records if "stage=generation" in r.getMessage())
    assert "outcome=error" in generation_record.getMessage()
    assert "duration_ms=" in generation_record.getMessage()
    assert "UpstreamServiceError" in generation_record.getMessage()
    assert generation_record.correlation_id == "cid-generation-fail"

    complete_record = next(r for r in rag_records if "stage=complete" in r.getMessage())
    complete_message = complete_record.getMessage()
    assert "outcome=error" in complete_message
    assert "total_ms=" in complete_message
    assert "failed_stage=generation" in complete_message

    for record in caplog.records:
        assert "does alpha do beta things?" not in record.getMessage()
        assert "alpha beta" not in record.getMessage()
