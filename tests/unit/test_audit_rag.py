"""``rag.query`` emission from RagService.answer() (Day 22 Task 8).

Unlike chat.turn/agent.run, rag.query is emitted from the *service* layer,
not an endpoint finalizer: RagService.answer() has no Request (it is called
directly in some tests, and potentially by future non-HTTP callers), so each
of its three terminals guards emission with has_audit_context() rather than
assuming one always applies. Fixtures reach into the already-built
app.state.rag_service and swap only ._retriever / ._chat_service, so the
service keeps the audit_attribution the composition point actually built
(same "same file, same sha256 is not the same instance" concern Task 6
documents for /chat).
"""

import logging
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import IDENTITY, audit_events

from azgenai_lab.core.audit import has_audit_context, request_duration_ms
from azgenai_lab.core.config import Settings
from azgenai_lab.core.correlation import correlation_id_var, request_start_var
from azgenai_lab.core.errors import ContentFilteredError, UpstreamServiceError
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search import SearchHit, SearchMode, SearchResult
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.embeddings import EmbeddingRejectedError, FakeEmbeddingClient
from azgenai_lab.services.rag import MAX_PROMPT_BYTES, build_rag_service
from azgenai_lab.services.retrieval import Retriever

PRINCIPAL = Principal(tenant_id="t1", user_id="u1", group_ids=())

DOC = {
    "chunk_id": "doc-a-0000",
    "parent_id": "doc-a",
    "title": "Doc A",
    "heading_path": "Doc A > Intro",
    "content": "alpha refund window",
    "tenant_id": "t1",
    "allowed_groups": [],
}


