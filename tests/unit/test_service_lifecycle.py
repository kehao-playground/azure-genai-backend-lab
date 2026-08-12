"""Shutdown-path composition (Day 14 review finding 4).

Long-lived clients — the Azure OpenAI ``AsyncOpenAI`` instances behind chat
and embeddings, and the search ``httpx.AsyncClient`` — have no shutdown path
except an explicit ``aclose()``. Each composed service must forward
``aclose()`` to everything it owns exactly once (idempotent), and
``create_app()``'s lifespan must call it on the top-level services it
builds at startup.
"""

import asyncio
import time
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


class _ExplodingEmbeddingClient(_RecordingEmbeddingClient):
    async def aclose(self) -> None:
        await super().aclose()
        raise RuntimeError("embedding close failed")


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


async def test_rag_service_aclose_isolates_a_retriever_failure_from_the_chat_service() -> None:
    """Two composed closers in the app didn't already follow the isolation
    discipline Day 14 review finding 4 established (Day 23 review, third
    wave): RagService.aclose() (fixed in the prior commit) and, one level
    down, Retriever.aclose() itself -- an embedding-client close failure
    must not strand the search client's own httpx pool, or RagService's fix
    only isolates the wrong layer. `_ExplodingEmbeddingClient` explodes in
    the first of Retriever's two closers, so this also exercises exactly
    the case the fix must cover.
    """
    embedding = _ExplodingEmbeddingClient()
    search = _RecordingSearchClient()
    chat = _RecordingChatService()
    rag = RagService(Retriever(embedding, search, top=5), chat)

    with pytest.raises(RuntimeError, match="embedding close failed"):
        await rag.aclose()

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


# ---------------------------------------------------------------------------
# Day 23 review A1: the four closers above used to run inside a nested
# try/finally with no timeout anywhere. These tests exercise the bounded
# replacement (`main._close_with_budget`) via the real lifespan, the same
# way the tests above do — `test_lifespan_isolates_close_failures` already
# covers "a closer raising a non-timeout error still runs the rest of the
# closers and then propagates" against this same bounded implementation
# (its RecordingCloser/ExplodingCloser instances are effectively instant, so
# they exercise the exception path, not the timeout path, of the new code).
# ---------------------------------------------------------------------------


class _HangingCloser:
    """A closer whose aclose() never returns on its own -- it must be cut
    off by the budget, not waited out. The 90s sleep is a stand-in for
    "forever": long enough that no test budget below would ever let it
    finish naturally, short enough that a bug reverting to the old,
    unbounded behaviour fails the test suite in under two minutes rather
    than hanging pytest indefinitely. Critically, `asyncio.sleep` is a real
    suspension point: `asyncio.timeout` can only interrupt a closer at one,
    which is what makes this fixture (unlike `_RecordingCloser`, whose
    aclose() never awaits anything) actually exercise the cancellation
    path.
    """

    async def aclose(self) -> None:
        await asyncio.sleep(90)


def _budget_settings(budget_seconds: float) -> Settings:
    return Settings(_env_file=None, shutdown_cleanup_budget_seconds=budget_seconds)


async def test_shutdown_cleanup_timeout_logs_the_offending_closer_by_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # create_app() (below) calls configure_logging(), whose
    # logging.basicConfig(force=True) replaces every root handler --
    # including caplog's -- with its own stderr StreamHandler each time it
    # runs. That handler is what these tests read back via capsys rather
    # than caplog: capsys is already patching sys.stderr by the time
    # create_app() (re-)binds the handler to it.
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(0.05)
    app.state.principal_resolver = _HangingCloser()
    closed: list[str] = []
    app.state.conversation_service = _RecordingCloser(closed, "conversation")
    app.state.rag_service = _RecordingCloser(closed, "rag")
    app.state.agent_turn_service = _RecordingCloser(closed, "agent")

    async with app.router.lifespan_context(app):
        pass  # must not hang, and must not raise TimeoutError outward

    assert "shutdown cleanup timed out closer=principal resolver" in capsys.readouterr().err


