"""Limit-hit tests for the real Agent Framework adapter (Day 17).

These drive the *production* `OpenAIChatClient` — the one whose MRO carries
`FunctionInvocationLayer` (locked by `test_agent_framework_api_surface.py`) —
so the loop under test is the framework's own, not a scripted stand-in.
Only the transport is faked: `openai.AsyncOpenAI` is real, but the single
call the framework makes on it (`responses.with_raw_response.create`) is
mocked to always answer with function calls, so the run can only end at a
limit.

Two mock shapes are driven, because the limits behave differently under each:

* *Batch* (`CALLS_PER_RESPONSE` calls per response) separates the three
  layers. The framework's own `max_function_calls` is a between-batch check
  (it sets `tool_choice="none"` after a batch lands), so it cannot bound a
  batch that is already larger than what is left of the budget. The per-run
  admission counter can, and that is exactly what
  `test_function_call_limit_via_admission` pins. A batch of 3 against a
  budget of 2 is the smallest overshoot that shows it.
* *Sequential* (one call per response) is the shape production actually
  ships: `allow_multiple_tool_calls=False` reaches the wire as
  `parallel_tool_calls=False` (pinned by
  `test_guardrail_options_reach_the_request`), so a real provider answers one
  call at a time. Under that shape admission never refuses anything — the
  framework disables tools first — and the run ends on the framework's own
  fallback text. `test_function_call_limit_sequential_mode` pins that
  outcome, and is also where the trace-derived fields (`answer`,
  `tool_calls`, `tool_round_count`, `per_round`) are asserted against real
  framework message assembly.
"""

import asyncio
import inspect
import itertools
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_usage import ResponseUsage

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.agent_framework import (
    AgentFrameworkService,
    AgentHistoryTurn,
    AgentRunError,
    build_agent_service,
)
from azgenai_lab.services.agent_tools import build_agent_tool_deps
from azgenai_lab.services.conversation_store import InMemoryConversationStore

OPS = Principal(tenant_id="opsdemo", user_id="u1", group_ids=())
PROMPT = load_prompt("ops_agent")

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

# Sequential runs alternate two tool names so the positional join's
# tool-name-mismatch guard (`_join_executions_to_rounds`) is checked against a
# transcript where a mis-ordered join would actually differ. A single repeated
# name makes that guard vacuous.
SEQUENTIAL_TOOL_NAMES = ("get_runtime_config", "get_conversation_usage")

# Arguments the mock emits per tool. Both tools answer without touching a
# provider: the runtime-config snapshot is pure settings, and an unknown
# conversation id is the documented not-found shape, not an error.
TOOL_ARGUMENTS = {
    "get_runtime_config": "{}",
    "get_conversation_usage": '{"conversation_id": "no-such-conversation"}',
}

# `agent_framework._tools._FUNCTION_INVOCATION_LIMIT_FALLBACK_TEXT`, injected by
# `_ensure_function_invocation_limit_fallback_response` when the tool budget is
# spent and the response it stripped calls out of had no other visible content.
# It used to surface verbatim as the app's `answer`; Task 4 strips it at the
# adapter boundary (`strip_framework_fallback`), so it is kept here only for
# reference and is no longer asserted against `result.answer`.
FUNCTION_LIMIT_FALLBACK = (
    "Function invocation limit reached before a final answer could be produced."
)

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


def _tool_calling_response(
    counter: "itertools.count[int]", tool_names: Sequence[str] = ()
) -> Response:
    names = tool_names or (TOOL_NAME,) * CALLS_PER_RESPONSE
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
                name=name,
                arguments=TOOL_ARGUMENTS[name],
                status="completed",
            )
            for index, name in ((next(counter), name) for name in names)
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


