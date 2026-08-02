"""Microsoft Agent Framework adapter (Day 17).

Framework types stop at this module's boundary: callers see the
AgentService Protocol and plain dataclasses, never agent_framework types
(the same rule Day 6 applied to Responses typed events).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from azgenai_lab.models.chat import TokenUsage

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
