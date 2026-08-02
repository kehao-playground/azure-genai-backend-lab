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
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.prompts.loader import load_prompt
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
comparison_redactions = agent_demo.comparison_redactions
ledger_figures = agent_demo.ledger_figures
normalized_post_ledger_trace = agent_demo.normalized_post_ledger_trace
normalized_trace = agent_demo.normalized_trace
redact = agent_demo.redact
resolve_settings = agent_demo.resolve_settings
seed_precondition_ok = agent_demo.seed_precondition_ok
summarize_assertions = agent_demo.summarize_assertions
tool_ms_from = agent_demo.tool_ms_from


def _settings(**overrides: Any) -> Settings:
    """Settings isolated from the repo-root `.env`.

    Same hazard `_run_demo` documents for the subprocess run, on this side of
    the process boundary: `Settings` reads `.env` relative to the working
    directory, `conftest.py` pins only the three fake-adapter flags, and
    several assertions below depend on the *default* values of fields nobody
    pins (`llm_max_output_tokens == 1000` is asserted outright). A developer's
    untracked `.env` would fail them in a way no fresh clone or CI checkout
    reproduces.
    """
    return Settings(_env_file=None, **overrides)


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
    settings = _settings(
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
    assert redacted["deployment"] == "<redacted>"  # the mask, not merely "changed"
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


def test_agent_prompt_provenance_is_not_applicable_in_fake_mode() -> None:
    # `FakeAgentService` applies no prompt at all, so recording the loaded
    # ops_agent values under `run_conditions` would read as the agent's own
    # provenance for a run that had none. The prompt still reaches the
    # baseline call in both modes — that copy is recorded under `baseline`.
    prompt = load_prompt("ops_agent")
    fake = agent_demo.agent_prompt_provenance(prompt, live=False)
    assert set(fake) == {"agent_prompt_name", "agent_prompt_version", "agent_prompt_sha256"}
    assert set(fake.values()) == {"not_applicable"}
    live = agent_demo.agent_prompt_provenance(prompt, live=True)
    assert live["agent_prompt_name"] == prompt.name
    assert live["agent_prompt_version"] == prompt.version
    assert live["agent_prompt_sha256"] == prompt.sha256


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


# Realistic seeded conversation ids. Both deliberately CONTAIN every ledger
# figure these tests use ("320", "80", "30", "370") as a bare substring, which
# is exactly the trap the real ids set: the seed contract forces spent into
# [320, 400), so the remainder is one or two digits, and a short digit run
# appears somewhere inside a 32-hex-character uuid most of the time. An id
# made of letters only ("near-uuid") cannot catch a substring match.
_NEAR_ID = "3208030f-3702-4bd6-8f23-d348fdb5a694"
_FRESH_ID = "3703080f-3202-4bd6-8f23-d348fdb5a694"


def _comparison_replacements() -> list[tuple[str, str]]:
    return comparison_redactions(_settings(), near_id=_NEAR_ID, fresh_id=_FRESH_ID)  # type: ignore[no-any-return]


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
        near_id=_NEAR_ID,
        fresh_id=_FRESH_ID,
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
            [_call(_LEDGER, {"conversation_id": _NEAR_ID}), _call(_SEARCH, {"query": "429"})],
        ),
        # fresh: ledger figures 30 spent / 370 remaining
        "diagnostic-fresh": _record(
            "diagnostic-fresh",
            "It has spent 30 tokens and can still spend 370.",
            [_call(_LEDGER, {"conversation_id": _FRESH_ID})],
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
        replacements=_comparison_replacements(),
    )
    assert evidence.form == "trace_divergence"


