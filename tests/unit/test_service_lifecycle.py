"""Shutdown-path composition (Day 14 review finding 4).

Long-lived clients — the Azure OpenAI ``AsyncOpenAI`` instances behind chat
and embeddings, and the search ``httpx.AsyncClient`` — have no shutdown path
except an explicit ``aclose()``. Each composed service must forward
``aclose()`` to everything it owns exactly once (idempotent), and
``create_app()``'s lifespan must call it on the two top-level services it
builds at startup.
"""

from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.agent_turn import AgentTurnService
from azgenai_lab.services.azure_openai import ChatResult, ChatStreamEvent, FakeChatService
from azgenai_lab.services.azure_search import AzureSearchClient, FakeSearchClient
from azgenai_lab.services.conversation import ConversationChatService
from azgenai_lab.services.conversation_store import InMemoryConversationStore
from azgenai_lab.services.embeddings import FakeEmbeddingClient
from azgenai_lab.services.rag import RagService
from azgenai_lab.services.retrieval import Retriever


class _RecordingChatService(FakeChatService):
    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _RecordingEmbeddingClient(FakeEmbeddingClient):
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _RecordingSearchClient(FakeSearchClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


async def test_retriever_aclose_closes_embedding_and_search_clients() -> None:
    embedding = _RecordingEmbeddingClient()
    search = _RecordingSearchClient()
    retriever = Retriever(embedding, search, top=5)

    await retriever.aclose()

    assert embedding.close_count == 1
    assert search.close_count == 1


async def test_rag_service_aclose_closes_retriever_and_chat_service() -> None:
    embedding = _RecordingEmbeddingClient()
    search = _RecordingSearchClient()
    chat = _RecordingChatService()
    rag = RagService(Retriever(embedding, search, top=5), chat)

    await rag.aclose()

    assert embedding.close_count == 1
    assert search.close_count == 1
    assert chat.close_count == 1


async def test_conversation_chat_service_aclose_closes_chat_service() -> None:
    chat = _RecordingChatService()
    service = ConversationChatService(chat, InMemoryConversationStore())

    await service.aclose()

    assert chat.close_count == 1


async def test_azure_search_client_double_aclose_is_idempotent() -> None:
    settings = Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )
    # No `client=` injected: this object owns its httpx.AsyncClient and is
    # the one responsible for closing it (an injected client, as used
    # elsewhere in this suite, is deliberately left open by aclose()).
    client = AzureSearchClient(settings)

    await client.aclose()
    await client.aclose()  # must not raise on the second call

    assert client._client.is_closed


VECTOR = [0.1] * EMBEDDING_DIMENSIONS


class _NoOpChatService:
    """A ChatService whose aclose() is observable but whose other methods are
    never expected to be called by entering/exiting a TestClient."""

    def __init__(self) -> None:
        self.close_count = 0

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        raise AssertionError("must not be called")

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        raise AssertionError("must not be called")

    async def aclose(self) -> None:
        self.close_count += 1


def test_create_app_lifespan_closes_composed_services_on_shutdown() -> None:
    from azgenai_lab.main import create_app

    app = create_app()
    # The composed services built inside create_app() reach real Azure
    # adapters only under non-default settings; swapping app.state here
    # (before the lifespan runs, not via dependency_overrides, which bypasses
    # state entirely) proves the lifespan itself calls aclose() on whatever
    # ends up in app.state, without needing live credentials.
    conversation_chat = _NoOpChatService()
    app.state.conversation_service = ConversationChatService(
        conversation_chat, InMemoryConversationStore()
    )
    rag_chat = _NoOpChatService()
    rag = RagService(
        Retriever(_RecordingEmbeddingClient(), _RecordingSearchClient(), top=5), rag_chat
    )
    app.state.rag_service = rag

    with TestClient(app):
        pass  # lifespan startup/shutdown both run across this block

    assert conversation_chat.close_count == 1
    assert rag_chat.close_count == 1


async def test_lifespan_isolates_close_failures() -> None:
    from azgenai_lab.main import create_app

    app = create_app()
    closed: list[str] = []

    class _Exploding:
        async def aclose(self) -> None:
            closed.append("conversation")
            raise RuntimeError("close failed")

    class _Recording:
        def __init__(self, name: str) -> None:
            self._name = name

        async def aclose(self) -> None:
            closed.append(self._name)

    app.state.conversation_service = _Exploding()
    app.state.rag_service = _Recording("rag")
    app.state.agent_turn_service = _Recording("agent")
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert closed == ["conversation", "rag", "agent"]


async def test_agent_turn_service_aclose_delegates_exactly_once() -> None:
    calls: list[str] = []

    class _Adapter:
        async def run(self, task, history, *, principal):  # type: ignore[no-untyped-def]
            raise AssertionError("not used")

        async def aclose(self) -> None:
            calls.append("close")

    service = AgentTurnService(_Adapter(), InMemoryConversationStore())
    await service.aclose()
    await service.aclose()
    # The wrapper's own _closed guard (Task 8) — not the adapter's — makes
    # the second call a no-op before it ever reaches the delegate.
    assert calls == ["close"]
