"""Shared fixtures/helpers for the Day 22 audit test files."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from azgenai_lab.core.audit import AuditUsage
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search import SearchHit, SearchMode, SearchResult
from azgenai_lab.services.azure_search import FakeSearchClient
from azgenai_lab.services.conversation_store import InMemoryConversationStore
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import MAX_PROMPT_BYTES
from azgenai_lab.services.retrieval import Retriever

IDENTITY = {"X-Tenant-Id": "t1", "X-User-Id": "u1"}
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


def audit_events(caplog: "pytest.LogCaptureFixture") -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "audit"]


def base_fields(**overrides) -> dict:
    fields = dict(
        occurred_at=NOW, correlation_id="cid-1", duration_ms=12.5,
        tenant_id="t1", user_id="u1", conversation_id="c1", streaming=False,
        committed=True, provider_call_attempted=True,
        prompt_name="default_chat", prompt_version=1, prompt_sha256="ab" * 32,
        deployment="fake", model_version="fake",
        usage=AuditUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        status="completed",
    )
    fields.update(overrides)
    return fields


def unattempted_fields(**overrides) -> dict:
    fields = base_fields(
        provider_call_attempted=False, prompt_name=None, prompt_version=None,
        prompt_sha256=None, deployment=None, model_version=None, usage=None,
        status=None, committed=False,
    )
    fields.update(overrides)
    return fields


# --- Cross-endpoint test doubles (Task 6-10) -------------------------------
#
# Extracted here (Task 13) so the observed-trace suite (test_audit_error_
# codes.py) can drive the exact same paths their owning task's tests already
# exercise, instead of duplicating them. Each owning file
# (test_audit_chat_api.py / test_audit_rag.py / test_audit_agent.py) keeps
# its original fixture names as thin wrappers over these.


class RaisingChatService:
    """Substitutes only the LLM boundary; the orchestrator around it stays
    real. Shared by /chat and /chat/stream (raising_client_factory) and
    /rag's generation step (with_broken_generation) -- all three call sites
    consume the same narrow complete()/open_stream()/aclose() shape. Takes
    any Exception, not just UpstreamError, so the same stub also drives a
    non-UpstreamError no-event proof."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def complete(self, items: Sequence[object]) -> object:
        raise self._error

    async def open_stream(self, items: Sequence[object]) -> object:
        raise self._error

    async def aclose(self) -> None:
        return None


class BrokenGetStore(InMemoryConversationStore):
    async def get(self, tenant_id: str, conversation_id: str) -> object:
        raise RuntimeError("store unavailable")


class BrokenAppendStore(InMemoryConversationStore):
    async def append(
        self,
        tenant_id: str,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
        expected_revision: int,
        usage_tokens: int,
        *,
        first_turn_authorization_group_ids: tuple[str, ...] | None,
    ) -> None:
        raise RuntimeError("disk on fire")


def with_raising_chat_service(client: TestClient, error: Exception) -> TestClient:
    client.app.state.conversation_service._chat_service = RaisingChatService(error)
    return client


def with_tiny_chat_budget(client: TestClient, budget: int = 1) -> TestClient:
    client.app.state.conversation_service._token_budget = budget
    return client


def with_broken_chat_get_store(client: TestClient) -> TestClient:
    client.app.state.conversation_service._store = BrokenGetStore()
    return client


def with_broken_chat_append_store(client: TestClient) -> TestClient:
    client.app.state.conversation_service._store = BrokenAppendStore()
    return client


def with_tiny_agent_budget(client: TestClient, budget: int = 1) -> TestClient:
    client.app.state.agent_turn_service._token_budget = budget
    return client


def with_broken_agent_get_store(client: TestClient) -> TestClient:
    client.app.state.agent_turn_service._store = BrokenGetStore()
    return client


def with_broken_agent_append_store(client: TestClient) -> TestClient:
    client.app.state.agent_turn_service._store = BrokenAppendStore()
    return client


class RaisingAgentService:
    """Substitutes only the loop boundary -- mirrors RaisingChatService."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run(self, task: str, history: object, *, principal: Principal) -> object:
        raise self._error

    async def aclose(self) -> None:
        return None


def with_raising_agent_service(client: TestClient, error: Exception) -> TestClient:
    client.app.state.agent_turn_service._agent_service = RaisingAgentService(error)
    return client


# --- RAG retrieval/generation doubles ---

RAG_DOC = {
    "chunk_id": "doc-a-0000",
    "parent_id": "doc-a",
    "title": "Doc A",
    "heading_path": "Doc A > Intro",
    "content": "alpha refund window",
    "tenant_id": "t1",
    "allowed_groups": [],
}


class RaisingRetriever:
    """Substitutes the whole retrieve() step -- embed vs. search are both
    "retrieve" stage as far as RagQueryEvent.failed_stage is concerned."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def retrieve(self, question: str, principal: Principal) -> object:
        raise self._error

    async def aclose(self) -> None:
        pass


class OversizedSearchClient:
    """Duck-typed SearchClient returning a fixed hostile hit set -- drives
    the assemble_context-stage failure, which never reaches the chat service
    at all (RagContextOverflowError is raised locally by
    _select_within_budget before any provider call)."""

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


def with_raising_retriever(client: TestClient, error: Exception) -> TestClient:
    client.app.state.rag_service._retriever = RaisingRetriever(error)
    return client


def with_seeded_retriever(client: TestClient) -> TestClient:
    client.app.state.rag_service._retriever = Retriever(
        FakeEmbeddingClient(), FakeSearchClient([RAG_DOC]), top=5
    )
    return client


def with_oversized_hit(client: TestClient) -> TestClient:
    hit = SearchHit(
        chunk_id="doc-oversized", parent_id="doc-oversized", title="Doc", heading_path="Doc",
        content="x" * (MAX_PROMPT_BYTES + 1), score=1.0,
    )
    client.app.state.rag_service._retriever = Retriever(
        FakeEmbeddingClient(), OversizedSearchClient([hit]), top=5
    )
    return client


def with_broken_generation(client: TestClient, error: Exception) -> TestClient:
    with_seeded_retriever(client)
    client.app.state.rag_service._chat_service = RaisingChatService(error)
    return client
