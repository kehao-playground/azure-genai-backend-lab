"""Shutdown-path composition (Day 14 review finding 4).

Long-lived clients — the Azure OpenAI ``AsyncOpenAI`` instances behind chat
and embeddings, and the search ``httpx.AsyncClient`` — have no shutdown path
except an explicit ``aclose()``. Each composed service must forward
``aclose()`` to everything it owns exactly once (idempotent), and
``create_app()``'s lifespan must call it on the top-level services it
builds at startup.
"""

from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from azgenai_lab.api.principal import HeaderPrincipalResolver, UninitializedResolver
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


class _RecordingCloser:
    def __init__(self, closed: list[str], name: str) -> None:
        self._closed = closed
        self._name = name

    async def aclose(self) -> None:
        self._closed.append(self._name)


class _ExplodingCloser(_RecordingCloser):
    async def aclose(self) -> None:
        await super().aclose()
        raise RuntimeError(f"{self._name} close failed")


# The shutdown order, and the app.state attribute each position lives on.
_CLOSE_ORDER = ["principal", "conversation", "rag", "agent"]
_STATE_ATTRIBUTES = {
    "principal": "principal_resolver",
    "conversation": "conversation_service",
    "rag": "rag_service",
    "agent": "agent_turn_service",
}


@pytest.mark.parametrize("failing", _CLOSE_ORDER)
async def test_lifespan_isolates_close_failures(failing: str) -> None:
    """Every position, not just one.

    Exercising only the conversation slot would leave the other three
    untested: hoisting the resolver close out of the nested chain, say, still
    closes everything on the happy path and only strands the rest when the
    resolver itself fails — which nothing would have noticed.
    """
    from azgenai_lab.main import create_app

    app = create_app()
    closed: list[str] = []
    for name in _CLOSE_ORDER:
        closer_type = _ExplodingCloser if name == failing else _RecordingCloser
        setattr(app.state, _STATE_ATTRIBUTES[name], closer_type(closed, name))

    with pytest.raises(RuntimeError, match=f"{failing} close failed"):
        async with app.router.lifespan_context(app):
            pass

    # All four, in order, whichever one raised: an isolated close failure
    # propagates but must not strand the positions after it.
    assert closed == _CLOSE_ORDER


# ---------------------------------------------------------------------------
# Day 19: the two-stage principal-resolver composition.
#
# create_app() installs a resolver synchronously (so the bare-TestClient entry
# points in tests/bdd/environment.py and tests/unit/test_agent_api.py keep
# working without a lifespan); the lifespan only *replaces* it, and only in
# Entra mode.
# ---------------------------------------------------------------------------


def _entra_settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="entra",
        entra_tenant_id="11111111-1111-1111-1111-111111111111",
        entra_audience="22222222-2222-2222-2222-222222222222",
        entra_required_scope="access_as_user",
    )


def _entra_mode_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """An Entra-mode app whose lifespan has not run.

    The mode is injected by patching the `get_settings` name `main` resolved
    at import, not by setting AUTH_MODE in the environment: `get_settings` is
    lru_cached process-wide, so an env-based approach would have to clear the
    cache and could leave Entra-mode settings behind for the rest of the
    session.
    """
    from azgenai_lab import main as main_module

    monkeypatch.setattr(main_module, "get_settings", _entra_settings)
    return main_module.create_app()


def test_headers_mode_installs_a_working_resolver_before_any_lifespan() -> None:
    from azgenai_lab.main import create_app

    app = create_app()

    assert isinstance(app.state.principal_resolver, HeaderPrincipalResolver)


def test_entra_mode_installs_the_sentinel_before_any_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _entra_mode_app(monkeypatch)

    assert isinstance(app.state.principal_resolver, UninitializedResolver)


async def test_entra_lifespan_replaces_the_sentinel_and_closes_the_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from azgenai_lab import main as main_module

    app = _entra_mode_app(monkeypatch)
    closed: list[str] = []
    built = _RecordingCloser(closed, "entra-resolver")
    seen_settings: list[Settings] = []

    async def fake_build(settings: Settings) -> object:
        seen_settings.append(settings)
        return built

    monkeypatch.setattr(main_module, "build_entra_resolver", fake_build)

    async with app.router.lifespan_context(app):
        # Not merely "some Entra resolver": the exact object the factory
        # returned, so a lifespan that built one and installed another
        # cannot pass.
        assert app.state.principal_resolver is built

    assert [s.auth_mode for s in seen_settings] == ["entra"]
    # Closed once, and it is the *replacement* that gets closed — not the
    # sentinel it displaced.
    assert closed == ["entra-resolver"]


async def test_headers_mode_lifespan_never_builds_an_entra_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from azgenai_lab import main as main_module

    async def exploding_build(settings: Settings) -> object:
        raise AssertionError("headers mode must do no auth startup work")

    monkeypatch.setattr(main_module, "build_entra_resolver", exploding_build)

    app = main_module.create_app()
    installed = app.state.principal_resolver
    async with app.router.lifespan_context(app):
        assert app.state.principal_resolver is installed


async def test_entra_startup_failure_still_closes_the_prebuilt_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The `finally` has to wrap the startup call, not just the `yield`:
    # otherwise a failed auth startup strands the conversation, RAG and agent
    # clients that create_app() already opened.
    from azgenai_lab import main as main_module

    app = _entra_mode_app(monkeypatch)
    closed: list[str] = []

    async def failing_build(settings: Settings) -> object:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(main_module, "build_entra_resolver", failing_build)
    app.state.conversation_service = _RecordingCloser(closed, "conversation")
    app.state.rag_service = _RecordingCloser(closed, "rag")
    app.state.agent_turn_service = _RecordingCloser(closed, "agent")

    with pytest.raises(RuntimeError, match="discovery failed"):
        async with app.router.lifespan_context(app):
            raise AssertionError("startup must not reach the request phase")

    # The sentinel is still what is installed on this path, and closing it is
    # a no-op — the point is that the other three were not skipped.
    assert isinstance(app.state.principal_resolver, UninitializedResolver)
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
