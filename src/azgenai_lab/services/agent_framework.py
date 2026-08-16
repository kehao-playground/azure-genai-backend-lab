"""Microsoft Agent Framework adapter (Day 17).

Framework types stop at this module's boundary: callers see the
AgentService Protocol and plain dataclasses, never agent_framework types
(the same rule Day 6 applied to Responses typed events).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import openai

from azgenai_lab.core.audit import AgentAuditTerminalSnapshot, AuditToolExecution
from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.principal import Principal
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.agent_tools import AgentToolDeps, AgentToolFn, bind_principal_tools
from azgenai_lab.services.azure_openai_auth import resolve_aoai_auth

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

    All-or-none for the required trio; reasoning maps when present. Both
    absence shapes -> None plus a log line (honest absence, Day 9 style) —
    never fabricated zeros. The two lines are worded distinctly because they
    mean different things: no usage block at all (nothing was reported) is
    not the same defect as a block that arrived missing a required count.
    """
    if details is None:
        logger.info("agent usage absent, dropping: response carried no usage block")
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
    # The Day 22 terminal snapshot (spec §4b): built by the adapter from the
    # same executions/tool_calls used to fill the fields above, so a
    # successful run's audit event and its AgentResponse always describe the
    # same trace, not two independently-derived ones.
    audit_snapshot: AgentAuditTerminalSnapshot


