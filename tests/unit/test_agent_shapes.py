"""Tests for the Day 17 agent service contract: task boundary, data shapes,
and the UsageDetails -> TokenUsage mapping (Task 8)."""

import pytest

from azgenai_lab.services.agent_framework import (
    AGENT_MAX_TASK_BYTES,
    FRAMEWORK_FALLBACK_TEXT,
    AgentTaskTooLargeError,
    map_usage_details,
    strip_framework_fallback,
    validate_task,
)


def test_task_cap_is_utf8_bytes_not_chars() -> None:
    assert validate_task("a" * 4000) == "a" * 4000  # 4000 bytes: accepted
    with pytest.raises(AgentTaskTooLargeError):
        validate_task("a" * 4001)  # 4001 bytes: rejected
    cjk = "算" * 1400  # 1400 chars = 4200 bytes
    assert len(cjk) < AGENT_MAX_TASK_BYTES  # char count would pass...
    with pytest.raises(AgentTaskTooLargeError):
        validate_task(cjk)  # ...but bytes reject it


def test_empty_or_whitespace_task_rejected() -> None:
    for bad in ("", "   ", "\n\t"):
        with pytest.raises(AgentTaskTooLargeError):
            validate_task(bad)


def test_usage_mapping_complete_block() -> None:
    usage = map_usage_details(
        {
            "input_token_count": 30,
            "output_token_count": 79,
            "total_token_count": 109,
            "reasoning_output_token_count": 64,
        }
    )
    assert usage is not None
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.reasoning_tokens,
    ) == (30, 79, 109, 64)


@pytest.mark.parametrize(
    "details",
    [
        None,
        {},
        {"input_token_count": 30},  # missing required counts
        {"input_token_count": 30, "output_token_count": 79},  # missing total
    ],
)
def test_usage_mapping_all_or_none(details: dict[str, int] | None) -> None:
    assert map_usage_details(details) is None


def test_usage_mapping_reasoning_optional() -> None:
    usage = map_usage_details(
        {"input_token_count": 10, "output_token_count": 5, "total_token_count": 15}
    )
    assert usage is not None and usage.reasoning_tokens is None


def test_strip_on_function_call_limit() -> None:
    assert strip_framework_fallback(FRAMEWORK_FALLBACK_TEXT, "function_call_limit") == ""


def test_strip_on_iteration_limit() -> None:
    assert strip_framework_fallback(FRAMEWORK_FALLBACK_TEXT, "iteration_limit") == ""


def test_natural_stop_never_strips() -> None:
    assert (
        strip_framework_fallback(FRAMEWORK_FALLBACK_TEXT, "natural")
        == FRAMEWORK_FALLBACK_TEXT
    )


def test_real_content_never_stripped_even_on_limit() -> None:
    assert strip_framework_fallback("Real answer.", "iteration_limit") == "Real answer."


def test_pinned_text_matches_framework_constant() -> None:
    from agent_framework._tools import _FUNCTION_INVOCATION_LIMIT_FALLBACK_TEXT

    assert FRAMEWORK_FALLBACK_TEXT == _FUNCTION_INVOCATION_LIMIT_FALLBACK_TEXT
