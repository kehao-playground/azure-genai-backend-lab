"""Microsoft Agent Framework adapter (Day 17).

Framework types stop at this module's boundary: callers see the
AgentService Protocol and plain dataclasses, never agent_framework types
(the same rule Day 6 applied to Responses typed events).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import openai

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.agent_tools import AgentToolFn, AgentToolset

logger = logging.getLogger(__name__)

# Task input cap in UTF-8 bytes (spec §2): without it the "fixed" part of the
# per-run cost formula has no upper bound. Byte-measured for the same reason
# as every other cap in this feature: tokens >= 1 byte each.
AGENT_MAX_TASK_BYTES = 4000

# Fixed refusal constant (< MAX_REFUSAL_RESULT_BYTES). Also the marker the
# trace extractor uses to tag refused calls (executed=False).
REFUSAL_MESSAGE = "tool call refused: this run's tool call budget is exhausted"


class AgentTaskTooLargeError(ValueError):
    """Caller-input error raised before any model call — never wrapped into
    the terminal-loop AgentRunError."""


def validate_task(task: str) -> str:
    if not task.strip():
        raise AgentTaskTooLargeError("task must be non-empty")
    size = len(task.encode("utf-8"))
    if size > AGENT_MAX_TASK_BYTES:
        raise AgentTaskTooLargeError(
            f"task is {size} UTF-8 bytes; the cap is {AGENT_MAX_TASK_BYTES}"
        )
    return task


def map_usage_details(details: Mapping[str, Any] | None) -> TokenUsage | None:
    """UsageDetails (all-optional *_token_count fields) -> Day 9 TokenUsage.

    All-or-none for the required trio; reasoning maps when present. Missing
    block or missing required count -> None plus a log line (honest absence,
    Day 9 style) — never fabricated zeros.
    """
    if details is None:
        return None
    input_tokens = details.get("input_token_count")
    output_tokens = details.get("output_token_count")
    total_tokens = details.get("total_token_count")
    if input_tokens is None or output_tokens is None or total_tokens is None:
        logger.info("agent usage incomplete, dropping: keys=%s", sorted(details))
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=details.get("reasoning_output_token_count"),
    )


@dataclass(frozen=True)
class AgentToolCall:
    tool_name: str
    arguments: Mapping[str, Any] | None  # parsed; None when unparseable
    arguments_canonical_json: str  # canonical re-serialization, not raw bytes
    round_index: int  # 1-based; parallel calls share an index
    executed: bool  # False = refused by admission


@dataclass(frozen=True)
class AgentRoundMetrics:
    round_index: int
    latency_ms: float | None
    usage: TokenUsage | None


@dataclass(frozen=True)
class AgentRunResult:
    answer: str
    model_call_count: int
    tool_round_count: int
    tool_call_count: int  # executed invocations only
    refused_call_count: int
    stop_reason: Literal["natural", "iteration_limit", "function_call_limit"]
    limit_reasons: frozenset[str]
    tool_calls: tuple[AgentToolCall, ...]
    usage: TokenUsage | None
    per_round: tuple[AgentRoundMetrics, ...] | None


class AgentRunError(Exception):
    """Terminal loop failure. Carries usage aggregated before the failure
    when obtainable — a mid-loop failure has already been billed."""

    def __init__(self, message: str, *, usage: TokenUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


_pending_transport_closes: set[asyncio.Task[None]] = set()


def _close_transport_best_effort(client: openai.AsyncOpenAI) -> None:
    """Close a transport from synchronous code (partial-construction cleanup).

    `__init__` cannot await, so the client's `close()` coroutine is run to
    completion when no loop is running and handed to the running loop
    otherwise, holding a strong reference so the task is not garbage-collected
    mid-flight. This is only reached before any request has been sent, so the
    pool holds no live connection and the close is bookkeeping, not I/O.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.close())
        return
    task = loop.create_task(client.close())
    _pending_transport_closes.add(task)
    task.add_done_callback(_pending_transport_closes.discard)