class AgentRunError(Exception):
    """Terminal loop failure. Carries usage aggregated before the failure
    when obtainable — a mid-loop failure has already been billed.

    ``audit_snapshot`` is required (Day 22, r6-4): every raise site has a
    degraded snapshot in hand (at minimum the executions observed so far),
    so there is no legitimate raise without one — the caller's audit event
    still needs *something* to report, honestly None past the point of
    failure rather than silently absent.
    """

    def __init__(
        self, message: str, *, usage: TokenUsage | None = None,
        audit_snapshot: AgentAuditTerminalSnapshot,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.audit_snapshot = audit_snapshot


def _close_transport_best_effort(
    client: openai.AsyncOpenAI, credential_aclose: Callable[[], Coroutine[Any, Any, None]]
) -> None:
    """Close a transport and its credential from synchronous code
    (partial-construction cleanup). `credential_aclose` is a no-op in
    api_key mode, so this always schedules both, no `None`-check needed.

    Best-effort only: `__init__` cannot await, and this runs while a real
    construction error is already propagating, so a failure here must never
    replace it — `contextlib.suppress` guarantees that. When no loop is
    running there is nothing to schedule the close on, so both are left for
    garbage collection instead: only two pure-sync constructors run between
    transport creation and here, so no request has been sent and neither the
    client's pool nor the credential's session holds a live connection to
    release.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop: connection-less client, dropped to GC
    with contextlib.suppress(Exception):
        loop.create_task(client.close())
    with contextlib.suppress(Exception):
        loop.create_task(credential_aclose())


@dataclass(frozen=True)
class AgentHistoryTurn:
    """App-owned conversation context for an agent run — projected from the
    client-visible transcript, never from opaque provider replay items."""

    role: Literal["user", "assistant"]
    text: str


class AgentService(Protocol):
    async def run(
        self,
        task: str,
        history: tuple[AgentHistoryTurn, ...],
        *,
        principal: Principal,
    ) -> AgentRunResult: ...

    async def aclose(self) -> None: ...


def _args_bytes(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> int:
    """UTF-8 length of the canonical [positional, keyword] serialization —
    covers positional AND keyword invocation, so the metric cannot silently
    under-count if the framework ever switches from its current kwargs-only
    tool dispatch. Byte length only; argument TEXT never reaches a log line
    (Day 14 redaction)."""
    return len(
        json.dumps([list(args), dict(kwargs)], sort_keys=True, default=str).encode(
            "utf-8"
        )
    )


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
                execution = ToolExecution(tool.__name__, executed=False, latency_ms=0.0)
                executions.append(execution)
                logger.info(
                    "agent_tool_execution name=%s seq=%d executed=%s "
                    "latency_ms=%.1f args_bytes=%d",
                    execution.tool_name,
                    len(executions),
                    execution.executed,
                    execution.latency_ms,
                    _args_bytes(args, kwargs),
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
                execution = ToolExecution(
                    tool.__name__,
                    executed=True,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
                executions.append(execution)
                logger.info(
                    "agent_tool_execution name=%s seq=%d executed=%s "
                    "latency_ms=%.1f args_bytes=%d",
                    execution.tool_name,
                    len(executions),
                    execution.executed,
                    execution.latency_ms,
                    _args_bytes(args, kwargs),
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

    `wrap_tools_with_admission` appends an executed call's record in the
    `finally`, i.e. at *completion*, and a refusal's before it returns the
    refusal constant, i.e. at *admission*. Those two orders are identical
    only while the run is sequential -- which is the primary mode here
    (`allow_multiple_tool_calls: False`) and precisely the assumption the
    guards below exist to protect. Tool-call contents are walked in message
    order above, so under that assumption index *i* of `executions`
    corresponds to index *i* of `tool_calls`. A length mismatch or a
    tool-name mismatch at the same index means the positional assumption
    doesn't hold here -- reject to `None` (honest absence) rather than
    attribute latency to the wrong round.
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


def _join_snapshot_executions(
    executions: Sequence[ToolExecution],
    joined_calls: Sequence[AgentToolCall] | None,
) -> tuple[AuditToolExecution, ...]:
    """Positional join of the audit-safe (name, executed) pair onto rounds.

    Joinability mirrors `_join_executions_to_rounds` exactly (r6-3a): same
    length AND every position's tool name matches. `joined_calls=None` covers
    the failure path, where the run never reached tool-call extraction at
    all. Anything not joinable -- length mismatch, a name mismatch at any
    position, or no calls to join against -- degrades every round_index to
    None rather than zipping two same-length sequences that don't actually
    correspond, which would be a plausible-looking lie, not a degraded
    trace."""
    joinable = (
        joined_calls is not None
        and len(joined_calls) == len(executions)
        and all(
            call.tool_name == execution.tool_name
            for call, execution in zip(joined_calls, executions, strict=True)
        )
    )
    if joinable:
        assert joined_calls is not None
        return tuple(
            AuditToolExecution(
                name=e.tool_name, executed=e.executed, round_index=c.round_index
            )
            for e, c in zip(executions, joined_calls, strict=True)
        )
    return tuple(
        AuditToolExecution(name=e.tool_name, executed=e.executed, round_index=None)
        for e in executions
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


# `agent_framework._tools._FUNCTION_INVOCATION_LIMIT_FALLBACK_TEXT`: the
# framework injects this exact sentence as the terminal answer on BOTH limit
# paths — the function-call limit and the iteration-exhaustion forced final
# (Phase 3) — whenever the stripped response has no other visible content.
# Pinned as our own constant so the boundary rule does not import a private
# upstream name at runtime; a lock test compares the two.
FRAMEWORK_FALLBACK_TEXT = (
    "Function invocation limit reached before a final answer could be produced."
)


def strip_framework_fallback(
    answer: str,
    stop_reason: Literal["natural", "iteration_limit", "function_call_limit"],
) -> str:
    """Day 6 boundary: the framework's hardcoded English fallback never leaves
    the adapter. Exact equality only — the framework injects the full constant
    as the sole visible content or not at all, so substring surgery could only
    ever eat model-authored text. A natural stop never strips: equality there
    would mean the model itself typed the sentence."""
    if stop_reason != "natural" and answer == FRAMEWORK_FALLBACK_TEXT:
        return ""
    return answer


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

    def __init__(self, deps: AgentToolDeps, *, prompt: PromptTemplate) -> None:
        self._deps = deps
        self._prompt = prompt
        self.last_history: tuple[AgentHistoryTurn, ...] | None = None
        self.last_principal: Principal | None = None
        self._closed = False

    async def run(
        self,
        task: str,
        history: tuple[AgentHistoryTurn, ...],
        *,
        principal: Principal,
    ) -> AgentRunResult:
        validate_task(task)
        self.last_history = history
        self.last_principal = principal
        tools = bind_principal_tools(self._deps, principal)
        tool_calls: list[AgentToolCall] = []
        outputs: list[str] = []
        # Accumulated alongside tool_calls, same loop, same order — so the
        # join below is exercised for real (not a parallel, independently
        # true-by-construction source) even though this adapter never talks
        # to a provider.
        executions: list[ToolExecution] = []
        for tool in tools:
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
            executions.append(ToolExecution(tool_name=name, executed=True, latency_ms=0.0))
        usage = TokenUsage(
            input_tokens=20, output_tokens=10, total_tokens=30, reasoning_tokens=0
        )
        # The stop shape below is hardcoded, and it is honest only by
        # coincidence at the *default* constants: the fake makes one call per
        # tool in one round (3 <= AGENT_MAX_TOOL_CALLS = 10) and reports two
        # model calls (2 <= AGENT_MAX_ITERATIONS + 1 = 6), so `derive_stop`
        # would return exactly this. The fake applies no admission counter and
        # holds no Settings, so lowering either limit (e.g.
        # AGENT_MAX_TOOL_CALLS=2) leaves it still reporting a natural stop with
        # zero refusals while `get_runtime_config` tells the model the limit is
        # 2. Deriving these would mean giving the fake the settings it
        # deliberately does not take; the applied-guardrail claim is narrowed at
        # its source instead (see `make_get_runtime_config`).
        return AgentRunResult(
            answer=(
                f"[fake-agent] {task} (history={len(history)}) "
                f"(tools={len(tool_calls)}) " + " ".join(outputs)
            ),
            model_call_count=2,  # one tool round + one final answer
            tool_round_count=1,
            tool_call_count=len(tool_calls),
            refused_call_count=0,
            stop_reason="natural",
            limit_reasons=frozenset(),
            tool_calls=tuple(tool_calls),
            usage=usage,
            per_round=None,
            audit_snapshot=AgentAuditTerminalSnapshot(
                provider_call_attempted=True,
                executions=_join_snapshot_executions(executions, tool_calls),
                model_calls=2,
                tool_call_count=len(tool_calls),
                refused_call_count=0,
                stop_reason="natural",
                usage=usage,
            ),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._deps.retriever.aclose()


def _to_framework_messages(
    history: tuple[AgentHistoryTurn, ...], task: str
) -> list[Any]:
    """Assembled inside the adapter so upstream types never cross the
    boundary: [...history, current task], no session — the pinned framework's
    Agent.run(messages=...) with session=None is documented stateless."""
    from agent_framework import Message as FrameworkMessage

    items: list[Any] = [
        FrameworkMessage(turn.role, [turn.text]) for turn in history
    ]
    items.append(FrameworkMessage("user", [task]))
    return items


class AgentFrameworkService:
    """Real adapter over the pinned Microsoft Agent Framework.

    Sequential tool mode (allow_multiple_tool_calls=False -> provider
    parallel_tool_calls=false) is the primary cost bound; the per-run
    admission counter is defense-in-depth; the framework's own
    max_function_calls is the third layer (between-batch, best-effort).
    """

    def __init__(
        self, settings: Settings, deps: AgentToolDeps, *, prompt: PromptTemplate
    ) -> None:
        if not (settings.azure_openai_endpoint and settings.azure_openai_deployment_name):
            raise ValueError(
                "USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT_NAME"
            )
        self._settings = settings
        self._deps = deps
        self._prompt = prompt
        self._closed = False
        # Project-owned transport: same timeout/retry policy as the chat
        # adapter — the framework never reads its own env vars here.
        auth = resolve_aoai_auth(settings)  # raises ValueError per mode (Day 24 keyless)
        # entra mode only: closes the ManagedIdentityCredential backing
        # `self._client`'s callable api_key. A no-op in api_key mode. Set
        # before the transport so the partial-construction guard below can
        # close it too — see azure_openai_auth.py's docstring on why it
        # must be closed at all.
        self._credential_aclose = auth.aclose
        self._client = openai.AsyncOpenAI(
            api_key=auth.api_key,  # str | async Callable — matches AsyncOpenAI's own type
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
            _close_transport_best_effort(self._client, self._credential_aclose)
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

    async def run(
        self,
        task: str,
        history: tuple[AgentHistoryTurn, ...],
        *,
        principal: Principal,
    ) -> AgentRunResult:
        validate_task(task)
        state = AdmissionState(limit=self._settings.agent_max_tool_calls)
        executions: list[ToolExecution] = []
        wrapped = wrap_tools_with_admission(
            bind_principal_tools(self._deps, principal), state, executions
        )
        start = time.perf_counter()
        result: AgentRunResult | None = None
        try:
            try:
                response = await self._agent.run(
                    _to_framework_messages(history, task), tools=wrapped
                )
            except Exception as exc:  # framework/provider terminal failure
                # usage=None is the honest value, not an omission: the framework's
                # loop keeps its running `aggregated_usage` as a function local and
                # attaches it only to a response it returns, never to anything it
                # raises. A mid-loop failure has already been billed for the calls
                # that completed, but that number is discarded above us and there is
                # no supported way to reach it (Day 9: never fabricate counts).
                # The audit snapshot mirrors that honesty at the trace level: the
                # run never reached tool-call extraction, so there is no
                # joined_calls to join against -- every execution's round_index
                # degrades to None rather than a guessed one.
                raise AgentRunError(
                    str(exc), usage=None,
                    audit_snapshot=AgentAuditTerminalSnapshot(
                        provider_call_attempted=True,
                        executions=_join_snapshot_executions(executions, None),
                        model_calls=None, tool_call_count=None, refused_call_count=None,
                        stop_reason=None, usage=None,
                    ),
                ) from exc
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
            run_usage = map_usage_details(response.usage_details)
            result = AgentRunResult(
                answer=strip_framework_fallback(shape.answer, stop_reason),
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
                usage=run_usage,
                per_round=shape.per_round,
                audit_snapshot=AgentAuditTerminalSnapshot(
                    provider_call_attempted=True,
                    executions=_join_snapshot_executions(executions, shape.tool_calls),
                    model_calls=shape.model_call_count,
                    tool_call_count=executed,
                    refused_call_count=refused,
                    stop_reason=stop_reason,
                    usage=run_usage,
                ),
            )
            return result
        finally:
            # Summary lives here because only the adapter owns prompt
            # provenance, partial tool executions and the run clock. On
            # failure the framework exposes no counts, stop or usage —
            # reported as unavailable, never as 0/natural (Day 9).
            duration_ms = (time.perf_counter() - start) * 1000
            if result is not None:
                logger.info(
                    "agent_run_summary model_calls=%d tool_calls=%d refused=%d stop=%s "
                    "total_tokens=%s duration_ms=%.1f prompt_name=%s "
                    "prompt_version=%d prompt_sha256=%s",
                    result.model_call_count,
                    result.tool_call_count,
                    result.refused_call_count,
                    result.stop_reason,
                    result.usage.total_tokens if result.usage else None,
                    duration_ms,
                    self._prompt.name,
                    self._prompt.version,
                    self._prompt.sha256,
                )
            else:
                logger.info(
                    "agent_run_summary model_calls=unavailable stop=unavailable "
                    "usage=unavailable tools_executed=%d duration_ms=%.1f "
                    "prompt_name=%s prompt_version=%d prompt_sha256=%s "
                    "(upstream may have incurred billable processing)",
                    sum(1 for e in executions if e.executed),
                    duration_ms,
                    self._prompt.name,
                    self._prompt.version,
                    self._prompt.sha256,
                )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Three handles are owned from construction, so all three must be
        # released: a raising close must not strand either of the other two
        # (`__init__` already guards the client+credential pair for the
        # partial-construction case). `_closed` is set above, so this stays
        # idempotent whichever close raises. No None-check on the credential
        # closer: it's always a valid awaitable, a no-op in api_key mode.
        try:
            try:
                await self._client.close()
            finally:
                await self._credential_aclose()
        finally:
            await self._deps.retriever.aclose()


def build_agent_service(
    settings: Settings, deps: AgentToolDeps, *, prompt: PromptTemplate
) -> AgentService:
    """Composition point: the only place that decides fake vs. real. `prompt`
    is loaded once by the caller (build_agent_turn_service) and handed to
    whichever adapter is built here -- the same instance also feeds
    build_audit_attribution, so the attribution reports what the adapter
    actually holds (Day 22), not a second load of the same file."""
    if settings.use_fake_llm:
        return FakeAgentService(deps, prompt=prompt)
    return AgentFrameworkService(settings, deps, prompt=prompt)


__all__ = [
    "AGENT_MAX_TASK_BYTES",
    "REFUSAL_MESSAGE",
    "AdmissionState",
    "AgentFrameworkService",
    "AgentHistoryTurn",
    "AgentRoundMetrics",
    "AgentRunError",
    "AgentRunResult",
    "AgentService",
    "AgentTaskTooLargeError",
    "AgentToolCall",
    "FRAMEWORK_FALLBACK_TEXT",
    "FakeAgentService",
    "ToolExecution",
    "build_agent_service",
    "derive_stop",
    "extract_run_shape",
    "map_usage_details",
    "strip_framework_fallback",
    "validate_task",
    "wrap_tools_with_admission",
]
