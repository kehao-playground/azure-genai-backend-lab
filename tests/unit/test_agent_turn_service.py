"""AgentTurnService: load → scope → budget → run → commit (Day 18)."""

import asyncio

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import StorageError, UpstreamServiceError
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.models.chat import Message, TokenUsage
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_framework import (
    AgentHistoryTurn,
    AgentRunError,
    AgentRunResult,
)
from azgenai_lab.services.agent_turn import (
    AgentTurnService,
    build_agent_turn_service,
    project_history,
)
from azgenai_lab.services.conversation import (
    ConversationNotFoundError,
    TokenBudgetExceededError,
)
from azgenai_lab.services.conversation_store import InMemoryConversationStore

P = Principal(tenant_id="t1", group_ids=("g1",))
P_OTHER = Principal(tenant_id="t1", group_ids=("g2",))

USAGE = TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30, reasoning_tokens=0)


def _result(
    answer: str = "done",
    stop: str = "natural",
    usage: TokenUsage | None = USAGE,
) -> AgentRunResult:
    return AgentRunResult(
        answer=answer,
        model_call_count=2,
        tool_round_count=1,
        tool_call_count=1,
        refused_call_count=0,
        stop_reason=stop,  # type: ignore[arg-type]
        limit_reasons=frozenset(),
        tool_calls=(),
        usage=usage,
        per_round=None,
    )


class _StubAgent:
    def __init__(self, result: AgentRunResult | Exception = None) -> None:  # type: ignore[assignment]
        self.result = result if result is not None else _result()
        self.calls: list[tuple[str, tuple[AgentHistoryTurn, ...], Principal]] = []

    async def run(self, task, history, *, principal):  # type: ignore[no-untyped-def]
        self.calls.append((task, history, principal))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None


def _service(
    agent: _StubAgent | None = None,
    budget: int | None = None,
    store: InMemoryConversationStore | None = None,
) -> tuple[AgentTurnService, _StubAgent, InMemoryConversationStore]:
    agent = agent or _StubAgent()
    store = store or InMemoryConversationStore()
    return AgentTurnService(agent, store, token_budget=budget), agent, store


async def test_new_conversation_commits_turn_scope_and_usage() -> None:
    service, agent, store = _service()
    cid, result = await service.run_turn("check config", None, principal=P)
    assert result.status == "completed"
    stored = await store.get("t1", cid)
    assert stored is not None
    assert stored.authorization_group_ids == ("g1",)
    assert [m.role for m in stored.messages] == ["user", "assistant"]
    assert stored.total_tokens == 30


async def test_history_is_projected_from_messages() -> None:
    service, agent, store = _service()
    cid, _ = await service.run_turn("first task", None, principal=P)
    await service.run_turn("second task", cid, principal=P)
    _, history, _ = agent.calls[1]
    assert history == (
        AgentHistoryTurn(role="user", text="first task"),
        AgentHistoryTurn(role="assistant", text="done"),
    )


async def test_unknown_and_scope_mismatch_are_not_found() -> None:
    service, _, _ = _service()
    with pytest.raises(ConversationNotFoundError):
        await service.run_turn("task", "never-issued", principal=P)
    service2, _, _ = _service()
    cid, _ = await service2.run_turn("task", None, principal=P)
    with pytest.raises(ConversationNotFoundError):
        await service2.run_turn("task", cid, principal=P_OTHER)


async def test_budget_gate_rejects_before_run() -> None:
    agent = _StubAgent()
    service, _, _ = _service(agent=agent, budget=10)
    cid, _ = await service.run_turn("task one", None, principal=P)  # spends 30 > 10
    with pytest.raises(TokenBudgetExceededError):
        await service.run_turn("task two", cid, principal=P)
    assert len(agent.calls) == 1  # the gate fired before the second run


async def test_run_failure_commits_nothing_and_maps_upstream() -> None:
    agent = _StubAgent(AgentRunError("boom", usage=None))
    service, _, store = _service(agent=agent)
    with pytest.raises(UpstreamServiceError):
        await service.run_turn("task", None, principal=P)
    # First turn never came into existence.
    assert len(agent.calls) == 1


async def test_natural_empty_answer_is_upstream_failure_no_commit() -> None:
    agent = _StubAgent(_result(answer="", stop="natural"))
    service, _, store = _service(agent=agent)
    with pytest.raises(UpstreamServiceError):
        await service.run_turn("task", None, principal=P)


async def test_limit_empty_answer_commits_user_only_shape() -> None:
    agent = _StubAgent(_result(answer="", stop="function_call_limit"))
    service, _, store = _service(agent=agent)
    cid, result = await service.run_turn("task", None, principal=P)
    assert result.status == "incomplete"
    assert result.incomplete_reason == "tool_call_limit"
    stored = await store.get("t1", cid)
    assert stored is not None
    assert [m.role for m in stored.messages] == ["user"]
    assert stored.total_tokens == 30


async def test_iteration_limit_maps_to_incomplete_reason() -> None:
    agent = _StubAgent(_result(answer="partial", stop="iteration_limit"))
    service, _, _ = _service(agent=agent)
    _, result = await service.run_turn("task", None, principal=P)
    assert result.status == "incomplete"
    assert result.incomplete_reason == "iteration_limit"


