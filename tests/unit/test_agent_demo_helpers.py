"""Pure helpers behind `tools/agent_demo.py`'s claims.

The demo's whole reason to exist is that a reader can re-run it, so the
logic its assertions rest on — trace normalization, the seed contract, the
latency source, redaction, mode atomicity — is testable here without a
provider, a network, or the script's `main()`.

`tools/` is not a package (no `__init__.py`, not installed), so the module
is loaded by path, the same pattern `tests/unit/test_index_recreate.py` and
`tests/unit/test_compare_retrieval.py` use.
"""

import argparse
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.agent_framework import AgentRoundMetrics, AgentRunResult

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "agent_demo.py"
_SPEC = importlib.util.spec_from_file_location("agent_demo", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
agent_demo = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = agent_demo
_SPEC.loader.exec_module(agent_demo)

build_redactions = agent_demo.build_redactions
normalized_post_ledger_trace = agent_demo.normalized_post_ledger_trace
normalized_trace = agent_demo.normalized_trace
redact = agent_demo.redact
resolve_settings = agent_demo.resolve_settings
seed_precondition_ok = agent_demo.seed_precondition_ok
tool_ms_from = agent_demo.tool_ms_from


def _call(name: str, args: dict[str, object]) -> dict[str, object]:
    return {
        "tool_name": name,
        "arguments": args,
        "arguments_canonical_json": json.dumps(args, sort_keys=True),
        "executed": True,
        "round_index": 1,
    }


def _result(per_round: tuple[AgentRoundMetrics, ...] | None) -> AgentRunResult:
    return AgentRunResult(
        answer="",
        model_call_count=2,
        tool_round_count=1,
        tool_call_count=1,
        refused_call_count=0,
        stop_reason="natural",
        limit_reasons=frozenset(),
        tool_calls=(),
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, reasoning_tokens=None),
        per_round=per_round,
    )


def test_post_ledger_trace_normalizes_conversation_id() -> None:
    trace_a = [
        _call("get_conversation_usage", {"conversation_id": "AAA"}),
        _call("search_docs", {"query": "429 remedy"}),
    ]
    trace_b = [
        _call("get_conversation_usage", {"conversation_id": "BBB"}),
        _call("search_docs", {"query": "429 remedy"}),
    ]
    # identical flows with different ids must compare EQUAL (spec §1: no
    # conversation-id false positives)
    assert normalized_post_ledger_trace(trace_a) == normalized_post_ledger_trace(trace_b)
    trace_c = [_call("get_conversation_usage", {"conversation_id": "AAA"})]
    assert normalized_post_ledger_trace(trace_a) != normalized_post_ledger_trace(trace_c)


def test_post_ledger_trace_without_a_ledger_call_is_a_sentinel() -> None:
    # Two traces that never called the ledger must compare EQUAL: "no ledger
    # call" is not branch evidence, and must never masquerade as a difference.
    no_ledger_a = [_call("search_docs", {"query": "a"})]
    no_ledger_b = [_call("search_docs", {"query": "b"}), _call("get_runtime_config", {})]
    assert normalized_post_ledger_trace(no_ledger_a) == normalized_post_ledger_trace(no_ledger_b)
    with_ledger = [_call("get_conversation_usage", {"conversation_id": "AAA"})]
    assert normalized_post_ledger_trace(no_ledger_a) != normalized_post_ledger_trace(with_ledger)


def test_post_ledger_trace_keeps_unparseable_arguments_distinguishable() -> None:
    # arguments=None means the model emitted arguments this project could not
    # parse; the canonical text is what stays comparable, and it must not
    # collapse into the zero-argument shape.
    unparseable = {
        "tool_name": "search_docs",
        "arguments": None,
        "arguments_canonical_json": "{not json",
        "executed": True,
        "round_index": 2,
    }
    trace = [_call("get_conversation_usage", {"conversation_id": "AAA"}), unparseable]
    no_args = [
        _call("get_conversation_usage", {"conversation_id": "AAA"}),
        _call("search_docs", {}),
    ]
    assert normalized_post_ledger_trace(trace) != normalized_post_ledger_trace(no_args)


def test_normalized_trace_detects_divergence_across_repeats() -> None:
    # The K-repeat divergence check compares the FULL trace, not the suffix.
    run_a = [_call("get_runtime_config", {}), _call("search_docs", {"query": "budget"})]
    run_b = [_call("get_runtime_config", {}), _call("search_docs", {"query": "budget"})]
    run_c = [_call("search_docs", {"query": "budget"}), _call("get_runtime_config", {})]
    assert normalized_trace(run_a) == normalized_trace(run_b)
    assert normalized_trace(run_a) != normalized_trace(run_c)


