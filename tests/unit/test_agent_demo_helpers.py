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

assert_suite = agent_demo.assert_suite
branch_evidence = agent_demo.branch_evidence
build_redactions = agent_demo.build_redactions
ledger_figures = agent_demo.ledger_figures
normalized_post_ledger_trace = agent_demo.normalized_post_ledger_trace
normalized_trace = agent_demo.normalized_trace
redact = agent_demo.redact
resolve_settings = agent_demo.resolve_settings
seed_precondition_ok = agent_demo.seed_precondition_ok
summarize_assertions = agent_demo.summarize_assertions
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


def _record(label: str, answer: str, trace: list[dict[str, object]]) -> object:
    return agent_demo.RunRecord(
        label=label,
        question="q",
        answer=answer,
        trace=trace,
        tool_outputs=[],
        measurements={},
        shape={},
    )


def _seed_outcome() -> object:
    return agent_demo.SeedOutcome(
        near_id="near-uuid",
        fresh_id="fresh-uuid",
        near_spent=320,
        fresh_spent=30,
        near_turns=3,
    )


_LEDGER = "get_conversation_usage"
_CONFIG = "get_runtime_config"
_SEARCH = "search_docs"


def _passing_records(settings: Settings) -> dict[str, object]:
    """A record set where every live assertion has something real to hold."""
    return {
        "config-only": _record(
            "config-only",
            f"The cap is {settings.llm_max_output_tokens} output tokens.",
            [_call(_CONFIG, {})],
        ),
        "docs-only": _record("docs-only", "ignore unknown event names", [_call(_SEARCH, {})]),
        "config+docs": _record(
            "config+docs", "The budget is 400 tokens.", [_call(_CONFIG, {}), _call(_SEARCH, {})]
        ),
        # near: ledger figures 320 spent / 80 remaining, plus the 429 wording
        "diagnostic-near": _record(
            "diagnostic-near",
            "It has spent 320 tokens and has 80 left, so the next call is rejected with 429.",
            [_call(_LEDGER, {"conversation_id": "near-uuid"}), _call(_SEARCH, {"query": "429"})],
        ),
        # fresh: ledger figures 30 spent / 370 remaining
        "diagnostic-fresh": _record(
            "diagnostic-fresh",
            "It has spent 30 tokens and can still spend 370.",
            [_call(_LEDGER, {"conversation_id": "fresh-uuid"})],
        ),
        "no-hit": _record(
            "no-hit",
            "I found no supporting evidence in the documentation.",
            [_call(_SEARCH, {"query": "a"}), _call(_SEARCH, {"query": "b"})],
        ),
    }


def _outcomes(assertions: list[object]) -> dict[str, str]:
    return {a.name: a.outcome for a in assertions}  # type: ignore[attr-defined]


def test_ledger_figures_are_the_two_numbers_only_the_ledger_supplies() -> None:
    assert ledger_figures(320, 400) == {"320", "80"}


def test_branch_evidence_prefers_full_trace_divergence() -> None:
    # The divergence must be found over the FULL trace: nothing forces the
    # ledger call to come first, so the post-ledger suffix alone can be empty
    # on both sides of a genuinely different run.
    near_trace = [_call(_SEARCH, {"query": "429"}), _call(_LEDGER, {"conversation_id": "A"})]
    fresh_trace = [_call(_LEDGER, {"conversation_id": "B"})]
    evidence = branch_evidence(
        near_trace=near_trace,
        fresh_trace=fresh_trace,
        near_answer="",
        fresh_answer="",
        near_figures={"320", "80"},
        fresh_figures={"30", "370"},
    )
    assert evidence.form == "trace_divergence"


def test_branch_evidence_never_counts_two_ledgerless_runs() -> None:
    # NO_LEDGER_SENTINEL's protection, carried over to the full-trace compare:
    # two runs that never consulted the ledger cannot be evidence of a
    # ledger-driven branch however different their traces look.
    evidence = branch_evidence(
        near_trace=[_call(_SEARCH, {"query": "a"})],
        fresh_trace=[_call(_CONFIG, {})],
        near_answer="",
        fresh_answer="",
        near_figures={"320", "80"},
        fresh_figures={"30", "370"},
    )
    assert evidence.form == "none"


def test_branch_evidence_accepts_answer_content_divergence() -> None:
    # Identical traces (search_docs -> ledger -> answer is plausible for both
    # diagnostics), but each answer carries its own conversation's numbers.
    trace_shape = [_call(_SEARCH, {"query": "429"}), _call(_LEDGER, {"conversation_id": "X"})]
    evidence = branch_evidence(
        near_trace=trace_shape,
        fresh_trace=[_call(_SEARCH, {"query": "429"}), _call(_LEDGER, {"conversation_id": "Y"})],
        near_answer="spent 320, only 80 left",
        fresh_answer="spent 30, 370 still available",
        near_figures={"320", "80"},
        fresh_figures={"30", "370"},
    )
    assert evidence.form == "answer_content_divergence"
    assert "320" in evidence.detail