def test_branch_evidence_ignores_a_conversation_id_inside_an_argument_value() -> None:
    # Stripping the `conversation_id` KEY is not enough: the diagnostic question
    # names the conversation, so the id plausibly travels inside another
    # argument's value. Two traces differing only by which id they carry are
    # the same behaviour and must not be reported as trace_divergence — the
    # strongest evidence form, the one an article would quote.
    near_trace = [
        _call(_LEDGER, {"conversation_id": _NEAR_ID}),
        _call(_SEARCH, {"query": f"conversation {_NEAR_ID} 429"}),
    ]
    fresh_trace = [
        _call(_LEDGER, {"conversation_id": _FRESH_ID}),
        _call(_SEARCH, {"query": f"conversation {_FRESH_ID} 429"}),
    ]
    evidence = branch_evidence(
        near_trace=near_trace,
        fresh_trace=fresh_trace,
        near_answer="",
        fresh_answer="",
        near_figures={"320", "80"},
        fresh_figures={"30", "370"},
        replacements=_comparison_replacements(),
    )
    assert evidence.form == "none"


def test_branch_evidence_answer_channel_ignores_an_echoed_conversation_id() -> None:
    # Same defect on the answer channel: two ungrounded answers, each merely
    # echoing its own conversation id, must not supply figure "hits" and turn
    # into answer_content_divergence.
    trace = [_call(_LEDGER, {"conversation_id": "X"})]
    evidence = branch_evidence(
        near_trace=trace,
        fresh_trace=trace,
        near_answer=f"Conversation {_NEAR_ID}: it may be rejected with 429.",
        fresh_answer=f"Conversation {_FRESH_ID}: it may be rejected with 429.",
        near_figures={"320", "80"},
        fresh_figures={"30", "370"},
        replacements=_comparison_replacements(),
    )
    assert evidence.form == "none"


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
        replacements=_comparison_replacements(),
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
        replacements=_comparison_replacements(),
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
        replacements=_comparison_replacements(),
    )
    assert evidence.form == "none"
    assert "identical" in evidence.detail


def test_near_answer_direction_is_not_satisfied_by_echoing_the_question() -> None:
    # DIAGNOSTIC_TEMPLATE itself contains "429" AND the conversation id:
    # restating the question must not pass an assertion that claims the agent
    # read the ledger.
    settings = _settings()
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        agent_demo.DIAGNOSTIC_TEMPLATE.format(cid=_NEAR_ID),
        records["diagnostic-near"].trace,  # type: ignore[attr-defined]
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert outcomes["diagnostic-near:answer_direction"] == "failed"


def test_answer_direction_is_not_satisfied_by_an_echoed_conversation_id() -> None:
    # The ledger figures are short (remainder in [1, 80] by the seed contract),
    # so a bare substring test finds them inside the conversation uuid the
    # answer echoes back. An answer carrying the id and no ledger content is
    # ungrounded on both sides and must fail on both sides.
    settings = _settings()
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        f"Conversation {_NEAR_ID}: it may be rejected with 429.",
        records["diagnostic-near"].trace,  # type: ignore[attr-defined]
    )
    records["diagnostic-fresh"] = _record(
        "diagnostic-fresh",
        f"Conversation {_FRESH_ID}: it can keep going.",
        records["diagnostic-fresh"].trace,  # type: ignore[attr-defined]
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert outcomes["diagnostic-near:answer_direction"] == "failed"
    assert outcomes["diagnostic-fresh:answer_direction"] == "failed"


def test_answer_direction_requires_the_figure_on_a_word_boundary() -> None:
    # A digit run inside a longer number is not the ledger figure: "320" inside
    # "4320" and "80" inside "1809" say nothing about what the agent read.
    settings = _settings()
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        "The conversation ran 4320 milliseconds and holds 1809 characters.",
        records["diagnostic-near"].trace,  # type: ignore[attr-defined]
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert outcomes["diagnostic-near:answer_direction"] == "failed"


def test_config_only_answer_contains_llm_max_output_tokens_requires_word_boundary() -> None:
    # Same defect class as the diagnostic figures above, on the other pair of
    # assertions: a digit run inside a longer number is not the configured
    # max-output-tokens figure. "1000" inside "10000" says nothing about what
    # get_runtime_config returned.
    settings = _settings()
    records = _passing_records(settings)
    records["config-only"] = _record(
        "config-only",
        "The service enforces a ceiling of 10000 output tokens per call.",
        records["config-only"].trace,  # type: ignore[attr-defined]
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=False))
    assert outcomes["config-only:answer_contains_llm_max_output_tokens"] == "failed"


