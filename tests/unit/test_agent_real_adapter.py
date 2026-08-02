"""Limit-hit tests for the real Agent Framework adapter (Day 17).

These drive the *production* `OpenAIChatClient` — the one whose MRO carries
`FunctionInvocationLayer` (locked by `test_agent_framework_api_surface.py`) —
so the loop under test is the framework's own, not a scripted stand-in.
Only the transport is faked: `openai.AsyncOpenAI` is real, but the single
call the framework makes on it (`responses.with_raw_response.create`) is
mocked to always answer with function calls, so the run can only end at a
limit.

The mock answers with a *batch* of `_CALLS_PER_RESPONSE` calls. That is the
one shape that separates the three layers: the framework's own
`max_function_calls` is a between-batch check (it sets `tool_choice="none"`
after a batch lands), so it cannot bound a batch that is already larger than
what is left of the budget. The per-run admission counter can, and that is
exactly what `test_function_call_limit_via_admission` pins. A batch of 3
against a budget of 2 is the smallest overshoot that shows it.
"""

import itertools
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from openai.types.responses import Response, ResponseFunctionToolCall
from openai.types.responses.response_usage import ResponseUsage

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_framework import (
    AgentFrameworkService,
    AgentRunError,
    build_agent_service,
)
from azgenai_lab.services.agent_tools import build_agent_toolset
from azgenai_lab.services.conversation_store import InMemoryConversationStore

OPS = Principal(tenant_id="opsdemo", group_ids=())

REAL = Settings(
    use_fake_llm=False,
    azure_openai_endpoint="https://masked.example",
    azure_openai_api_key="test-key",
    azure_openai_deployment_name="chat-mini",
)

# The zero-argument tool: the mock can emit `{}` arguments for it without
# inventing a query the fake corpus would have to answer.
TOOL_NAME = "get_runtime_config"

# Per model response. Must exceed the smallest `agent_max_tool_calls` under
# test (2) for admission to have anything to refuse.
CALLS_PER_RESPONSE = 3

# Per model call, so a three-call run aggregates to 30 / 15 / 45 / 6.
INPUT_TOKENS = 10
OUTPUT_TOKENS = 5
TOTAL_TOKENS = 15
REASONING_TOKENS = 2


class _RawResponseStub:
    """Stands in for the SDK's raw-response wrapper.

    `_inner_get_response` calls `.parse()` for the body and reads `.headers`
    to detect the served model, so those two are the whole contract.
    """

    def __init__(self, response: Response) -> None:
        self._response = response
        self.headers: dict[str, str] = {}

    def parse(self) -> Response:
        return self._response


def _tool_calling_response(counter: "itertools.count[int]") -> Response:
    return Response(
        id="resp_mock",
        created_at=0.0,
        model="chat-mini",
        object="response",
        output=[
            ResponseFunctionToolCall(
                type="function_call",
                id=f"fc_{index}",
                call_id=f"call_{index}",
                name=TOOL_NAME,
                arguments="{}",
                status="completed",
            )
            for index in (next(counter) for _ in range(CALLS_PER_RESPONSE))
        ],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        status="completed",
        usage=ResponseUsage.model_validate({
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "total_tokens": TOTAL_TOKENS,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": REASONING_TOKENS},
        }),
    )


def _install_always_tool_calling_mock(service: AgentFrameworkService) -> None:
    """Mock the one transport call the non-streaming framework path makes.

    `agent_framework_openai._chat_client` reaches the SDK through
    `client.responses.with_raw_response.create(stream=False, **run_options)`
    and then `.parse()`s the result, so that method is the seam. Everything
    above it — option translation, the function-invocation loop, tool
    dispatch, usage aggregation — is the real framework.
    """
    counter = itertools.count()

    async def _create(**_kwargs: Any) -> _RawResponseStub:
        return _RawResponseStub(_tool_calling_response(counter))

    service._client.responses.with_raw_response.create = AsyncMock(side_effect=_create)


def _make_mock_raise(service: AgentFrameworkService) -> None:
    request = httpx.Request("POST", "https://masked.example/openai/v1/responses")
    service._client.responses.with_raw_response.create = AsyncMock(
        side_effect=openai.APIConnectionError(message="transport exploded", request=request)
    )


def _service(**overrides: Any) -> AgentFrameworkService:
    settings = REAL.model_copy(update=overrides)
    toolset = build_agent_toolset(
        settings, OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = AgentFrameworkService(settings, toolset)
    _install_always_tool_calling_mock(service)
    return service


async def test_iteration_limit_is_never_reported_natural() -> None:
    service = _service(agent_max_iterations=2, agent_max_tool_calls=10)
    try:
        result = await service.run("loop forever")
    finally:
        await service.aclose()
    assert result.stop_reason == "iteration_limit"
    assert result.model_call_count == 3  # 2 iterations + forced final
    assert "iteration_limit" in result.limit_reasons
    # Usage is the loop's aggregate, not the last call's: three model calls.
    assert result.usage is not None
    assert result.usage.total_tokens == 3 * TOTAL_TOKENS
    assert result.usage.reasoning_tokens == 3 * REASONING_TOKENS


async def test_function_call_limit_via_admission() -> None:
    service = _service(agent_max_iterations=5, agent_max_tool_calls=2)
    try:
        result = await service.run("loop forever")
    finally:
        await service.aclose()
    assert "function_call_limit" in result.limit_reasons
    assert result.tool_call_count == 2  # executed, hard-bounded
    assert result.refused_call_count >= 1


async def test_guardrail_options_reach_the_request() -> None:
    """The three cost bounds are only real if they reach the wire.

    `max_output_tokens` is not a declared `ChatOptions` key (the service
    casts around that), and `allow_multiple_tool_calls` is renamed on the
    way out, so both are asserted on the payload the SDK would have sent.
    """
    captured: list[dict[str, Any]] = []
    service = _service(agent_max_iterations=1)
    counter = itertools.count()

    async def _create(**kwargs: Any) -> _RawResponseStub:
        captured.append(kwargs)
        return _RawResponseStub(_tool_calling_response(counter))

    service._client.responses.with_raw_response.create = AsyncMock(side_effect=_create)
    try:
        await service.run("what are the limits?")
    finally:
        await service.aclose()

    assert captured
    for payload in captured:
        assert payload["store"] is False
        assert payload["max_output_tokens"] == REAL.llm_max_output_tokens
        assert payload["parallel_tool_calls"] is False  # allow_multiple_tool_calls=False
        assert payload["instructions"].startswith("You are the operations assistant")
        # Tools are per-run, not baked into the Agent (fresh admission state).
        assert {tool["name"] for tool in payload["tools"]} == {
            "search_docs",
            "get_runtime_config",
            "get_conversation_usage",
        }


async def test_provider_error_becomes_agent_run_error() -> None:
    service = _service()
    _make_mock_raise(service)  # harness: transport raises APIError
    try:
        with pytest.raises(AgentRunError):
            await service.run("hello")
    finally:
        await service.aclose()


async def test_build_selects_real_adapter_and_aclose_is_idempotent() -> None:
    toolset = build_agent_toolset(
        REAL, OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = build_agent_service(REAL, toolset)
    assert isinstance(service, AgentFrameworkService)
    await service.aclose()
    await service.aclose()  # idempotent


async def test_missing_azure_configuration_fails_fast() -> None:
    settings = REAL.model_copy(update={"azure_openai_api_key": None})
    toolset = build_agent_toolset(
        settings, OPS, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        AgentFrameworkService(settings, toolset)
    await toolset.retriever.aclose()