class _RaisingRetriever:
    """Substitutes the whole retrieve() step -- embed vs. search are both
    "retrieve" stage as far as RagQueryEvent.failed_stage is concerned, so a
    single stub raising directly is enough to drive that stage's failure
    path without separately faking the embedding/search split."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def retrieve(self, question: str, principal: Principal) -> object:
        raise self._error

    async def aclose(self) -> None:
        pass


class _RaisingChatService:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def complete(self, items: Sequence[object]) -> object:
        raise self._error

    async def aclose(self) -> None:
        pass


class _OversizedSearchClient:
    """Duck-typed SearchClient returning a fixed hostile hit set (same shape
    as test_rag_api.py's helper of the same name) -- drives the
    assemble_context-stage failure, which never reaches the chat service at
    all (RagContextOverflowError is raised locally by _select_within_budget
    before any provider call)."""

    def __init__(self, hits: Sequence[SearchHit]) -> None:
        self._hits = tuple(hits)

    async def search(
        self,
        query_text: str,
        query_vector: Sequence[float] | None = None,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top: int,
        principal: Principal,
        vector_k: int = 50,
    ) -> SearchResult:
        return SearchResult(hits=self._hits, mode=mode, vector_k=vector_k)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def seeded_client(client: TestClient) -> TestClient:
    client.app.state.rag_service._retriever = Retriever(
        FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5
    )
    return client


@pytest.fixture
def broken_retriever_client_factory(client: TestClient):
    def _factory(error: Exception) -> TestClient:
        client.app.state.rag_service._retriever = _RaisingRetriever(error)
        return client

    return _factory


@pytest.fixture
def oversized_hit_client(client: TestClient) -> TestClient:
    hit = SearchHit(
        chunk_id="doc-oversized", parent_id="doc-oversized", title="Doc", heading_path="Doc",
        content="x" * (MAX_PROMPT_BYTES + 1), score=1.0,
    )
    client.app.state.rag_service._retriever = Retriever(
        FakeEmbeddingClient(), _OversizedSearchClient([hit]), top=5
    )
    return client


@pytest.fixture
def seeded_broken_generation_client(client: TestClient) -> TestClient:
    client.app.state.rag_service._retriever = Retriever(
        FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5
    )
    client.app.state.rag_service._chat_service = _RaisingChatService(
        UpstreamServiceError("boom")
    )
    return client


@pytest.fixture
def seeded_broken_generation_client_cf(client: TestClient) -> TestClient:
    client.app.state.rag_service._retriever = Retriever(
        FakeEmbeddingClient(), FakeSearchClient([DOC]), top=5
    )
    client.app.state.rag_service._chat_service = _RaisingChatService(
        ContentFilteredError("blocked")
    )
    return client


# --- context-guard coverage (Task 3 gap: has_audit_context/request_duration_ms
# had no direct test until this, their first real consumer) ---


def test_has_audit_context_false_outside_a_request() -> None:
    assert correlation_id_var.get() is None
    assert request_start_var.get() is None
    assert has_audit_context() is False


def test_has_audit_context_true_only_with_both_vars_set() -> None:
    cid_token = correlation_id_var.set("cid-x")
    try:
        # correlation id alone (no request_start) is not enough.
        assert has_audit_context() is False
        start_token = request_start_var.set(0.0)
        try:
            assert has_audit_context() is True
        finally:
            request_start_var.reset(start_token)
    finally:
        correlation_id_var.reset(cid_token)


def test_request_duration_ms_zero_outside_a_request() -> None:
    assert request_start_var.get() is None
    assert request_duration_ms() == 0.0


async def test_direct_service_invocation_emits_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No AuditRequestContext (no TestClient request in flight): the guard
    # must hold even though the pipeline runs to a real terminal.
    service = build_rag_service(Settings(_env_file=None))
    with caplog.at_level(logging.INFO, logger="audit"):
        result = await service.answer("what is x", PRINCIPAL)
    assert result.status in ("answered", "no_answer")
    assert audit_events(caplog) == []


def test_rag_answered_event(seeded_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = seeded_client.post(
            "/api/v1/rag", json={"question": "refund window?"}, headers=IDENTITY
        )
    assert response.status_code == 200 and response.json()["status"] == "answered"
    [event] = audit_events(caplog)
    assert event["status"] == "answered" and event["provider_call_attempted"] is True
    assert event["model_version"] == "fake" and event["prompt_name"] == "rag_answer"
    assert list(event["selected_chunk_ids"]) == [
        s["chunk_id"] for s in response.json()["sources"]
    ]
    assert event["hit_count"] >= len(response.json()["sources"])
    assert event["correlation_id"] == response.json()["correlation_id"]
    assert event["tenant_id"] == "t1" and event["user_id"] == "u1"


def test_rag_no_answer_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    # No override: startup wiring uses an empty fake corpus -> structural
    # no-answer, generation never attempted.
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/rag", json={"question": "nothing matches"}, headers=IDENTITY
        )
    assert response.json()["status"] == "no_answer"
    [event] = audit_events(caplog)
    assert event["status"] == "no_answer" and event["provider_call_attempted"] is False
    assert event["usage"] is None and event["prompt_name"] is None
    assert event["hit_count"] == 0 and event["selected_chunk_ids"] is None


def test_rag_retrieve_failure_event(
    broken_retriever_client_factory, caplog: pytest.LogCaptureFixture
) -> None:
    client = broken_retriever_client_factory(UpstreamServiceError("search down"))
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    assert response.status_code == 502
    [event] = audit_events(caplog)
    assert (event["outcome"], event["failed_stage"]) == ("error", "retrieve")
    assert event["provider_call_attempted"] is False
    assert event["hit_count"] is None and event["selected_chunk_ids"] is None
    assert event["prompt_name"] is None


def test_rag_context_overflow_before_generation_never_attempted(
    oversized_hit_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # assemble_context's byte-budget guard raises RagContextOverflowError
    # locally, before the chat service is ever called -- distinct from
    # test_rag_generation_failure_event's "generation" stage: retrieval did
    # find a hit (hit_count populated), but no provider call happened
    # (attempted=false) and nothing was selected into the prompt
    # (selected_chunk_ids=None, matching the no_answer/retrieve-failure rule
    # that only a "generation" stage failure reports a real selection).
    with caplog.at_level(logging.INFO, logger="audit"):
        response = oversized_hit_client.post(
            "/api/v1/rag", json={"question": "x"}, headers=IDENTITY
        )
    assert response.status_code == 500
    [event] = audit_events(caplog)
    assert event["error_code"] == "rag_context_overflow"
    assert (event["outcome"], event["failed_stage"]) == ("error", "assemble_context")
    assert event["provider_call_attempted"] is False and event["prompt_name"] is None
    assert event["hit_count"] == 1 and event["selected_chunk_ids"] is None


def test_rag_embedding_rejected_event(
    broken_retriever_client_factory, caplog: pytest.LogCaptureFixture
) -> None:
    client = broken_retriever_client_factory(EmbeddingRejectedError("bad input"))
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    assert response.status_code == 500
    [event] = audit_events(caplog)
    assert event["error_code"] == "embedding_rejected"
    assert (event["failed_stage"], event["provider_call_attempted"]) == ("retrieve", False)


def test_rag_generation_failure_event(
    seeded_broken_generation_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = seeded_broken_generation_client.post(
            "/api/v1/rag", json={"question": "refund window?"}, headers=IDENTITY
        )
    assert response.status_code == 502
    [event] = audit_events(caplog)
    assert (event["failed_stage"], event["provider_call_attempted"]) == ("generation", True)
    assert event["prompt_name"] == "rag_answer"
    assert event["hit_count"] is not None and event["selected_chunk_ids"] is not None


def test_rag_content_filtered_is_rejected_with_stage(
    seeded_broken_generation_client_cf: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = seeded_broken_generation_client_cf.post(
            "/api/v1/rag", json={"question": "refund window?"}, headers=IDENTITY
        )
    assert response.status_code == 400
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "content_filtered")
    assert (event["status"], event["failed_stage"]) == ("error", "generation")
    assert event["provider_call_attempted"] is True


def test_non_upstream_exception_from_rag_emits_nothing(
    broken_retriever_client_factory, caplog: pytest.LogCaptureFixture
) -> None:
    # A bug (not a contract case): non-UpstreamError propagates unchanged and
    # emits nothing, same rule as /chat and /chat/stream.
    client = broken_retriever_client_factory(RuntimeError("not an UpstreamError"))
    with (
        caplog.at_level(logging.INFO, logger="audit"),
        pytest.raises(RuntimeError, match="not an UpstreamError"),
    ):
        client.post("/api/v1/rag", json={"question": "x"}, headers=IDENTITY)
    assert audit_events(caplog) == []


def test_rag_never_logs_question_or_chunk_content(
    seeded_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = seeded_client.post(
            "/api/v1/rag", json={"question": "refund window?"}, headers=IDENTITY
        )
    assert response.status_code == 200
    [event] = audit_events(caplog)
    payload = str(event)
    assert "refund window?" not in payload
    assert "alpha refund window" not in payload