def test_seed_preconditions() -> None:
    assert seed_precondition_ok("fresh", spent=100, budget=400)
    assert not seed_precondition_ok("fresh", spent=320, budget=400)
    assert seed_precondition_ok("near_exhausted", spent=320, budget=400)
    assert not seed_precondition_ok("near_exhausted", spent=400, budget=400)  # exhausted
    assert not seed_precondition_ok("near_exhausted", spent=100, budget=400)


def test_seed_precondition_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        seed_precondition_ok("whatever", spent=1, budget=400)


def test_tool_ms_is_unavailable_rather_than_zero_when_per_round_is_none() -> None:
    # per_round is None whenever the execution-to-round join was rejected (and
    # always for the fake). Reporting 0.0 there would publish a measurement
    # that was never taken.
    assert tool_ms_from(_result(None)) is None
    assert tool_ms_from(_result((AgentRoundMetrics(1, None, None),))) is None


def test_tool_ms_sums_measured_rounds() -> None:
    per_round = (AgentRoundMetrics(1, 12.5, None), AgentRoundMetrics(2, 7.5, None))
    assert tool_ms_from(_result(per_round)) == 20.0


def test_redaction_masks_ids_endpoints_and_guids() -> None:
    settings = Settings(
        azure_openai_endpoint="https://aoai-azgenai-lab.openai.azure.com",
        azure_openai_deployment_name="chat-mini",
        azure_search_endpoint="https://srch-azgenai-lab.search.windows.net",
    )
    replacements = build_redactions(settings, near_id="near-uuid", fresh_id="fresh-uuid")
    payload = {
        "question": "Conversation near-uuid: why 429?",
        "nested": ["fresh-uuid", "https://aoai-azgenai-lab.openai.azure.com/openai/v1/"],
        "deployment": "chat-mini",
        "subscription": "da3f9b31-6cce-42a2-bece-61d13570a6e5",
        "count": 3,
    }
    redacted = redact(payload, replacements)
    assert redacted["question"] == "Conversation conv-near: why 429?"
    assert redacted["nested"][0] == "conv-fresh"
    assert "azgenai-lab" not in redacted["nested"][1]
    assert redacted["deployment"] != "chat-mini"
    assert redacted["subscription"] == "<redacted-guid>"
    assert redacted["count"] == 3  # non-strings pass through untouched


def test_provider_provenance_is_not_applicable_in_fake_mode() -> None:
    # A fake run measures nothing about the provider; a plausible-looking
    # model name here would be fabricated evidence.
    arguments = argparse.Namespace(
        model_name="gpt-5-mini",
        model_version="2025-08-07",
        context_window=400_000,
        context_window_source="https://example.invalid/docs",
        context_window_checked_at="2026-08-02",
    )
    fake = agent_demo.provider_provenance(arguments, live=False)
    assert set(fake.values()) == {"not_applicable"}
    live = agent_demo.provider_provenance(arguments, live=True)
    assert live["provider_model_name"] == "gpt-5-mini"
    assert live["context_window_tokens"] == 400_000
    assert live["context_window_source_checked_at"] == "2026-08-02"


async def test_recorder_preserves_tool_identity_and_captures_output() -> None:
    # The admission wrapper joins executions to rounds on `__name__`, and the
    # framework builds each tool's schema from its signature — a recorder that
    # lost either would break the trace silently.
    async def search_docs(query: str) -> str:
        """doc."""
        return f"result for {query}"

    sink: list[object] = []
    (wrapped,) = agent_demo.wrap_tools_with_recorder([search_docs], sink)
    assert wrapped.__name__ == "search_docs"
    assert str(inspect.signature(wrapped)) == "(query: str) -> str"
    assert await wrapped(query="sse") == "result for sse"
    assert [(r.tool_name, r.arguments, r.output) for r in sink] == [
        ("search_docs", {"query": "sse"}, "result for sse")
    ]


def test_mode_resolution_is_atomic() -> None:
    base = Settings(use_fake_llm=False, use_fake_search=True, use_fake_embeddings=False)
    fake = resolve_settings(base, live=False)
    assert (fake.use_fake_llm, fake.use_fake_search, fake.use_fake_embeddings) == (True, True, True)
    live = resolve_settings(base, live=True)
    assert (live.use_fake_llm, live.use_fake_search, live.use_fake_embeddings) == (
        False,
        False,
        False,
    )