def test_config_docs_answer_contains_demo_token_budget_requires_word_boundary() -> None:
    # Embedded-digit sub-case only: "400" hidden inside a longer number
    # ("4000-series") is not the budget figure. This says nothing about the
    # *standalone* HTTP-400 collision, which word-boundary matching cannot
    # reject by construction — see the known-gap test below.
    settings = _settings()
    records = _passing_records(settings)
    records["config+docs"] = _record(
        "config+docs",
        "It documents an HTTP 4000-series response for invalid input, but "
        "never states the token budget.",
        records["config+docs"].trace,  # type: ignore[attr-defined]
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=False))
    assert outcomes["config+docs:answer_contains_demo_token_budget"] == "failed"


def test_config_docs_budget_assertion_accepts_a_standalone_http_400_known_gap() -> None:
    """Pins a KNOWN-ACCEPTED gap rather than a rejection.

    `DEMO_TOKEN_BUDGET` is 400, which is also an HTTP status code. An answer
    that says "a malformed request comes back as 400 invalid_input" states a
    standalone `400` on a word boundary, and no boundary rule can tell it
    apart from the budget figure. The constant is spec-fixed and a
    wording-proximity heuristic was removed from this file twice already, so
    the gap is recorded here instead of papered over: this answer passes.

    What keeps the claim honest is that the assertion is documented as a
    corroborating signal, and the structural proof that the agent read
    `get_runtime_config` is `config+docs:runtime_config_executed`, asserted
    in both modes. (The demo's own corpus, `data/sample-docs/opsdemo/`,
    contains no `400` at all — the collision can only come from the model's
    general HTTP knowledge.)
    """
    settings = _settings()
    records = _passing_records(settings)
    records["config+docs"] = _record(
        "config+docs",
        "A malformed request comes back as 400 invalid_input.",
        records["config+docs"].trace,  # type: ignore[attr-defined]
    )
    assertions = assert_suite(records, settings, _seed_outcome(), live=False)
    outcomes = _outcomes(assertions)
    assert outcomes["config+docs:answer_contains_demo_token_budget"] == "passed"
    (budget_assertion,) = [
        a for a in assertions if a.name == "config+docs:answer_contains_demo_token_budget"
    ]
    # the detail must own the residual, not hide it
    detail = budget_assertion.detail  # type: ignore[attr-defined]
    assert "HTTP status" in detail
    assert "config+docs:runtime_config_executed" in detail


def test_config_answer_matching_neutralizes_the_conversation_ids() -> None:
    # The config questions carry no conversation id today, so this is
    # defensive — but a seeded uuid whose group is literally "1000"-shaped
    # produces a `\b`-delimited digit run, and an answer that merely echoed
    # that id would otherwise "state" the configured max-output figure. The
    # budget assertion gets the identical treatment for the same reason; a
    # three-digit run cannot be `\b`-delimited inside a dashed uuid, so only
    # the four-digit case is demonstrable here.
    settings = _settings()
    digit_id = "10000030-1000-4bd6-8f23-d348fdb5a694"
    assert str(settings.llm_max_output_tokens) == "1000"
    records = _passing_records(settings)
    records["config-only"] = _record(
        "config-only",
        f"Conversation {digit_id} did not report a ceiling.",
        records["config-only"].trace,  # type: ignore[attr-defined]
    )
    seed_outcome = agent_demo.SeedOutcome(
        near_id=digit_id, fresh_id=_FRESH_ID, near_spent=320, fresh_spent=30, near_turns=3
    )
    outcomes = _outcomes(assert_suite(records, settings, seed_outcome, live=False))
    assert outcomes["config-only:answer_contains_llm_max_output_tokens"] == "failed"


