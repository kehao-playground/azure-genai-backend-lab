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

from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.agent_tools import AgentToolFn

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
                results_by_call_id[str(call_id)] = str(_content_attr(content, "result"))

    tool_calls: list[AgentToolCall] = []
    per_round_usage: list[TokenUsage | None] = []
    assistant_ordinal = 0
    last_assistant_text = ""
    tool_round_count = 0
    for message in response_messages:
        if message.role != "assistant":
            continue
        assistant_ordinal += 1
        round_usage: TokenUsage | None = None
        texts: list[str] = []
        saw_call = False
        for content in message.contents:
            if content.type == "text":
                texts.append(str(_content_attr(content, "text") or ""))
            elif content.type == "usage":
                round_usage = map_usage_details(_content_attr(content, "usage_details"))
            elif content.type == "function_call":
                saw_call = True
                raw = _content_attr(content, "arguments") or ""
                if isinstance(raw, Mapping):
                    parsed: Mapping[str, Any] | None = dict(raw)
                    canonical = json.dumps(parsed, sort_keys=True)
                else:
                    canonical = str(raw)
                    try:
                        parsed = json.loads(canonical)
                    except (ValueError, TypeError):
                        parsed = None
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
        if saw_call:
            tool_round_count += 1
        last_assistant_text = "".join(texts) if texts else last_assistant_text
        per_round_usage.append(round_usage)

    per_round: tuple[AgentRoundMetrics, ...] | None
    if any(usage is not None for usage in per_round_usage):
        per_round = tuple(
            AgentRoundMetrics(round_index=i + 1, latency_ms=None, usage=usage)
            for i, usage in enumerate(per_round_usage)
        )
    else:
        per_round = None  # no per-response signal at these pins — never fabricated

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
    limits fire; limit_reasons carries every limit that fired."""
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
