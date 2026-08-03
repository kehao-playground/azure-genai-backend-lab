import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_framework import (
    AgentHistoryTurn,
    AgentTaskTooLargeError,
    FakeAgentService,
    build_agent_service,
)
from azgenai_lab.services.agent_tools import (
    AgentToolDeps,
    bind_principal_tools,
    build_agent_tool_deps,
)
from azgenai_lab.services.conversation_store import InMemoryConversationStore

OPS = Principal(tenant_id="opsdemo", user_id="u1", group_ids=())


def _settings() -> Settings:
    """Settings isolated from the repo-root `.env`.

    `conftest.py` pins only the three fake-adapter flags, so every other
    field is still ambient: a developer's untracked `.env` would otherwise
    reach these assertions, and no fresh clone or CI checkout would
    reproduce the failure (the same hazard `tests/unit/test_config.py`
    avoids with `_env_file=None`).
    """
    return Settings(_env_file=None)


def _deps() -> AgentToolDeps:
    return build_agent_tool_deps(
        _settings(), conversation_store=InMemoryConversationStore(), token_budget=400
    )


async def test_fake_agent_actually_invokes_injected_tools() -> None:
    deps = _deps()
    service = FakeAgentService(deps)
    try:
        result = await service.run("what is the token budget?", (), principal=OPS)
    finally:
        await service.aclose()
    # wiring proof (Day 8 fake-marker philosophy): tool outputs are embedded
    assert result.answer.startswith("[fake-agent]")
    assert "conversation_token_budget" in result.answer  # from get_runtime_config
    assert '"hits"' in result.answer  # from search_docs
    assert result.stop_reason == "natural" and result.limit_reasons == frozenset()
    assert result.model_call_count == 2 and result.tool_round_count == 1
    assert result.tool_call_count == len(bind_principal_tools(deps, OPS))
    assert all(c.executed for c in result.tool_calls)
    assert result.usage is not None  # deterministic fake usage, wired end to end


async def test_fake_agent_validates_task_with_zero_tool_calls() -> None:
    deps = _deps()
    service = FakeAgentService(deps)
    try:
        with pytest.raises(AgentTaskTooLargeError):
            await service.run("", (), principal=OPS)
    finally:
        await service.aclose()


async def test_fake_records_history_and_principal_and_marks_answer() -> None:
    deps = build_agent_tool_deps(
        Settings(use_fake_llm=True, use_fake_search=True),
        conversation_store=InMemoryConversationStore(),
        token_budget=50_000,
    )
    service = FakeAgentService(deps)
    principal = Principal(tenant_id="t1", user_id="u1", group_ids=("g1",))
    history = (
        AgentHistoryTurn(role="user", text="Hello"),
        AgentHistoryTurn(role="assistant", text="Hi"),
    )
    try:
        result = await service.run("what changed?", history, principal=principal)
    finally:
        await service.aclose()
    assert service.last_history == history
    assert service.last_principal is principal
    assert "history=2" in result.answer


async def test_build_selects_fake_by_default_and_aclose_is_idempotent() -> None:
    deps = _deps()
    service = build_agent_service(_settings(), deps)
    assert isinstance(service, FakeAgentService)
    await service.aclose()
    await service.aclose()  # idempotent