def test_branch_evidence_reports_no_branch_rather_than_failing() -> None:
    trace_shape = [_call(_LEDGER, {"conversation_id": "X"})]
    evidence = branch_evidence(
        near_trace=trace_shape,
        fresh_trace=[_call(_LEDGER, {"conversation_id": "Y"})],
        near_answer="the budget may be exhausted",
        fresh_answer="the budget may be exhausted",
        near_figures={"320", "80"},
        fresh_figures={"30", "370"},
    )
    assert evidence.form == "none"
    assert "identical" in evidence.detail


def test_near_answer_direction_is_not_satisfied_by_echoing_the_question() -> None:
    # DIAGNOSTIC_TEMPLATE itself contains "429": restating the question must
    # not pass an assertion that claims the agent read the ledger.
    settings = Settings()
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        agent_demo.DIAGNOSTIC_TEMPLATE.format(cid="near-uuid"),
        records["diagnostic-near"].trace,  # type: ignore[attr-defined]
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert outcomes["diagnostic-near:answer_direction"] == "failed"


def test_near_answer_direction_passes_on_ledger_figure_without_429_wording() -> None:
    # The pass condition is the ledger-derived figure alone; "429"/"exhaust"
    # wording is a recorded signal, not a requirement. A live answer phrased
    # without that vocabulary but with the correct numbers must still pass —
    # otherwise a paid live run fails on a wording preference, not a defect.
    settings = Settings()
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        "It has spent 320 tokens and has 80 remaining.",
        records["diagnostic-near"].trace,  # type: ignore[attr-defined]
    )
    assertions = assert_suite(records, settings, _seed_outcome(), live=True)
    outcomes = _outcomes(assertions)
    assert outcomes["diagnostic-near:answer_direction"] == "passed"
    (assertion,) = [a for a in assertions if a.name == "diagnostic-near:answer_direction"]
    assert "not observed" in assertion.detail  # type: ignore[attr-defined]


def test_live_suite_passes_when_the_answers_carry_ledger_figures() -> None:
    settings = Settings()
    assertions = assert_suite(_passing_records(settings), settings, _seed_outcome(), live=True)
    outcomes = _outcomes(assertions)
    assert set(outcomes.values()) == {"passed"}


def test_branch_evidence_degrades_to_no_branch_observed_not_to_failure() -> None:
    # A plausible correct run with nothing to distinguish: the same trace on
    # both sides (rule 3 makes search_docs -> ledger -> answer plausible for
    # both diagnostics) and, at spent=320/80, ledger figure sets that
    # coincide. Both answers are grounded, yet no branch is observable — that
    # must be recorded as unverified, never as a failure.
    settings = Settings()
    records = _passing_records(settings)
    seed_outcome = agent_demo.SeedOutcome(
        near_id="near-uuid", fresh_id="fresh-uuid", near_spent=320, fresh_spent=80, near_turns=3
    )
    trace = records["diagnostic-near"].trace  # type: ignore[attr-defined]
    records["diagnostic-fresh"] = _record(
        "diagnostic-fresh", "It has spent 80 tokens and can still spend 320.", trace
    )
    assertions = assert_suite(records, settings, seed_outcome, live=True)
    outcomes = _outcomes(assertions)
    assert outcomes["diagnostic:branch_evidence"] == "no_branch_observed"
    assert "failed" not in outcomes.values()
    (branch,) = [a for a in assertions if a.name == "diagnostic:branch_evidence"]
    assert branch.evidence_form == "none"  # type: ignore[attr-defined]


def test_assert_suite_fake_live_split_is_exactly_the_model_behaviour_claims() -> None:
    # The authorized fake/live deviation rests on this property: fake mode
    # skips the four model-behaviour claims and asserts everything else.
    settings = Settings()
    records = _passing_records(settings)
    fake = _outcomes(assert_suite(records, settings, _seed_outcome(), live=False))
    expected_skipped = {
        "diagnostic:branch_evidence",
        "diagnostic-near:answer_direction",
        "diagnostic-fresh:answer_direction",
        "no-hit:answer_admits_missing_evidence",
    }
    assert {name for name, outcome in fake.items() if outcome == "skipped_fake"} == expected_skipped
    assert not expected_skipped & {name for name, outcome in fake.items() if outcome == "passed"}
    live = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert "skipped_fake" not in live.values()
    assert set(live) == set(fake)


def test_summary_never_claims_all_passed_when_something_was_unverified() -> None:
    passed = [agent_demo.Assertion("a", "passed", "")]
    assert summarize_assertions(passed) == "all assertions passed (1 passed)"
    mixed = [*passed, agent_demo.Assertion("b", "skipped_fake", "")]
    line = summarize_assertions(mixed)
    assert "all assertions passed" not in line
    assert "1 unverified" in line and "--live" in line
    unobserved = [*passed, agent_demo.Assertion("b", "no_branch_observed", "")]
    line = summarize_assertions(unobserved)
    assert "all assertions passed" not in line
    # a live run must not be told anything about fake mode
    assert "--live" not in line


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