async def test_shutdown_cleanup_budget_bounds_total_wall_time_when_every_closer_hangs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One shared 0.05s budget across four closers that would each hang for
    90s proves two things at once: total wall time is bounded near the
    budget (not the 360s four independent per-closer timeouts would allow),
    and every closer past the first -- including the three left with an
    already-exhausted (zero) remaining budget once the first one's timeout
    alone consumes the whole thing -- is still individually handed to
    asyncio.timeout and gets its own timeout log line, rather than the loop
    stopping after the first timeout or silently dropping the rest of the
    accounting.
    """
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(0.05)
    app.state.principal_resolver = _HangingCloser()
    app.state.conversation_service = _HangingCloser()
    app.state.rag_service = _HangingCloser()
    app.state.agent_turn_service = _HangingCloser()

    start = time.monotonic()
    async with app.router.lifespan_context(app):
        pass
    elapsed = time.monotonic() - start

    # Comfortably above the 0.05s budget (scheduling overhead, four
    # sequential asyncio.timeout cancellations) and comfortably below what
    # any single one of the four 90s hangs would take alone -- the two-
    # order-of-magnitude gap is the point, not the exact figure.
    assert elapsed < 2.0

    stderr = capsys.readouterr().err
    timed_out = [
        line.rsplit("closer=", 1)[1]
        for line in stderr.splitlines()
        if "shutdown cleanup timed out" in line
    ]
    assert timed_out == [
        "principal resolver",
        "conversation service",
        "rag service",
        "agent turn service",
    ]


async def test_shutdown_cleanup_already_exhausted_budget_still_attempts_the_next_closer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A budget spent entirely by the first two closers' hangs leaves the
    last two with zero remaining time. For the two hanging closers this
    must still surface as its own logged timeout, not a silent skip --
    "never tried" and "timed out" are different facts, and the log line is
    the only observable proof.

    The two `_RecordingCloser`s that follow are the honest asterisk on that
    claim: `asyncio.timeout` can only interrupt a closer at a suspension
    point of its own, and `_RecordingCloser.aclose()` never awaits anything,
    so it runs to completion in the same synchronous step even at a zero
    remaining budget -- no timeout, no log line, and `closed` ends up
    non-empty. Every closer with any real I/O (httpx, AsyncOpenAI --
    everything this budget exists for in production) does have a
    suspension point and would be cut off like the two hanging closers
    above; this is a property of the test double, not of the mechanism.
    """
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(0.03)
    app.state.principal_resolver = _HangingCloser()
    app.state.conversation_service = _HangingCloser()
    closed: list[str] = []
    app.state.rag_service = _RecordingCloser(closed, "rag")
    app.state.agent_turn_service = _RecordingCloser(closed, "agent")

    async with app.router.lifespan_context(app):
        pass

    stderr = capsys.readouterr().err
    assert "shutdown cleanup timed out closer=principal resolver" in stderr
    assert "shutdown cleanup timed out closer=conversation service" in stderr
    assert closed == ["rag", "agent"]


async def test_shutdown_cleanup_first_exception_wins_and_later_ones_are_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The nested try/finally chain this replaces was last-wins: an
    exception from a later closer replaced an earlier in-flight one as the
    propagated exception, keeping the earlier one only as `__context__`
    (verified empirically against the old code: closers 1 and 3 both
    raising propagated closer 3's RuntimeError with closer 1's chained into
    `__context__`, not the other way around).

    `_close_with_budget` is deliberately first-wins instead. That is a
    change, not a preservation, and it drops information the old chain
    didn't: without `__context__` linking them, a later real failure needs
    its own log line or it vanishes with no trace at all. This pins both
    halves: closer 1's exception (not closer 3's) is what propagates, and
    closer 3's failure still shows up in the log even though it lost.
    """
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(8.0)
    closed: list[str] = []
    app.state.principal_resolver = _ExplodingCloser(closed, "principal")
    app.state.conversation_service = _RecordingCloser(closed, "conversation")
    app.state.rag_service = _ExplodingCloser(closed, "rag")
    app.state.agent_turn_service = _RecordingCloser(closed, "agent")

    with pytest.raises(RuntimeError, match="^principal close failed$"):
        async with app.router.lifespan_context(app):
            pass

    # All four still ran, in order, despite the first failure.
    assert closed == ["principal", "conversation", "rag", "agent"]
    # The propagated exception is closer 1's ("principal"), not closer 3's
    # ("rag") -- first-wins, confirmed via pytest.raises' exact match above,
    # not just this log check. Closer 3's own failure is not silently
    # dropped just because it lost the re-raise; the class name (RuntimeError)
    # is present alongside the message, not relied on alone (Day 23 review,
    # third wave N4).
    assert (
        "shutdown cleanup closer=rag service raised RuntimeError: rag close failed"
        in capsys.readouterr().err
    )


class _RaisingCloser:
    """A closer whose aclose() always raises the given exception, with no
    internal await -- see the corresponding note on `_HangingCloser`: a
    synchronous raise is enough for `asyncio.timeout`'s `__aexit__` to see
    the exception is unrelated to its own (not-yet-fired) deadline callback,
    so `cm.expired()` reads False without needing a suspension point.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def aclose(self) -> None:
        raise self._exc


