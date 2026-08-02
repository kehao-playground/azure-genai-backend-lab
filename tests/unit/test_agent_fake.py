import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_framework import (
    AgentTaskTooLargeError,
    FakeAgentService,
    build_agent_service,
)
from azgenai_lab.services.agent_tools import AgentToolset, build_agent_toolset
from azgenai_lab.services.conversation_store import InMemoryConversationStore

OPS = Principal(tenant_id="opsdemo", group_ids=())


def _settings() -> Settings:
    """Settings isolated from the repo-root `.env`.

    `conftest.py` pins only the three fake-adapter flags, so every other
    field is still ambient: a developer's untracked `.env` would otherwise
    reach these assertions, and no fresh clone or CI checkout would
    reproduce the failure (the same hazard `tests/unit/test_config.py`
    avoids with `_env_file=None`).
    """
    return Settings(_env_file=None)


def _toolset() -> AgentToolset:
    return build_agent_toolset(
        _settings(), OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )


async def test_fake_agent_actually_invokes_injected_tools() -> None:
    toolset = _toolset()
    service = FakeAgentService(toolset)
    try:
        result = await service.run("what is the token budget?")
    finally:
        await service.aclose()
    # wiring proof (Day 8 fake-marker philosophy): tool outputs are embedded
    assert result.answer.startswith("[fake-agent]")
    assert "conversation_token_budget" in result.answer  # from get_runtime_config
    assert '"hits"' in result.answer  # from search_docs
    assert result.stop_reason == "natural" and result.limit_reasons == frozenset()
    assert result.model_call_count == 2 and result.tool_round_count == 1
    assert result.tool_call_count == len(toolset.tools)
    assert all(c.executed for c in result.tool_calls)
    assert result.usage is not None  # deterministic fake usage, wired end to end


async def test_fake_agent_validates_task_with_zero_tool_calls() -> None:
    toolset = _toolset()
    service = FakeAgentService(toolset)
    try:
        with pytest.raises(AgentTaskTooLargeError):
            await service.run("")
    finally:
        await service.aclose()


async def test_build_selects_fake_by_default_and_aclose_is_idempotent() -> None:
    toolset = _toolset()
    service = build_agent_service(_settings(), toolset)
    assert isinstance(service, FakeAgentService)
    await service.aclose()
    await service.aclose()  # idempotent