def test_wording_signal_is_measured_on_the_neutralized_answer() -> None:
    # The recorded "429/exhaustion wording" signal is not a pass condition,
    # but the article may quote it. A seeded uuid containing "429" would
    # inflate it on a bare-substring test, so the signal is read from the same
    # neutralized text the figures are.
    settings = _settings()
    near_id = "429e0a1b-3702-4bd6-8f23-d348fdb5a694"
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        f"Conversation {near_id} has spent 320 tokens and has 80 remaining.",
        [_call(_LEDGER, {"conversation_id": near_id})],
    )
    seed_outcome = agent_demo.SeedOutcome(
        near_id=near_id, fresh_id=_FRESH_ID, near_spent=320, fresh_spent=30, near_turns=3
    )
    assertions = assert_suite(records, settings, seed_outcome, live=True)
    (direction,) = [a for a in assertions if a.name == "diagnostic-near:answer_direction"]
    assert direction.outcome == "passed"  # type: ignore[attr-defined]
    assert "wording not observed" in direction.detail  # type: ignore[attr-defined]


def test_ledger_call_target_must_be_the_seeded_conversation() -> None:
    # Neutralization collapses both ids into one token, so a run that queried
    # the FRESH ledger while answering the NEAR diagnostic normalizes exactly
    # like a correct run. Only a raw, pre-neutralization comparison of the
    # ledger call's own `conversation_id` argument can see it.
    settings = _settings()
    records = _passing_records(settings)
    records["diagnostic-near"] = _record(
        "diagnostic-near",
        records["diagnostic-near"].answer,  # type: ignore[attr-defined]
        [_call(_LEDGER, {"conversation_id": _FRESH_ID})],  # wrong conversation
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert outcomes["diagnostic-near:ledger_call_targeted_the_seeded_conversation"] == "failed"
    assert outcomes["diagnostic-fresh:ledger_call_targeted_the_seeded_conversation"] == "passed"
    # the ledger call still happened — this is a different defect from not
    # consulting the ledger at all
    assert outcomes["diagnostic-near:ledger_call_executed"] == "passed"


def test_ledger_call_target_is_unverified_when_the_arguments_are_unparseable() -> None:
    # arguments=None means the model's arguments could not be parsed, so there
    # is no id to read. Guessing one would be fabrication; the assertion fails.
    settings = _settings()
    records = _passing_records(settings)
    records["diagnostic-fresh"] = _record(
        "diagnostic-fresh",
        records["diagnostic-fresh"].answer,  # type: ignore[attr-defined]
        [
            {
                "tool_name": _LEDGER,
                "arguments": None,
                "arguments_canonical_json": "{not json",
                "executed": True,
                "round_index": 1,
            }
        ],
    )
    outcomes = _outcomes(assert_suite(records, settings, _seed_outcome(), live=True))
    assert outcomes["diagnostic-fresh:ledger_call_targeted_the_seeded_conversation"] == "failed"


async def test_divergence_probe_records_what_its_masking_hides() -> None:
    # The probe compares GUID-masked traces, so a model fabricating DIFFERENT
    # uuid-shaped argument values across repeats is invisible to it. That is
    # real non-determinism the probe exists to observe, so "not observed" must
    # carry the caveat rather than read as "identical".
    class _StubService:
        async def run(self, task: str) -> AgentRunResult:
            return _result(None)

        async def aclose(self) -> None:
            return None

    probe = await agent_demo.run_divergence_probe(_StubService(), "q", [])
    assert probe["divergence_observed"] is False
    assert "GUID" in probe["masking_caveat"]


def test_near_answer_direction_passes_on_ledger_figure_without_429_wording() -> None:
    # The pass condition is the ledger-derived figure alone; "429"/"exhaust"
    # wording is a recorded signal, not a requirement. A live answer phrased
    # without that vocabulary but with the correct numbers must still pass —
    # otherwise a paid live run fails on a wording preference, not a defect.
    settings = _settings()
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
    settings = _settings()
    assertions = assert_suite(_passing_records(settings), settings, _seed_outcome(), live=True)
    outcomes = _outcomes(assertions)
    assert set(outcomes.values()) == {"passed"}


def test_branch_evidence_degrades_to_no_branch_observed_not_to_failure() -> None:
    # A plausible correct run with nothing to distinguish: the same trace on
    # both sides (rule 3 makes search_docs -> ledger -> answer plausible for
    # both diagnostics) and, at spent=320/80, ledger figure sets that
    # coincide. Both answers are grounded, yet no branch is observable — that
    # must be recorded as unverified, never as a failure.
    settings = _settings()
    records = _passing_records(settings)
    seed_outcome = agent_demo.SeedOutcome(
        near_id=_NEAR_ID, fresh_id=_FRESH_ID, near_spent=320, fresh_spent=80, near_turns=3
    )
    # same trace SHAPE on both sides, each still targeting its own conversation
    records["diagnostic-fresh"] = _record(
        "diagnostic-fresh",
        "It has spent 80 tokens and can still spend 320.",
        [_call(_LEDGER, {"conversation_id": _FRESH_ID}), _call(_SEARCH, {"query": "429"})],
    )
    assertions = assert_suite(records, settings, seed_outcome, live=True)
    outcomes = _outcomes(assertions)
    assert outcomes["diagnostic:branch_evidence"] == "no_branch_observed"
    assert "failed" not in outcomes.values()
    (branch,) = [a for a in assertions if a.name == "diagnostic:branch_evidence"]
    assert branch.evidence_form == "none"  # type: ignore[attr-defined]


def test_assert_suite_fake_live_split_is_exactly_the_model_behaviour_claims() -> None:
    # The authorized fake/live deviation rests on this property: fake mode
    # skips the claims a canned fake cannot exercise and asserts everything
    # else. The two ledger-target claims join that set for a different reason
    # than the model-behaviour ones: FakeAgentService calls the ledger with a
    # hardcoded id, so there is nothing to match against a seeded id.
    settings = _settings()
    records = _passing_records(settings)
    fake = _outcomes(assert_suite(records, settings, _seed_outcome(), live=False))
    expected_skipped = {
        "diagnostic:branch_evidence",
        "diagnostic-near:answer_direction",
        "diagnostic-fresh:answer_direction",
        "diagnostic-near:ledger_call_targeted_the_seeded_conversation",
        "diagnostic-fresh:ledger_call_targeted_the_seeded_conversation",
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


def _run_demo(tmp_path: Path, *extra: str) -> "subprocess.CompletedProcess[str]":
    """A real fake-mode run of the script — zero network by construction.

    `cwd` is the test's temp directory, not the repo root: `Settings` reads
    `.env` relative to the working directory, and the repo root carries an
    untracked one on a developer machine. Running there made the exit code
    depend on a file that exists in no fresh clone and in no CI checkout.
    Nothing about the run needs it — `--fake` forces all three fake adapters
    atomically, the script and the capture path are absolute, and the capture's
    commit sha is read with the repo root as its own cwd.
    """
    return subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--fake",
            "--capture",
            str(tmp_path / "capture.json"),
            *extra,
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )


def test_require_live_assertions_turns_unverified_into_a_non_zero_exit(tmp_path: Path) -> None:
    # `summarize_assertions` and UNVERIFIED_OUTCOMES are unit-tested, but the
    # wiring that turns an unverified assertion into exit 1 is the part an
    # operator actually depends on, and it was evidenced only by a manual run.
    # Fake mode always leaves the four model-behaviour claims unverified, so it
    # is the honest fixture for both halves of the contract.
    assert not (tmp_path / ".env").exists()  # the run stands on no local config
    default_run = _run_demo(tmp_path)
    assert default_run.returncode == 0, default_run.stdout + default_run.stderr
    assert "unverified" in default_run.stdout
    strict = _run_demo(tmp_path, "--require-live-assertions")
    assert strict.returncode == 1, strict.stdout + strict.stderr
    assert "--require-live-assertions: 6 assertion(s) unverified" in strict.stdout
    assert "diagnostic:branch_evidence" in strict.stdout


def test_mode_resolution_is_atomic() -> None:
    base = _settings(use_fake_llm=False, use_fake_search=True, use_fake_embeddings=False)
    fake = resolve_settings(base, live=False)
    assert (fake.use_fake_llm, fake.use_fake_search, fake.use_fake_embeddings) == (True, True, True)
    live = resolve_settings(base, live=True)
    assert (live.use_fake_llm, live.use_fake_search, live.use_fake_embeddings) == (
        False,
        False,
        False,
    )