class AgentService(Protocol):
    async def run(self, task: str) -> AgentRunResult: ...

    async def aclose(self) -> None: ...


@dataclass
class ToolExecution:
    """Wrapper-side record: the only latency source for executed tools."""

    tool_name: str
    executed: bool
    latency_ms: float


class AdmissionState:
    """Per-run hard bound on *executed* tool invocations (spec §2a).

    A slot is atomically consumed at admission — immediately before the tool
    body is invoked — and is never released, including when cancellation
    lands between admission and the body's first line. Releasing there would
    let a retrying loop start more than `limit` tool bodies.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.admitted = 0
        self.refused = 0
        self._lock = asyncio.Lock()

    async def try_admit(self) -> bool:
        async with self._lock:
            if self.admitted >= self.limit:
                self.refused += 1
                return False
            self.admitted += 1
            return True


def wrap_tools_with_admission(
    tools: Sequence[AgentToolFn],
    state: AdmissionState,
    executions: list[ToolExecution],
    *,
    _test_pause_after_admit: asyncio.Event | None = None,
) -> list[AgentToolFn]:
    """Fresh-per-run wrapping: the counter lives on the run, never on the
    long-lived toolset (spec §2a)."""

    def _wrap(tool: AgentToolFn) -> AgentToolFn:
        @functools.wraps(tool)  # preserves name/doc/signature for framework introspection
        async def admitted_tool(*args: Any, **kwargs: Any) -> str:
            if not await state.try_admit():
                executions.append(
                    ToolExecution(tool.__name__, executed=False, latency_ms=0.0)
                )
                return REFUSAL_MESSAGE
            if _test_pause_after_admit is not None:
                await _test_pause_after_admit.wait()
            start = time.perf_counter()
            try:
                return await tool(*args, **kwargs)
            finally:
                # runs on normal return, on a raised exception, and on
                # cancellation: the slot was consumed and the body ran, so
                # the sink must not silently lose failing/slow calls. The
                # exception (if any) propagates unchanged — `finally` here
                # only records, it never suppresses or converts.
                executions.append(
                    ToolExecution(
                        tool.__name__,
                        executed=True,
                        latency_ms=(time.perf_counter() - start) * 1000,
                    )
                )

        return admitted_tool

    return [_wrap(tool) for tool in tools]


@dataclass(frozen=True)
class _RunShape:
    answer: str
    model_call_count: int
    tool_round_count: int
    tool_calls: tuple[AgentToolCall, ...]
    per_round: tuple[AgentRoundMetrics, ...] | None


def _content_attr(content: Any, *names: str) -> Any:
    for name in names:
        value = getattr(content, name, None)
        if value is not None:
            return value
    return None


def _parse_call_arguments(raw: Any) -> tuple[Mapping[str, Any] | None, str]:
    """Parse a function_call's `arguments` per the framework's own
    three-way split (`Content.parse_arguments`): an absent attribute means
    no arguments were offered at all (`None`); an empty-but-present value
    (`""` or `{}`) means an explicit no-arg call (`{}`). Genuinely
    malformed JSON, and JSON that parses but isn't an object (a list, a
    bare `null`, a number, a string), both fall through to the same
    unparseable outcome (`arguments=None`) with the raw text preserved as
    the canonical field, so they stay distinguishable from the two zero-
    argument forms above.
    """
    if raw is None:
        return None, ""
    if isinstance(raw, Mapping):
        parsed = dict(raw)
        return parsed, json.dumps(parsed, sort_keys=True)
    raw_text = str(raw)
    if raw_text == "":
        return {}, "{}"
    try:
        loaded = json.loads(raw_text)
    except (ValueError, TypeError):
        return None, raw_text
    if isinstance(loaded, dict):
        return loaded, json.dumps(loaded, sort_keys=True)
    return None, raw_text


def _join_executions_to_rounds(
    tool_calls: Sequence[AgentToolCall],
    executions: Sequence[ToolExecution],
) -> tuple[AgentRoundMetrics, ...] | None:
    """Positional join of measured tool latency onto rounds.

    `executions` are appended in admission order (`wrap_tools_with_admission`)
    and tool-call contents are walked in message order above, with the
    primary mode sequential (`allow_multiple_tool_calls: False`), so index
    *i* of `executions` corresponds to index *i* of `tool_calls`. A length
    mismatch or a tool-name mismatch at the same index means the positional
    assumption doesn't hold here -- reject to `None` (honest absence)
    rather than attribute latency to the wrong round.
    """
    if len(executions) != len(tool_calls):
        logger.info(
            "agent per-round latency join rejected: %d executions but %d tool calls",
            len(executions),
            len(tool_calls),
        )
        return None
    for index, (call, execution) in enumerate(zip(tool_calls, executions, strict=True)):
        if call.tool_name != execution.tool_name:
            logger.info(
                "agent per-round latency join rejected: tool name mismatch at "
                "index %d (tool_call=%r, execution=%r)",
                index,
                call.tool_name,
                execution.tool_name,
            )
            return None
    totals: dict[int, float] = {}
    for call, execution in zip(tool_calls, executions, strict=True):
        totals[call.round_index] = totals.get(call.round_index, 0.0) + execution.latency_ms
    return tuple(
        AgentRoundMetrics(round_index=round_index, latency_ms=latency_ms, usage=None)
        for round_index, latency_ms in sorted(totals.items())
    )


def extract_run_shape(
    response_messages: Sequence[Any],
    executions: Sequence[ToolExecution],
    *,
    refusal_message: str,
) -> _RunShape:
    """Derive the app-owned run shape from the framework transcript.

    Rounds are 1-based assistant-message ordinals; a call is `executed`
    iff its function result is not the refusal constant. The answer is the
    terminal assistant message's text only — earlier rounds' text never
    leaks into it (spec §5).
    """
    results_by_call_id: dict[str, str] = {}
    for message in response_messages:
        for content in message.contents:
            if content.type == "function_result":
                call_id = _content_attr(content, "call_id")
                if call_id is None:
                    # Never key a real result under the literal string
                    # "None" -- two such contents would collide and
                    # cross-assign each other's results.
                    continue
                results_by_call_id[str(call_id)] = str(_content_attr(content, "result"))

    tool_calls: list[AgentToolCall] = []
    assistant_ordinal = 0
    last_assistant_text = ""
    tool_round_count = 0
    for message in response_messages:
        if message.role != "assistant":
            continue
        assistant_ordinal += 1
        texts: list[str] = []
        saw_call = False
        for content in message.contents:
            if content.type == "text":
                texts.append(str(_content_attr(content, "text") or ""))
            elif content.type == "function_call":
                saw_call = True
                parsed, canonical = _parse_call_arguments(_content_attr(content, "arguments"))
                call_id = str(_content_attr(content, "call_id"))
                result = results_by_call_id.get(call_id, "")
                tool_calls.append(
                    AgentToolCall(
                        tool_name=str(_content_attr(content, "name")),
                        arguments=parsed,
                        arguments_canonical_json=canonical,
                        round_index=assistant_ordinal,
                        executed=result != refusal_message,
                    )
                )
            # Per-response usage is not available at these pins: the
            # non-streaming client puts `usage` on the response object (not
            # a message content), and the streaming aggregator strips
            # `usage` contents out of messages before this function ever
            # sees them. So `usage` is None on every round below; the
            # measured signal is per-tool latency, joined in separately.
        if saw_call:
            tool_round_count += 1
        # Assign unconditionally: a text-less terminal message (e.g.
        # reasoning-only output) must yield an honest "", never an earlier
        # round's aside.
        last_assistant_text = "".join(texts)

    per_round = _join_executions_to_rounds(tool_calls, executions)

    return _RunShape(
        answer=last_assistant_text,
        model_call_count=assistant_ordinal,
        tool_round_count=tool_round_count,
        tool_calls=tuple(tool_calls),
        per_round=per_round,
    )


def derive_stop(
    model_call_count: int,
    *,
    executed: int,
    refused: int,
    max_iterations: int,
    max_tool_calls: int,
) -> tuple[Literal["natural", "iteration_limit", "function_call_limit"], frozenset[str]]:
    """App-owned stop classification (spec §5): a framework forced-final is
    never reported natural. iteration_limit wins the single label when both
    limits fire; limit_reasons carries every limit that fired.

    Note: `executed >= max_tool_calls` with `refused == 0` still reports
    `function_call_limit` even if the model went on to answer on its own
    afterward -- the tool-call budget was exhausted, which is not proof
    that the limit is what truncated the run. Treat it as a budget signal,
    not a causal claim.
    """
    reasons: set[str] = set()
    if model_call_count >= max_iterations + 1:
        reasons.add("iteration_limit")
    if refused > 0 or executed >= max_tool_calls:
        reasons.add("function_call_limit")
    if "iteration_limit" in reasons:
        return "iteration_limit", frozenset(reasons)
    if "function_call_limit" in reasons:
        return "function_call_limit", frozenset(reasons)
    return "natural", frozenset()


class FakeAgentService:
    """Deterministic stand-in that really invokes the injected tools.

    One simulated round: every tool is called once (search gets the task as
    its query), results are embedded in the answer so contract tests prove
    the wiring -- the fake never talks to any provider."""

    def __init__(self, toolset: AgentToolset) -> None:
        self._toolset = toolset
        self._closed = False

    async def run(self, task: str) -> AgentRunResult:
        validate_task(task)
        tool_calls: list[AgentToolCall] = []
        outputs: list[str] = []
        for tool in self._toolset.tools:
            name = tool.__name__
            kwargs: dict[str, Any] = {}
            if name == "search_docs":
                kwargs = {"query": task}
            elif name == "get_conversation_usage":
                kwargs = {"conversation_id": "fake-conversation"}
            output = await tool(**kwargs)
            outputs.append(output)
            tool_calls.append(
                AgentToolCall(
                    tool_name=name,
                    arguments=kwargs,
                    arguments_canonical_json=json.dumps(kwargs, sort_keys=True),
                    round_index=1,
                    executed=True,
                )
            )
        usage = TokenUsage(
            input_tokens=20, output_tokens=10, total_tokens=30, reasoning_tokens=0
        )
        return AgentRunResult(
            answer=f"[fake-agent] {task} (tools={len(tool_calls)}) " + " ".join(outputs),
            model_call_count=2,  # one tool round + one final answer
            tool_round_count=1,
            tool_call_count=len(tool_calls),
            refused_call_count=0,
            stop_reason="natural",
            limit_reasons=frozenset(),
            tool_calls=tuple(tool_calls),
            usage=usage,
            per_round=None,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._toolset.retriever.aclose()


class AgentFrameworkService:
    """Real adapter over the pinned Microsoft Agent Framework.

    Sequential tool mode (allow_multiple_tool_calls=False -> provider
    parallel_tool_calls=false) is the primary cost bound; the per-run
    admission counter is defense-in-depth; the framework's own
    max_function_calls is the third layer (between-batch, best-effort).
    """

    def __init__(self, settings: Settings, toolset: AgentToolset) -> None:
        if not (
            settings.azure_openai_endpoint
            and settings.azure_openai_api_key
            and settings.azure_openai_deployment_name
        ):
            raise ValueError(
                "USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT_NAME"
            )
        self._settings = settings
        self._toolset = toolset
        self._prompt = load_prompt("ops_agent")
        self._closed = False
        # Project-owned transport: same timeout/retry policy as the chat
        # adapter — the framework never reads its own env vars here.
        self._client = openai.AsyncOpenAI(
            api_key=settings.azure_openai_api_key.get_secret_value(),
            base_url=settings.azure_openai_endpoint.rstrip("/") + "/openai/v1/",
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        # Everything after the transport exists runs under this guard: the
        # transport is owned from the moment it is constructed, and a failure
        # in either framework constructor would otherwise strand it (aclose()
        # is only reachable on a service that was fully built).
        try:
            self._build_agent(settings)
        except BaseException:
            _close_transport_best_effort(self._client)
            raise

    def _build_agent(self, settings: Settings) -> None:
        from agent_framework import Agent
        from agent_framework.openai import OpenAIChatClient, OpenAIChatOptions

        chat_client = OpenAIChatClient(
            model=settings.azure_openai_deployment_name,
            async_client=self._client,
            function_invocation_configuration={
                "max_iterations": settings.agent_max_iterations,
                "max_function_calls": settings.agent_max_tool_calls,
            },
        )
        # All three keys are declared on OpenAIChatOptions, so the TypedDict
        # annotation (no cast) keeps mypy checking every one of them. Two are
        # renamed on the way to the wire by `_prepare_options`'s translation
        # table: `max_tokens` -> `max_output_tokens` (the Responses spelling of
        # Day 9's per-call cap — `max_tokens` is this client's option name for
        # it, not a chat-completions leftover) and `allow_multiple_tool_calls`
        # -> `parallel_tool_calls`. The wire-payload test pins the renamed
        # forms, since the option names alone prove nothing about the request.
        default_options: OpenAIChatOptions = {
            "store": False,
            "max_tokens": settings.llm_max_output_tokens,
            "allow_multiple_tool_calls": False,
        }
        # Tools deliberately absent here: each run() wraps them with a fresh
        # AdmissionState and passes them per-run (spec §2a).
        self._agent = Agent(
            client=chat_client,
            instructions=self._prompt.text,
            default_options=default_options,
        )

    async def run(self, task: str) -> AgentRunResult:
        validate_task(task)
        state = AdmissionState(limit=self._settings.agent_max_tool_calls)
        executions: list[ToolExecution] = []
        wrapped = wrap_tools_with_admission(self._toolset.tools, state, executions)
        try:
            response = await self._agent.run(task, tools=wrapped)
        except Exception as exc:  # framework/provider terminal failure
            # usage=None is the honest value, not an omission: the framework's
            # loop keeps its running `aggregated_usage` as a function local and
            # attaches it only to a response it returns, never to anything it
            # raises. A mid-loop failure has already been billed for the calls
            # that completed, but that number is discarded above us and there is
            # no supported way to reach it (Day 9: never fabricate counts).
            raise AgentRunError(str(exc), usage=None) from exc
        shape = extract_run_shape(
            response.messages, executions, refusal_message=REFUSAL_MESSAGE
        )
        executed = sum(1 for e in executions if e.executed)
        refused = state.refused
        stop_reason, limit_reasons = derive_stop(
            shape.model_call_count,
            executed=executed,
            refused=refused,
            max_iterations=self._settings.agent_max_iterations,
            max_tool_calls=self._settings.agent_max_tool_calls,
        )
        return AgentRunResult(
            answer=shape.answer,
            model_call_count=shape.model_call_count,
            tool_round_count=shape.tool_round_count,
            tool_call_count=executed,
            refused_call_count=refused,
            stop_reason=stop_reason,
            limit_reasons=limit_reasons,
            tool_calls=shape.tool_calls,
            # Loop aggregate, not the last call's: FunctionInvocationLayer
            # sums every iteration's usage into the returned response
            # (`add_usage_details`), so this is the whole run's bill.
            usage=map_usage_details(response.usage_details),
            per_round=shape.per_round,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()
        await self._toolset.retriever.aclose()


def build_agent_service(settings: Settings, toolset: AgentToolset) -> AgentService:
    """Composition point: the only place that decides fake vs. real."""
    if settings.use_fake_llm:
        return FakeAgentService(toolset)
    return AgentFrameworkService(settings, toolset)
