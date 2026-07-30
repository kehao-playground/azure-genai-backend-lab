from fastapi.testclient import TestClient

from azgenai_lab.api.rag import get_rag_service
from azgenai_lab.main import app
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.search import SearchHit
from azgenai_lab.services.rag import RagAnswer


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


def test_rag_endpoint_wired_at_startup(client: TestClient) -> None:
    # No override: exercises the app-state service built at startup, with the
    # fake embeddings + fake search adapters and no corpus -> no_answer.
    response = client.post("/api/v1/rag", json={"question": "anything?"})

    assert response.status_code == 200
    assert response.json()["status"] == "no_answer"
