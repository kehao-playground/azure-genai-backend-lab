from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi.testclient import TestClient

from azgenai_lab.api.rag import get_rag_service
from azgenai_lab.main import app
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.search import SearchHit
from azgenai_lab.services.azure_openai import ChatResult, ChatStreamEvent, IncompleteReason
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import RagAnswer, RagService
from azgenai_lab.services.retrieval import Retriever


class StubRagService:
    def __init__(self, answer: RagAnswer) -> None:
        self._answer = answer

    async def answer(self, question: str) -> RagAnswer:
        return self._answer


def override_with(answer: RagAnswer) -> None:
    app.dependency_overrides[get_rag_service] = lambda: StubRagService(answer)


def test_rag_answered_maps_hits_to_numbered_sources(client: TestClient) -> None:
    hit = SearchHit(
        chunk_id="chunk-1",
        parent_id="doc-1",
        title="Doc Title",
        heading_path="Doc Title > Section",
        content="some content",
        score=1.5,
    )
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    override_with(
        RagAnswer(
            status="answered",
            answer="The answer is [1].",
            hits=(hit,),
            usage=usage,
            incomplete_reason=None,
        )
    )

    response = client.post(
        "/api/v1/rag",
        json={"question": "what is it?"},
        headers={"X-Correlation-Id": "test-id-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["answer"] == "The answer is [1]."
    assert body["sources"][0] == {
        "number": 1,
        "chunk_id": "chunk-1",
        "title": "Doc Title",
        "heading_path": "Doc Title > Section",
        "score": 1.5,
        "reranker_score": None,
    }
    assert body["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "reasoning_tokens": None,
    }
    assert body["correlation_id"] == "test-id-123"


def test_rag_no_answer_returns_200_with_empty_sources(client: TestClient) -> None:
    override_with(
        RagAnswer(status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None)
    )

    response = client.post("/api/v1/rag", json={"question": "anything?"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_answer"
    assert body["answer"] is None
    assert body["sources"] == []
    assert body["usage"] is None


def test_rag_empty_question_is_422_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/rag", json={"question": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["correlation_id"]


def test_rag_whitespace_only_question_is_422_envelope() -> None:
    # Day 14 review finding 1: a whitespace-only question passed the old
    # min_length=1 check, then reached models/search.py's
    # validate_search_arguments, which raised an unhandled ValueError -> a
    # plain-text 500 with no envelope. raise_server_exceptions=False lets
    # this test observe that failure mode directly if the fix regresses,
    # instead of pytest re-raising the exception past the client.
    with TestClient(app, raise_server_exceptions=False) as raw_client:
        response = raw_client.post("/api/v1/rag", json={"question": "   "})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["correlation_id"]


def test_rag_question_over_max_length_is_422(client: TestClient) -> None:
    response = client.post("/api/v1/rag", json={"question": "a" * 2001})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"


def test_rag_question_is_stripped_before_reaching_service(client: TestClient) -> None:
    class RecordingRagService:
        def __init__(self) -> None:
            self.received_question: str | None = None

        async def answer(self, question: str) -> RagAnswer:
            self.received_question = question
            return RagAnswer(
                status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None
            )

    service = RecordingRagService()
    app.dependency_overrides[get_rag_service] = lambda: service

    response = client.post("/api/v1/rag", json={"question": "  what is it?  "})

    assert response.status_code == 200
    assert service.received_question == "what is it?"


DOC = {
    "chunk_id": "doc-a-0000",
    "parent_id": "doc-a",
    "title": "Doc A",
    "heading_path": "Doc A > Intro",
    "content": "alpha beta",
}


class StubIncompleteChatService:
    """Real-shaped ChatService that always returns an incomplete ChatResult
    with a fixed reason, so the real RagService's plumbing (not a stub
    RagAnswer) is what's under test (Day 14 review finding 9)."""

    def __init__(self, incomplete_reason: IncompleteReason) -> None:
        self._incomplete_reason = incomplete_reason

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        return ChatResult(
            message="partial answer",
            status="incomplete",
            incomplete_reason=self._incomplete_reason,
        )

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        raise AssertionError("this test never streams")

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter", "other"])
def test_rag_incomplete_reason_flows_through_real_service_to_http(
    client: TestClient, reason: IncompleteReason
) -> None:
    service = RagService(
        Retriever(FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5),
        StubIncompleteChatService(reason),
    )
    app.dependency_overrides[get_rag_service] = lambda: service

    response = client.post("/api/v1/rag", json={"question": "alpha"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["incomplete_reason"] == reason
    # OpenAPI-required fields are present in the body even though they can be null.
    assert "usage" in body
    assert "incomplete_reason" in body


def test_openapi_schema_marks_usage_and_incomplete_reason_required() -> None:
    schema = app.openapi()
    rag_response_schema = schema["components"]["schemas"]["RagResponse"]
    required = set(rag_response_schema.get("required", []))
    assert "usage" in required
    assert "incomplete_reason" in required


def test_rag_endpoint_wired_at_startup(client: TestClient) -> None:
    # No override: exercises the app-state service built at startup, with the
    # fake embeddings + fake search adapters and no corpus -> no_answer.
    response = client.post("/api/v1/rag", json={"question": "anything?"})

    assert response.status_code == 200
    assert response.json()["status"] == "no_answer"