@pytest.mark.parametrize(
    "failure",
    ["unknown_conversation", "budget", "run_error", "commit_error"],
)
async def test_lock_released_on_every_exit(failure: str) -> None:
    if failure == "unknown_conversation":
        service, _, _ = _service()
        with pytest.raises(ConversationNotFoundError):
            await service.run_turn("task", "never-issued", principal=P)
        key = ("t1", "never-issued")
    elif failure == "budget":
        service, _, _ = _service(budget=10)
        cid, _ = await service.run_turn("task one", None, principal=P)
        with pytest.raises(TokenBudgetExceededError):
            await service.run_turn("task two", cid, principal=P)
        key = ("t1", cid)
    elif failure == "run_error":
        service, _, _ = _service(agent=_StubAgent(AgentRunError("boom")))
        with pytest.raises(UpstreamServiceError):
            await service.run_turn("task", None, principal=P)
        key = None  # id was freshly issued; registry emptiness is the check
    else:
        class _BrokenStore(InMemoryConversationStore):
            async def append(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("disk gone")

        service, _, _ = _service(store=_BrokenStore())
        with pytest.raises(StorageError):
            await service.run_turn("task", None, principal=P)
        key = None
    if key is not None:
        assert service._locks.holders(key) == 0
    assert len(service._locks) == 0  # reference-counted entries all reclaimed


async def test_cancellation_releases_lock() -> None:
    class _HangingAgent(_StubAgent):
        async def run(self, task, history, *, principal):  # type: ignore[no-untyped-def]
            await asyncio.Event().wait()

    service, _, _ = _service(agent=_HangingAgent())
    run = asyncio.ensure_future(service.run_turn("task", None, principal=P))
    await asyncio.sleep(0.01)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert len(service._locks) == 0


async def test_chat_and_agent_serialize_on_the_same_conversation() -> None:
    """Shared locks: an in-flight agent turn blocks a second turn on the same
    conversation instead of letting both read the same revision."""
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowAgent(_StubAgent):
        async def run(self, task, history, *, principal):  # type: ignore[no-untyped-def]
            started.set()
            await release.wait()
            return _result()

    locks: KeyedLock[tuple[str, str]] = KeyedLock()
    store = InMemoryConversationStore()
    service = AgentTurnService(_SlowAgent(), store, locks=locks)
    release.set()  # let the seed turn through; cleared again below
    cid, _ = await service.run_turn("seed", None, principal=P)
    started.clear()  # the seed turn set it too; re-arm for the slow turn
    release.clear()
    first = asyncio.ensure_future(service.run_turn("slow", cid, principal=P))
    await started.wait()
    assert locks.is_held(("t1", cid))
    second = asyncio.ensure_future(service.run_turn("queued", cid, principal=P))
    await asyncio.sleep(0.01)
    assert locks.holders(("t1", cid)) == 2  # holder + waiter, not two holders
    release.set()
    await first
    await second
    loaded = await store.get("t1", cid)
    assert loaded is not None
    assert loaded.revision == 3


async def test_aclose_is_idempotent_even_after_a_failing_close() -> None:
    closes: list[str] = []

    class _Adapter(_StubAgent):
        async def aclose(self) -> None:
            closes.append("close")
            raise RuntimeError("close failed")

    service = AgentTurnService(_Adapter(), InMemoryConversationStore())
    with pytest.raises(RuntimeError):
        await service.aclose()
    await service.aclose()  # second call must NOT re-delegate
    assert closes == ["close"]


async def test_stage_logs_on_success(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    service, _, _ = _service()
    with caplog.at_level(logging.INFO):
        await service.run_turn("task", None, principal=P)
    stages = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("agent_turn_stage")
    ]
    assert [s.split()[1] for s in stages] == ["stage=load", "stage=run", "stage=commit"]
    assert all("outcome=ok" in s and "duration_ms=" in s for s in stages)


async def test_stage_log_on_run_failure_no_commit_stage(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    service, _, _ = _service(agent=_StubAgent(AgentRunError("boom")))
    with caplog.at_level(logging.INFO), pytest.raises(UpstreamServiceError):
        await service.run_turn("task", None, principal=P)
    stages = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("agent_turn_stage")
    ]
    assert any(
        "stage=run outcome=error" in s and "exception=UpstreamServiceError" in s
        for s in stages
    )
    assert not any("stage=commit" in s for s in stages)


def test_project_history_skips_non_conversation_roles() -> None:
    turns = project_history(
        [
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
            Message(role="system", content="s"),
        ]
    )
    assert turns == (
        AgentHistoryTurn(role="user", text="q"),
        AgentHistoryTurn(role="assistant", text="a"),
    )


def test_build_agent_turn_service_wires_injected_store_locks_and_budget() -> None:
    settings = Settings(_env_file=None, use_fake_llm=True, use_fake_search=True)
    store = InMemoryConversationStore()
    locks: KeyedLock[tuple[str, str]] = KeyedLock()
    service = build_agent_turn_service(settings, store=store, locks=locks)
    assert service._store is store  # shared, never rebuilt (Task 2 contract)
    assert service._locks is locks
    assert service._token_budget == settings.conversation_token_budget