def _final_text_response(counter: "itertools.count[int]", text: str) -> Response:
    """A terminal response carrying model-authored visible text instead of a
    function call -- the shape the framework produces when it does NOT need
    to inject its fallback."""
    index = next(counter)
    return Response(
        id="resp_mock",
        created_at=0.0,
        model="chat-mini",
        object="response",
        output=[
            ResponseOutputMessage(
                id=f"msg_{index}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
            )
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


def _install_sequential_tool_calling_mock(
    service: AgentFrameworkService,
    *,
    final_text: str | None = None,
    final_after: int = 0,
) -> None:
    """Same seam, but one call per response — the production wire shape.

    `parallel_tool_calls=False` is what the service sends, so a provider
    answers with a single function call per turn; this mock reproduces that,
    cycling `SEQUENTIAL_TOOL_NAMES` so consecutive rounds differ.

    When `final_text` is set, the first `final_after` calls answer with tool
    calls as usual, and every call after that answers with model-authored
    visible text instead — reproducing a run where the tool budget is spent
    but the model still gets to answer on its own, so the framework never
    injects its fallback.
    """
    counter = itertools.count()
    names = itertools.cycle(SEQUENTIAL_TOOL_NAMES)
    call_index = itertools.count()

    async def _create(**_kwargs: Any) -> _RawResponseStub:
        idx = next(call_index)
        if final_text is not None and idx >= final_after:
            return _RawResponseStub(_final_text_response(counter, final_text))
        return _RawResponseStub(_tool_calling_response(counter, (next(names),)))

    service._client.responses.with_raw_response.create = AsyncMock(side_effect=_create)


def _make_mock_raise(service: AgentFrameworkService) -> None:
    request = httpx.Request("POST", "https://masked.example/openai/v1/responses")
    service._client.responses.with_raw_response.create = AsyncMock(
        side_effect=openai.APIConnectionError(message="transport exploded", request=request)
    )


def _service(
    *, sequential: bool = False, final_text: str | None = None, **overrides: Any
) -> AgentFrameworkService:
    settings = REAL.model_copy(update=overrides)
    deps = build_agent_tool_deps(
        settings, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = AgentFrameworkService(settings, deps, prompt=PROMPT)
    if sequential:
        _install_sequential_tool_calling_mock(
            service,
            final_text=final_text,
            final_after=settings.agent_max_tool_calls,
        )
    else:
        _install_always_tool_calling_mock(service)
    return service


def _service_capturing_requests(
    *, sequential: bool = True
) -> tuple[AgentFrameworkService, list[dict[str, Any]]]:
    """Same construction as `_service`, but the mocked transport appends each
    outgoing JSON body to `captured` before answering with a terminal
    model-authored text response (mirrors the capture point of
    `test_guardrail_options_reach_the_request`)."""
    del sequential  # single terminal response: shape is identical either way
    deps = build_agent_tool_deps(
        REAL, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = AgentFrameworkService(REAL, deps, prompt=PROMPT)
    captured: list[dict[str, Any]] = []
    counter = itertools.count()

    async def _create(**kwargs: Any) -> _RawResponseStub:
        captured.append(kwargs)
        return _RawResponseStub(_final_text_response(counter, "Nothing changed."))

    service._client.responses.with_raw_response.create = AsyncMock(side_effect=_create)
    return service, captured


def _failing_transport_service() -> AgentFrameworkService:
    """The provider-error setup of `test_provider_error_becomes_agent_run_error`
    as a reusable helper."""
    service = _service()
    _make_mock_raise(service)
    return service


async def test_history_reaches_the_wire_in_order_before_task() -> None:
    """[...history, task] maps to framework messages, no session: the request
    input must carry user/assistant/user in order, the task exactly once,
    and store=false must survive."""
    service, captured = _service_capturing_requests(sequential=True)
    history = (
        AgentHistoryTurn(role="user", text="Hello"),
        AgentHistoryTurn(role="assistant", text="Hi there"),
    )
    try:
        await service.run("what changed?", history, principal=OPS)
    finally:
        await service.aclose()
    body = captured[0]
    assert body["store"] is False
    roles = [item["role"] for item in body["input"] if item.get("role")]
    assert roles[:3] == ["user", "assistant", "user"]
    texts = str(body["input"])
    assert texts.count("what changed?") == 1
    assert "Hello" in texts and "Hi there" in texts


async def test_tool_execution_lines_for_executed_and_refused(caplog) -> None:  # type: ignore[no-untyped-def]
    """One agent_tool_execution line per call, executed and refused alike;
    argument TEXT never appears — only its byte length."""
    import logging

    service = _service(sequential=False, agent_max_iterations=5, agent_max_tool_calls=2)
    with caplog.at_level(logging.INFO):
        try:
            await service.run("loop forever", (), principal=OPS)
        finally:
            await service.aclose()
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("agent_tool_execution")
    ]
    assert any("executed=True" in line for line in lines)
    assert any("executed=False" in line for line in lines)  # refused by admission
    for field in ("name=", "seq=", "executed=", "latency_ms=", "args_bytes="):
        assert all(field in line for line in lines), field
    assert all("no-such-conversation" not in line for line in lines)  # argument text redacted


async def test_run_summary_on_success(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    service = _service(sequential=True, agent_max_iterations=5, agent_max_tool_calls=2)
    with caplog.at_level(logging.INFO):
        try:
            await service.run("loop forever", (), principal=OPS)
        finally:
            await service.aclose()
    summaries = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("agent_run_summary")
    ]
    assert len(summaries) == 1
    for field in (
        "model_calls=", "tool_calls=", "refused=", "stop=", "total_tokens=",
        "duration_ms=", "prompt_name=", "prompt_version=", "prompt_sha256=",
    ):
        assert field in summaries[0], field


async def test_run_summary_on_failure_reports_unavailable_never_fabricated(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    service = _failing_transport_service()  # the file's existing provider-error setup
    with caplog.at_level(logging.INFO), pytest.raises(AgentRunError):
        try:
            await service.run("boom", (), principal=OPS)
        finally:
            await service.aclose()
    summaries = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("agent_run_summary")
    ]
    assert len(summaries) == 1
    for field in (
        "model_calls=unavailable", "stop=unavailable", "usage=unavailable",
        "tools_executed=", "duration_ms=", "prompt_name=", "prompt_version=",
        "prompt_sha256=", "may have incurred billable processing",
    ):
        assert field in summaries[0], field
    assert "stop=natural" not in summaries[0]


async def test_iteration_limit_is_never_reported_natural() -> None:
    service = _service(agent_max_iterations=2, agent_max_tool_calls=10)
    try:
        result = await service.run("loop forever", (), principal=OPS)
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
        result = await service.run("loop forever", (), principal=OPS)
    finally:
        await service.aclose()
    assert "function_call_limit" in result.limit_reasons
    assert result.tool_call_count == 2  # executed, hard-bounded
    assert result.refused_call_count >= 1


async def test_function_call_limit_sequential_mode() -> None:
    """The shape production ships: one call per response, no refusals.

    With `parallel_tool_calls=False` on the wire the framework's own
    between-batch check is enough. After the second call lands,
    `_disable_tools_at_function_call_limit` sets `tool_choice="none"`; the
    third response's call is then dropped by
    `_ensure_function_invocation_limit_fallback_response` *before* dispatch,
    so admission is never asked and `refused_call_count` stays 0. The budget
    signal therefore comes from `executed >= max_tool_calls` alone.
    """
    service = _service(sequential=True, agent_max_iterations=5, agent_max_tool_calls=2)
    try:
        result = await service.run("loop forever", (), principal=OPS)
    finally:
        await service.aclose()

    assert result.refused_call_count == 0  # admission never sees the dropped call
    assert "function_call_limit" in result.limit_reasons
    assert result.stop_reason == "function_call_limit"
    assert result.tool_call_count == 2
    # Two tool rounds plus the forced final response that carries the fallback.
    assert result.model_call_count == 3
    assert result.tool_round_count == 2
    # The framework injects the canned string here (it strips the over-budget
    # call and the response has nothing else visible), but the adapter strips
    # it at the boundary (Task 4): the structured signal is `stop_reason`, not
    # this English sentence.
    assert result.answer == ""
    assert result.usage is not None
    assert result.usage.total_tokens == 3 * TOTAL_TOKENS

    assert [call.tool_name for call in result.tool_calls] == list(SEQUENTIAL_TOOL_NAMES)
    assert all(call.executed for call in result.tool_calls)
    assert [call.round_index for call in result.tool_calls] == [1, 2]
    assert result.tool_calls[1].arguments == {"conversation_id": "no-such-conversation"}

    # per_round's failure mode is a silent None: the positional join rejects
    # rather than mis-attribute. Two differently-named calls make the
    # tool-name guard meaningful, so a non-None result here says the join
    # really held against framework-assembled messages.
    assert result.per_round is not None
    assert [metrics.round_index for metrics in result.per_round] == [1, 2]
    assert all(metrics.latency_ms is not None for metrics in result.per_round)
    assert all(metrics.usage is None for metrics in result.per_round)


async def test_guardrail_options_reach_the_request() -> None:
    """The three cost bounds are only real if they reach the wire.

    Two of the three are renamed by `_prepare_options` on the way out
    (`max_tokens` -> `max_output_tokens`, `allow_multiple_tool_calls` ->
    `parallel_tool_calls`), so the option names the service sets prove
    nothing on their own; all three are asserted on the payload the SDK
    would have sent.
    """
    captured: list[dict[str, Any]] = []
    service = _service(agent_max_iterations=1)
    counter = itertools.count()

    async def _create(**kwargs: Any) -> _RawResponseStub:
        captured.append(kwargs)
        return _RawResponseStub(_tool_calling_response(counter))

    service._client.responses.with_raw_response.create = AsyncMock(side_effect=_create)
    try:
        await service.run("what are the limits?", (), principal=OPS)
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
        with pytest.raises(AgentRunError) as err:
            await service.run("hello", (), principal=OPS)
        # usage=None is deliberate here (see the raise site's comment), not
        # an omission, and the provider exception must still be reachable
        # via the standard `raise ... from exc` chain.
        assert err.value.usage is None
        assert err.value.__cause__ is not None
        # The failure happened before any tool ran and before extract_run_shape
        # ever executed -- the degraded snapshot has zero executions and every
        # framework-derived field honestly None, not a fabricated zero.
        snapshot = err.value.audit_snapshot
        assert snapshot.provider_call_attempted is True
        assert snapshot.executions == ()
        assert snapshot.model_calls is None and snapshot.stop_reason is None
    finally:
        await service.aclose()


async def test_build_selects_real_adapter_and_aclose_is_idempotent() -> None:
    deps = build_agent_tool_deps(
        REAL, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = build_agent_service(REAL, deps, prompt=PROMPT)
    assert isinstance(service, AgentFrameworkService)
    await service.aclose()
    await service.aclose()  # idempotent


async def test_partial_construction_closes_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A framework constructor that raises must not strand the transport.

    `aclose()` is only reachable on a service that was fully built, so the
    transport created before `OpenAIChatClient`/`Agent` has no other owner.
    """
    import agent_framework

    created: list[openai.AsyncOpenAI] = []
    real_client_cls = openai.AsyncOpenAI

    class _CapturingClient(real_client_cls):  # type: ignore[valid-type, misc]
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            created.append(self)

    def _exploding_agent(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("agent construction failed")

    monkeypatch.setattr(openai, "AsyncOpenAI", _CapturingClient)
    monkeypatch.setattr(agent_framework, "Agent", _exploding_agent)

    deps = build_agent_tool_deps(
        REAL, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    with pytest.raises(RuntimeError, match="agent construction failed"):
        AgentFrameworkService(REAL, deps, prompt=PROMPT)
    await deps.retriever.aclose()

    assert len(created) == 1
    # __init__ is sync, so the close is scheduled on this loop; give it a step.
    for _ in range(5):
        if created[0].is_closed():
            break
        await asyncio.sleep(0)
    assert created[0].is_closed()


async def test_missing_azure_configuration_fails_fast() -> None:
    settings = REAL.model_copy(update={"azure_openai_api_key": None})
    deps = build_agent_tool_deps(
        settings, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        AgentFrameworkService(settings, deps, prompt=PROMPT)
    await deps.retriever.aclose()


async def test_missing_endpoint_or_deployment_still_fails_fast_without_key_check() -> None:
    # The API-key requirement moved into resolve_aoai_auth (Task 2); this
    # pre-check now only owns endpoint + deployment.
    settings = REAL.model_copy(
        update={"azure_openai_deployment_name": None, "azure_openai_api_key": None}
    )
    deps = build_agent_tool_deps(
        settings, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    with pytest.raises(
        ValueError,
        match="USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT_NAME",
    ):
        AgentFrameworkService(settings, deps, prompt=PROMPT)
    await deps.retriever.aclose()


async def test_real_service_api_key_mode_aclose_is_a_safe_no_op_and_still_closes_client() -> None:
    # api_key mode never mints a credential; aclose() must still close the
    # AsyncOpenAI client, and awaiting the resolver's no-op closer must not
    # raise.
    deps = build_agent_tool_deps(
        REAL, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = AgentFrameworkService(REAL, deps, prompt=PROMPT)

    await service.aclose()

    assert service._client.is_closed()
    await deps.retriever.aclose()


class _CloseTrackingCredential:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def _patch_entra_credential(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    created: dict[str, object] = {}

    def fake_credential_ctor(client_id: str) -> _CloseTrackingCredential:
        credential = _CloseTrackingCredential(client_id)
        created["credential"] = credential
        return credential

    def fake_provider(credential: object, scope: str) -> Callable[[], Awaitable[str]]:
        created["scope"] = scope

        async def token_callable() -> str:
            return "tok"

        return token_callable

    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.ManagedIdentityCredential",
        fake_credential_ctor,
    )
    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.get_bearer_token_provider", fake_provider
    )
    return created


async def test_real_service_builds_in_entra_mode_and_shares_the_credentialed_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _patch_entra_credential(monkeypatch)
    settings = REAL.model_copy(
        update={
            "azure_openai_auth": "entra",
            "azure_client_id": "cid",
            "azure_openai_api_key": None,
        }
    )
    deps = build_agent_tool_deps(
        settings, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = AgentFrameworkService(settings, deps, prompt=PROMPT)
    try:
        provider = service._client._api_key_provider
        assert provider is not None
        assert inspect.iscoroutinefunction(provider)
        assert created["scope"] == "https://cognitiveservices.azure.com/.default"
        assert isinstance(created["credential"], _CloseTrackingCredential)
        # `_build_agent` hands `async_client=self._client` to OpenAIChatClient
        # (agent_framework.py) — there is only ever one AsyncOpenAI instance
        # here, so this is the same credentialed transport the framework
        # actually calls through, not a second one built alongside it.
    finally:
        await service.aclose()
        await deps.retriever.aclose()


async def test_real_service_entra_mode_aclose_closes_credential_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _patch_entra_credential(monkeypatch)
    settings = REAL.model_copy(
        update={
            "azure_openai_auth": "entra",
            "azure_client_id": "cid",
            "azure_openai_api_key": None,
        }
    )
    deps = build_agent_tool_deps(
        settings, conversation_store=InMemoryConversationStore(), token_budget=400
    )
    service = AgentFrameworkService(settings, deps, prompt=PROMPT)
    credential = created["credential"]
    assert isinstance(credential, _CloseTrackingCredential)

    await service.aclose()

    assert credential.close_count == 1
    assert service._client.is_closed()

    await service.aclose()  # idempotent: no second close
    assert credential.close_count == 1

    await deps.retriever.aclose()


async def test_iteration_limit_forced_final_fallback_is_stripped() -> None:
    """Pure iteration exhaustion (tool budget untouched): Phase 3's forced
    final also injects the fallback; the adapter must strip it here too."""
    service = _service(sequential=True, agent_max_iterations=1, agent_max_tool_calls=10)
    try:
        result = await service.run("loop forever", (), principal=OPS)
    finally:
        await service.aclose()
    assert result.stop_reason == "iteration_limit"
    assert result.answer == ""


async def test_both_limits_iteration_precedence_still_strips() -> None:
    service = _service(sequential=True, agent_max_iterations=1, agent_max_tool_calls=1)
    try:
        result = await service.run("loop forever", (), principal=OPS)
    finally:
        await service.aclose()
    assert result.stop_reason == "iteration_limit"
    assert "function_call_limit" in result.limit_reasons
    assert result.answer == ""


async def test_limit_with_real_visible_content_is_preserved() -> None:
    """When the model authored text in the terminal response, the framework
    does not inject the fallback and the adapter must not strip anything."""
    # Arrange the mock transport so the LAST response carries visible text
    # (reuse the file's final-answer response builder) while the tool budget
    # is already exhausted by the earlier responses.
    service = _service(
        sequential=True,
        agent_max_iterations=5,
        agent_max_tool_calls=2,
        final_text="Here is what I found.",
    )
    try:
        result = await service.run("loop forever", (), principal=OPS)
    finally:
        await service.aclose()
    assert result.stop_reason == "function_call_limit"
    assert result.answer == "Here is what I found."