async def test_shutdown_cleanup_closers_own_timeout_error_propagates_and_is_not_swallowed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The non-expired-TimeoutError branch: a closer's own TimeoutError
    (e.g. a socket/SSL teardown), not the budget expiring. This is the
    entire reason _close_with_budget uses asyncio.timeout + cm.expired()
    instead of asyncio.wait_for (Important 2, Day 23 review second wave),
    and its log line was touched again for the class name (N4, third wave)
    -- reworked twice with no automated coverage until now.

    A generous budget (5.0s, far larger than this test needs) rules out the
    budget itself expiring as an alternative explanation for whatever
    happens; the closer raises immediately, well inside it.
    """
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(5.0)
    closed: list[str] = []
    app.state.principal_resolver = _RaisingCloser(TimeoutError("ssl teardown timed out"))
    app.state.conversation_service = _RecordingCloser(closed, "conversation")
    app.state.rag_service = _RecordingCloser(closed, "rag")
    app.state.agent_turn_service = _RecordingCloser(closed, "agent")

    # Not swallowed as a logged-and-ignored budget expiry: it must actually
    # propagate out of the lifespan.
    with pytest.raises(TimeoutError, match="^ssl teardown timed out$"):
        async with app.router.lifespan_context(app):
            pass

    # The remaining three closers still ran despite the first one raising.
    assert closed == ["conversation", "rag", "agent"]
    stderr = capsys.readouterr().err
    # Not logged as a budget timeout (that wording is reserved for a real
    # expiry -- see the other shutdown_cleanup_timeout_logs_* tests)...
    assert "shutdown cleanup timed out closer=principal resolver" not in stderr
    # ...and the log line that *is* emitted names both the closer and the
    # exception class.
    assert "shutdown cleanup closer=principal resolver raised TimeoutError" in stderr


async def test_shutdown_cleanup_closers_own_exception_with_empty_str_still_logs_diagnostic_content(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """N4's actual motivation: some exceptions stringify to nothing at all
    -- a bare ConnectionResetError() has no message -- so the class name in
    the log line is not decoration; without it this line would carry zero
    diagnostic content.
    """
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(5.0)
    closed: list[str] = []
    app.state.principal_resolver = _RaisingCloser(ConnectionResetError())
    app.state.conversation_service = _RecordingCloser(closed, "conversation")
    app.state.rag_service = _RecordingCloser(closed, "rag")
    app.state.agent_turn_service = _RecordingCloser(closed, "agent")

    with pytest.raises(ConnectionResetError):
        async with app.router.lifespan_context(app):
            pass

    assert closed == ["conversation", "rag", "agent"]
    assert (
        "shutdown cleanup closer=principal resolver raised ConnectionResetError: "
        in capsys.readouterr().err
    )


# ---------------------------------------------------------------------------
# Day 23 review F2: external cancellation. `asyncio.CancelledError` derives
# from BaseException, so neither `except TimeoutError` nor `except Exception`
# in _close_with_budget sees it -- before the fix, cancelling the cleanup
# task while a closer was suspended skipped every closer after it. These
# tests drive _close_with_budget as its own task (rather than through the
# lifespan) because `task.cancel()` needs a task handle to aim at.
# ---------------------------------------------------------------------------


class _SignallingHangingCloser:
    """Announces that it has started, then hangs at a real suspension point
    -- so the test can cancel at a deterministic moment (closer 1 suspended)
    instead of racing the event loop.
    """

    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def aclose(self) -> None:
        self._started.set()
        await asyncio.sleep(90)


class _AwaitingRecordingCloser(_RecordingCloser):
    """Suspends before recording.

    `_RecordingCloser` never awaits, so it would complete in the same
    synchronous step even inside a cancelled task -- which would leave "the
    remaining closers still ran" proven only for closers that do nothing.
    This one yields to the event loop first: it can only record if the task
    is genuinely still runnable after the cancellation was absorbed.
    """

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        await super().aclose()


def _cancellation_app(started: asyncio.Event, closed: list[str]) -> FastAPI:
    from azgenai_lab.main import create_app

    app = create_app()
    app.state.settings = _budget_settings(8.0)
    app.state.principal_resolver = _SignallingHangingCloser(started)
    app.state.conversation_service = _AwaitingRecordingCloser(closed, "conversation")
    app.state.rag_service = _AwaitingRecordingCloser(closed, "rag")
    app.state.agent_turn_service = _AwaitingRecordingCloser(closed, "agent")
    return app


async def test_shutdown_cleanup_external_cancellation_still_attempts_every_closer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from azgenai_lab.main import _close_with_budget

    started = asyncio.Event()
    closed: list[str] = []
    app = _cancellation_app(started, closed)

    task = asyncio.create_task(_close_with_budget(app, 8.0))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation is delayed, never swallowed: the task still ends cancelled.
    assert task.cancelled()
    # ...and the three closers after the cancelled one each got their turn,
    # each of them across a real suspension point.
    assert closed == ["conversation", "rag", "agent"]
    assert "shutdown cleanup cancelled during closer=principal resolver" in capsys.readouterr().err


async def test_shutdown_cleanup_cancellation_wins_over_a_closer_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cancellation outranks first-wins.

    A closer that raises *after* the cancellation was absorbed must not
    convert a cancelled task into a merely-failed one -- callers that
    cancelled this task are entitled to see CancelledError, and asyncio
    itself treats "returned an ordinary exception after being cancelled" as
    a task that ignored its cancellation. The losing exception is still
    logged, exactly as in the first-wins test above.
    """
    from azgenai_lab.main import _close_with_budget

    started = asyncio.Event()
    closed: list[str] = []
    app = _cancellation_app(started, closed)
    app.state.rag_service = _RaisingCloser(RuntimeError("rag close failed"))

    task = asyncio.create_task(_close_with_budget(app, 8.0))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert closed == ["conversation", "agent"]
    assert (
        "shutdown cleanup closer=rag service raised RuntimeError: rag close failed"
        in capsys.readouterr().err
    )
