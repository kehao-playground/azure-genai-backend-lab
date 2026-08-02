"""Day 17 agent demo: replayable suite, smoke assertions, measurements.

Modes (atomic — no mixed composition):
  --fake  (default) forces use_fake_llm/search/embeddings True; zero network.
  --live  forces all three real; requires Azure env (chat-mini + search).

Flow: build shared store -> seed two conversations through the REAL
ConversationChatService commit path (near-exhausted + fresh, preconditions
asserted) -> run the question suite -> assert -> print measurements ->
write redacted JSON capture (--capture PATH).

Exit codes: 0 no assertion failed; 1 assertion/seed-contract failure;
2 configuration error. An assertion that was never exercised is reported as
*unverified*, never as passed — `--require-live-assertions` turns any
unverified assertion into exit 1 for anyone who needs a fully-verified run
(the default invocation keeps the table above).

What each mode can and cannot prove. Fake mode exercises the wiring and the
*shape* of every trace: which tools ran, in what order, with what arguments,
and how many times. It cannot exercise a claim about model behaviour —
`FakeAgentService` calls every tool exactly once in a fixed order with a
fixed conversation id, so its trace is a constant, and comparing two of its
traces proves nothing about branching. Those assertions (the ledger->branch
evidence and every answer-direction check) therefore run in --live only and
are recorded as `skipped_fake` here, never as passed. A demo that reported
them green against a canned fake would be exactly the kind of unearned claim
this script exists to prevent.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, NoReturn

from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search_index import INDEX_NAME
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.agent_framework import (
    AGENT_MAX_TASK_BYTES,
    AgentRunError,
    AgentRunResult,
    AgentService,
    build_agent_service,
)
from azgenai_lab.services.agent_tools import (
    MAX_REFUSAL_RESULT_BYTES,
    MAX_SEARCH_HITS,
    MAX_SNIPPET_CHARS,
    MAX_TOOL_RESULT_BYTES,
    NEAR_EXHAUSTED_THRESHOLD,
    AgentToolFn,
    build_agent_toolset,
)
from azgenai_lab.services.azure_openai import ChatService, build_chat_service
from azgenai_lab.services.conversation import ConversationChatService
from azgenai_lab.services.conversation_store import ConversationStore, build_conversation_store

DEMO_TOKEN_BUDGET = 400          # single source (spec §1): seed service,
                                 # config tool, usage tool, evidence manifest
SEED_MAX_TURNS = 10
# Same constant as the tools apply, imported rather than re-typed: a demo that
# defined its own 0.8 could seed against a threshold the tools do not use.
NEAR_LOW = NEAR_EXHAUSTED_THRESHOLD
DEMO_TENANT = "opsdemo"
DEMO_PRINCIPAL = Principal(tenant_id=DEMO_TENANT, group_ids=())
K_REPEATS = 5
DEFAULT_CAPTURE = "agent-demo-capture.json"

EXIT_ASSERTION = 1
EXIT_CONFIG = 2

LEDGER_TOOL = "get_conversation_usage"
CONFIG_TOOL = "get_runtime_config"
SEARCH_TOOL = "search_docs"
NO_LEDGER_SENTINEL = [("<no-ledger-call>", "")]

# Assertion outcomes. `skipped_fake` and `no_branch_observed` are both
# *unverified*: nothing failed, and nothing was proved either.
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED_FAKE = "skipped_fake"
OUTCOME_NO_BRANCH = "no_branch_observed"
UNVERIFIED_OUTCOMES = frozenset({OUTCOME_SKIPPED_FAKE, OUTCOME_NO_BRANCH})

# Forms of branch evidence, strongest first (see `branch_evidence`).
BRANCH_TRACE = "trace_divergence"
BRANCH_ANSWER = "answer_content_divergence"
BRANCH_NONE = "none"

QUESTION_SUITE: list[tuple[str, str | None]] = [
    ("config-only", "What is the maximum output tokens per model call here?"),
    ("docs-only", "What does a client have to do with unknown SSE event names?"),
    ("config+docs", "What is the conversation token budget here, and why does it exist?"),
    ("diagnostic-near", None),   # filled with near-exhausted conversation id
    ("diagnostic-fresh", None),  # filled with fresh conversation id
    ("no-hit", "What is the retention policy for uploaded fine-tuning datasets?"),
]
DIAGNOSTIC_TEMPLATE = (
    "Conversation {cid}: why might its next request be rejected with 429, "
    "and how many more tokens can it spend?"
)
BASELINE_LABEL = "config+docs"

_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _fail(message: str) -> NoReturn:
    print(f"FAIL: {message}")
    raise SystemExit(EXIT_ASSERTION)


def _config_error(message: str) -> NoReturn:
    print(f"CONFIG ERROR: {message}")
    raise SystemExit(EXIT_CONFIG)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/unit/test_agent_demo_helpers.py)
# --------------------------------------------------------------------------


def _normalized_arguments(call: Mapping[str, Any]) -> str:
    """Canonical argument text with `conversation_id` normalized out.

    `arguments=None` means the model's arguments were unparseable; the raw
    canonical text is kept so such a call stays distinguishable from a
    genuine zero-argument call instead of collapsing into `{}`.
    """
    arguments = call.get("arguments")
    if arguments is None:
        return str(call.get("arguments_canonical_json", ""))
    args = {k: v for k, v in arguments.items() if k != "conversation_id"}
    return json.dumps(args, sort_keys=True)


def normalized_trace(tool_calls: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Whole trace as (tool_name, normalized arguments) pairs."""
    return [(str(call["tool_name"]), _normalized_arguments(call)) for call in tool_calls]


def normalized_post_ledger_trace(
    tool_calls: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """Trace suffix after the first ledger call, conversation_id normalized out.

    A run that never consulted the ledger yields a sentinel, so two such runs
    compare equal to each other: "no ledger call" is not branch evidence.
    """
    for i, call in enumerate(tool_calls):
        if call["tool_name"] == LEDGER_TOOL:
            suffix = tool_calls[i + 1 :]
            break
    else:
        return list(NO_LEDGER_SENTINEL)
    return normalized_trace(suffix)


def ledger_figures(spent: int, budget: int) -> set[str]:
    """The two numbers only the ledger can supply: spend and remainder.

    Neither is in the question, so an answer carrying one of them read the
    ledger; an answer that merely restates the question carries neither.
    """
    return {str(spent), str(budget - spent)}


@dataclass(frozen=True)
class BranchEvidence:
    """Which *form* of branch was observed, so the article can name it."""

    form: str  # BRANCH_TRACE | BRANCH_ANSWER | BRANCH_NONE
    detail: str


def branch_evidence(
    *,
    near_trace: Sequence[Mapping[str, Any]],
    fresh_trace: Sequence[Mapping[str, Any]],
    near_answer: str,
    fresh_answer: str,
    near_figures: set[str],
    fresh_figures: set[str],
) -> BranchEvidence:
    """Graded observation of whether the two diagnostics actually branched.

    Trace divergence is the strongest form and is looked for over the *full*
    normalized trace: nothing forces the ledger call to come first (the
    prompt's rule 3 makes `search_docs -> ledger -> answer` a plausible trace
    for both diagnostics), so a post-ledger suffix comparison alone would
    call a perfectly good run a failure. `NO_LEDGER_SENTINEL`'s protection is
    kept: two runs that never consulted the ledger are never branch evidence,
    however different their traces look.

    Failing that, an answer-content divergence on the ledger-derived numbers
    is a real branch too — expressed in the answer instead of in the trace.
    When neither holds the outcome is `BRANCH_NONE`, which the caller records
    as unverified: absence of observed branching is not proof of its absence,
    and it is certainly not a pass.
    """
    near_full = normalized_trace(near_trace)
    fresh_full = normalized_trace(fresh_trace)
    consulted_ledger = (
        normalized_post_ledger_trace(near_trace) != NO_LEDGER_SENTINEL
        or normalized_post_ledger_trace(fresh_trace) != NO_LEDGER_SENTINEL
    )
    traces_differ = near_full != fresh_full
    if traces_differ and consulted_ledger:
        return BranchEvidence(
            BRANCH_TRACE,
            f"full traces differ: near={near_full} fresh={fresh_full}",
        )
    near_only = sorted(near_figures - fresh_figures)
    fresh_only = sorted(fresh_figures - near_figures)
    near_hits = [figure for figure in near_only if figure in near_answer]
    fresh_hits = [figure for figure in fresh_only if figure in fresh_answer]
    if near_hits and fresh_hits:
        return BranchEvidence(
            BRANCH_ANSWER,
            f"traces match, answers do not: the near answer carries {near_hits} "
            f"and the fresh answer carries {fresh_hits}, and those figures are "
            "obtainable only from each conversation's own ledger entry",
        )
    reason = (
        "traces are identical"
        if not traces_differ
        else "traces differ but neither run consulted the ledger, so the "
        "difference is not evidence of a ledger-driven branch"
    )
    return BranchEvidence(
        BRANCH_NONE,
        f"{reason}; ledger figures unique to near {near_only} seen in the near "
        f"answer: {near_hits}, unique to fresh {fresh_only} seen in the fresh "
        f"answer: {fresh_hits}",
    )


def seed_precondition_ok(kind: str, *, spent: int, budget: int) -> bool:
    if kind == "fresh":
        return spent < NEAR_LOW * budget
    if kind == "near_exhausted":
        return NEAR_LOW * budget <= spent < budget
    raise ValueError(kind)


def trace_of(result: AgentRunResult) -> list[dict[str, Any]]:
    """The run's tool calls as plain dicts — the capture's trace shape."""
    return [
        {
            "tool_name": call.tool_name,
            "arguments": dict(call.arguments) if call.arguments is not None else None,
            "arguments_canonical_json": call.arguments_canonical_json,
            "round_index": call.round_index,
            "executed": call.executed,
        }
        for call in result.tool_calls
    ]


def tool_ms_from(result: AgentRunResult) -> float | None:
    """Executed-tool latency, or None when it was never measured.

    The only latency source is the admission wrapper's `ToolExecution`
    records, which reach us as `per_round`. `per_round` is None whenever the
    execution-to-round join was rejected (and always for the fake), and a
    round's `latency_ms` may itself be None — both are honest absences, and
    summing them to 0.0 would publish a measurement nobody took.
    """
    if result.per_round is None:
        return None
    if any(round_metrics.latency_ms is None for round_metrics in result.per_round):
        return None
    return sum(round_metrics.latency_ms or 0.0 for round_metrics in result.per_round)


def per_round_ms_from(result: AgentRunResult) -> dict[str, float | None] | None:
    """Per-round latency keyed by `round_index` (never positional).

    `per_round` carries only rounds that made a tool call — never the
    terminal answer round — so indexing it against model calls would be
    wrong. JSON object keys must be strings, hence `str(round_index)`.
    """
    if result.per_round is None:
        return None
    return {str(r.round_index): r.latency_ms for r in result.per_round}


def usage_of(result: AgentRunResult) -> dict[str, int | None] | None:
    if result.usage is None:
        return None
    return {
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "total_tokens": result.usage.total_tokens,
        "reasoning_tokens": result.usage.reasoning_tokens,
    }


def build_redactions(
    settings: Settings, *, near_id: str, fresh_id: str
) -> list[tuple[str, str]]:
    """Literal replacements applied to every string in the capture.

    Conversation ids first, so they become stable placeholders rather than
    being swallowed by the GUID mask that runs afterwards. `opsdemo` is
    deliberately *not* masked: it is a sample-corpus tenant published in this
    repo, not a customer or Entra tenant id — those are GUIDs and are masked
    by pattern.
    """
    replacements = [(near_id, "conv-near"), (fresh_id, "conv-fresh")]
    for value in (
        settings.azure_openai_endpoint,
        settings.azure_search_endpoint,
        settings.azure_openai_deployment_name,
        settings.azure_openai_embedding_deployment,
        INDEX_NAME,
    ):
        if value:
            replacements.append((value, "<redacted>"))
    # Longest first: a deployment name that is a substring of the endpoint
    # must not shadow the endpoint's own replacement.
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
    return replacements


def redact(value: Any, replacements: Sequence[tuple[str, str]]) -> Any:
    """Recursively mask ids, endpoints and resource names in a JSON payload."""
    if isinstance(value, str):
        for needle, mask in replacements:
            value = value.replace(needle, mask)
        return _GUID_RE.sub("<redacted-guid>", value)
    if isinstance(value, dict):
        return {key: redact(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, replacements) for item in value]
    return value


def resolve_settings(base: Settings, *, live: bool) -> Settings:
    """Atomic mode selection: all three adapters move together, never mixed."""
    return base.model_copy(
        update={
            "use_fake_llm": not live,
            "use_fake_search": not live,
            "use_fake_embeddings": not live,
        }
    )


def build_questions(near_id: str, fresh_id: str) -> list[tuple[str, str]]:
    """The frozen suite with the two diagnostic ids filled in."""
    ids = {"diagnostic-near": near_id, "diagnostic-fresh": fresh_id}
    questions: list[tuple[str, str]] = []
    for label, text in QUESTION_SUITE:
        if text is None:
            text = DIAGNOSTIC_TEMPLATE.format(cid=ids[label])
        questions.append((label, text))
    return questions


# --------------------------------------------------------------------------
# Tool-output recording (the baseline reuses what the agent actually read)
# --------------------------------------------------------------------------


@dataclass
class ToolOutputRecord:
    tool_name: str
    arguments: dict[str, Any]
    output: str


def wrap_tools_with_recorder(
    tools: Sequence[AgentToolFn], sink: list[ToolOutputRecord]
) -> tuple[AgentToolFn, ...]:
    """Capture each tool's real output so the baseline can reuse it.

    `AgentRunResult` carries the calls, not their results, and re-invoking
    the tools afterwards would be a *second* retrieval — a different set of
    documents is possible, and in live mode it costs another embedding call.
    Recording here means the baseline prompt contains exactly the text the
    agent read. `functools.wraps` keeps `__name__`/`__doc__`/`__wrapped__`
    intact, which is what the framework introspects and what the admission
    wrapper joins on.
    """

    def _wrap(tool: AgentToolFn) -> AgentToolFn:
        @functools.wraps(tool)
        async def recording_tool(*args: Any, **kwargs: Any) -> str:
            output = await tool(*args, **kwargs)
            # Keyword arguments only: every caller in this project invokes
            # tools by keyword (the framework binds the JSON arguments object
            # by name), so positional args would be a shape this demo has
            # never seen rather than something to silently coerce.
            sink.append(ToolOutputRecord(tool.__name__, dict(kwargs), output))
            return output

        return recording_tool

    return tuple(_wrap(tool) for tool in tools)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


@dataclass
class SeedOutcome:
    near_id: str
    fresh_id: str
    near_spent: int
    fresh_spent: int
    near_turns: int
    usage_totals: dict[str, int] = field(default_factory=dict)


def _add_usage(totals: dict[str, int], result_usage: Any) -> None:
    # The call is counted even when the provider omitted its usage block, so
    # `model_calls` stays a true count of seed calls and the token totals stay
    # a true sum of what was actually reported (Day 9: never fabricate counts).
    totals["model_calls"] = totals.get("model_calls", 0) + 1
    if result_usage is None:
        totals["calls_without_usage"] = totals.get("calls_without_usage", 0) + 1
        return
    totals["input_tokens"] = totals.get("input_tokens", 0) + result_usage.input_tokens
    totals["output_tokens"] = totals.get("output_tokens", 0) + result_usage.output_tokens
    totals["total_tokens"] = totals.get("total_tokens", 0) + result_usage.total_tokens


async def seed(store: ConversationStore, seed_service: ConversationChatService) -> SeedOutcome:
    """Seed through the real commit path — `ChatService.complete` never commits.

    `store` is the same instance the toolset closed over, so what the seed
    commits is exactly what `get_conversation_usage` later reads.

    The near-exhausted conversation is turned until the ledger the tools read
    shows spent >= 80% of the budget, hard-capped at SEED_MAX_TURNS so a live
    run's nondeterministic usage still terminates. Both preconditions are a
    contract: failing them here is a seed-contract failure, not a confusing
    branch failure three assertions later.
    """
    totals: dict[str, int] = {}
    near_id, result = await seed_service.complete("seed turn 1", None, tenant_id=DEMO_TENANT)
    _add_usage(totals, result.usage)
    turns = 1
    for _ in range(SEED_MAX_TURNS - 1):
        conversation = await store.get(DEMO_TENANT, near_id)
        if conversation is None:
            _fail(f"seed contract: conversation {near_id} vanished from the store")
        if conversation.total_tokens >= NEAR_LOW * DEMO_TOKEN_BUDGET:
            break
        _, result = await seed_service.complete("seed turn", near_id, tenant_id=DEMO_TENANT)
        _add_usage(totals, result.usage)
        turns += 1
    fresh_id, result = await seed_service.complete("hello", None, tenant_id=DEMO_TENANT)
    _add_usage(totals, result.usage)

    # preconditions (spec §1): fail as seed-contract failure, not later
    near_conversation = await store.get(DEMO_TENANT, near_id)
    fresh_conversation = await store.get(DEMO_TENANT, fresh_id)
    if near_conversation is None or fresh_conversation is None:
        _fail("seed contract: a seeded conversation is not visible in the shared store")
    near = near_conversation.total_tokens
    fresh = fresh_conversation.total_tokens
    if not seed_precondition_ok("near_exhausted", spent=near, budget=DEMO_TOKEN_BUDGET):
        _fail(
            f"seed contract: near-exhausted spent={near} budget={DEMO_TOKEN_BUDGET} "
            f"after {turns} turns (cap {SEED_MAX_TURNS})"
        )
    if not seed_precondition_ok("fresh", spent=fresh, budget=DEMO_TOKEN_BUDGET):
        _fail(f"seed contract: fresh spent={fresh}")
    return SeedOutcome(
        near_id=near_id,
        fresh_id=fresh_id,
        near_spent=near,
        fresh_spent=fresh,
        near_turns=turns,
        usage_totals=totals,
    )


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


@dataclass
class RunRecord:
    label: str
    question: str
    answer: str
    trace: list[dict[str, Any]]
    tool_outputs: list[ToolOutputRecord]
    measurements: dict[str, Any]
    shape: dict[str, Any]


async def run_question(
    service: AgentService, label: str, question: str, sink: list[ToolOutputRecord]
) -> RunRecord:
    sink.clear()
    started = time.perf_counter()
    try:
        result = await service.run(question)
    except AgentRunError as exc:
        # Redaction (Task 13 carry-forward): AgentRunError's message is
        # str(provider_exception) and can carry the endpoint host. Only the
        # exception class and the message's size are ever surfaced.
        _fail(
            f"{label}: agent run failed — exception={type(exc).__name__} "
            f"message_bytes={len(str(exc).encode('utf-8'))}"
        )
    end_to_end_ms = (time.perf_counter() - started) * 1000
    tool_ms = tool_ms_from(result)
    return RunRecord(
        label=label,
        question=question,
        answer=result.answer,
        trace=trace_of(result),
        tool_outputs=list(sink),
        measurements={
            "agent_end_to_end_ms": round(end_to_end_ms, 1),
            "agent_tool_ms": None if tool_ms is None else round(tool_ms, 1),
            "agent_model_ms": None if tool_ms is None else round(end_to_end_ms - tool_ms, 1),
            "agent_tool_ms_source": "per_round" if tool_ms is not None else "unavailable",
            "per_round_ms_by_round_index": per_round_ms_from(result),
            "usage": usage_of(result),
        },
        shape={
            "model_call_count": result.model_call_count,
            "tool_round_count": result.tool_round_count,
            "tool_call_count": result.tool_call_count,
            "refused_call_count": result.refused_call_count,
            "stop_reason": result.stop_reason,
            "limit_reasons": sorted(result.limit_reasons),
        },
    )


def _baseline_input(instructions: str, record: RunRecord) -> str:
    parts = [instructions, "", "Tool results gathered for this question:"]
    for tool_output in record.tool_outputs:
        parts.append(f"- {tool_output.tool_name}: {tool_output.output}")
    parts.extend(["", f"Question: {record.question}"])
    return "\n".join(parts)


async def run_baseline(
    chat_service: ChatService, instructions: str, record: RunRecord
) -> dict[str, Any]:
    """One model call over the same instructions and the agent's tool outputs.

    This is a **model/token loop-overhead comparison, not an end-to-end
    latency baseline**: the tool work is already done and simply pasted in.
    """
    items = [{"role": "user", "content": _baseline_input(instructions, record)}]
    started = time.perf_counter()
    result = await chat_service.complete(items)
    baseline_model_ms = (time.perf_counter() - started) * 1000
    usage = result.usage
    return {
        "baseline_kind": "model_token_overhead_comparison",
        "for_question": record.label,
        "baseline_model_ms": round(baseline_model_ms, 1),
        "usage": (
            None
            if usage is None
            else {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            }
        ),
        "caveat": (
            "the baseline call also carries the default_chat prompt as the "
            "provider's `instructions` (build_chat_service owns that), while "
            "the ops_agent instructions travel inside the input item — so the "
            "input-token figure includes both, and is an upper bound on a "
            "single-instruction control"
        ),
    }


async def run_divergence_probe(
    service: AgentService, question: str, sink: list[ToolOutputRecord]
) -> dict[str, Any]:
    """K repeats of one question; divergence is reported, never presupposed."""
    traces: list[list[tuple[str, str]]] = []
    for _ in range(K_REPEATS):
        record = await run_question(service, BASELINE_LABEL, question, sink)
        traces.append(normalized_trace(record.trace))
    unique = {tuple(trace) for trace in traces}
    return {
        "question_label": BASELINE_LABEL,
        "k": K_REPEATS,
        "divergence_observed": len(unique) > 1,
        "distinct_traces": len(unique),
        "normalized_traces": [[list(step) for step in trace] for trace in traces],
    }


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------


@dataclass
class Assertion:
    name: str
    # OUTCOME_PASSED | OUTCOME_FAILED | OUTCOME_SKIPPED_FAKE | OUTCOME_NO_BRANCH
    outcome: str
    detail: str
    # Only branch evidence carries one: which form was observed, so the
    # article can say what was seen rather than "it branched".
    evidence_form: str | None = None


def summarize_assertions(assertions: Sequence[Assertion]) -> str:
    """The closing line — it must never claim more than was exercised.

    A partially-verified run and a fully-verified one printed the same line
    once; `tools/tenant_smoke.py` already settled how this project treats an
    unexercised probe (INCONCLUSIVE, never PASS), and this is the same rule.
    """
    passed = sum(1 for a in assertions if a.outcome == OUTCOME_PASSED)
    unverified = [a for a in assertions if a.outcome in UNVERIFIED_OUTCOMES]
    if not unverified:
        return f"all assertions passed ({passed} passed)"
    reasons = []
    if any(a.outcome == OUTCOME_SKIPPED_FAKE for a in unverified):
        reasons.append("model-behaviour claims require --live")
    if any(a.outcome == OUTCOME_NO_BRANCH for a in unverified):
        reasons.append("no branch was observed")
    return (
        f"no assertion failed ({passed} passed, {len(unverified)} unverified — "
        f"{'; '.join(reasons)})"
    )


def _executed_calls(trace: Sequence[Mapping[str, Any]], tool_name: str) -> int:
    return sum(1 for call in trace if call["tool_name"] == tool_name and call["executed"])


def _check(results: list[Assertion], name: str, ok: bool, detail: str) -> None:
    results.append(Assertion(name, OUTCOME_PASSED if ok else OUTCOME_FAILED, detail))


def _skip(results: list[Assertion], name: str, reason: str) -> None:
    results.append(Assertion(name, OUTCOME_SKIPPED_FAKE, reason))


_FAKE_REASON = (
    "model-behaviour claim: FakeAgentService calls every tool once in a fixed "
    "order with a fixed conversation id, so this cannot be exercised without a "
    "provider (asserted in --live)"
)


def assert_suite(
    records: Mapping[str, RunRecord], settings: Settings, seed_outcome: SeedOutcome, *, live: bool
) -> list[Assertion]:
    results: list[Assertion] = []

    config_only = records["config-only"]
    max_output = str(settings.llm_max_output_tokens)
    _check(
        results,
        "config-only:answer_contains_llm_max_output_tokens",
        max_output in config_only.answer,
        f"expected {max_output!r} in the answer",
    )
    _check(
        results,
        "config-only:runtime_config_executed",
        _executed_calls(config_only.trace, CONFIG_TOOL) >= 1,
        f"expected an executed {CONFIG_TOOL} call",
    )

    config_docs = records["config+docs"]
    budget = str(DEMO_TOKEN_BUDGET)
    _check(
        results,
        "config+docs:answer_contains_demo_token_budget",
        budget in config_docs.answer,
        f"expected {budget!r} in the answer",
    )
    _check(
        results,
        "config+docs:runtime_config_executed",
        _executed_calls(config_docs.trace, CONFIG_TOOL) >= 1,
        f"expected an executed {CONFIG_TOOL} call",
    )

    near = records["diagnostic-near"]
    fresh = records["diagnostic-fresh"]
    for label, record in (("near", near), ("fresh", fresh)):
        _check(
            results,
            f"diagnostic-{label}:ledger_call_executed",
            _executed_calls(record.trace, LEDGER_TOOL) >= 1,
            f"expected an executed {LEDGER_TOOL} call",
        )

    if live:
        near_figures = ledger_figures(seed_outcome.near_spent, DEMO_TOKEN_BUDGET)
        fresh_figures = ledger_figures(seed_outcome.fresh_spent, DEMO_TOKEN_BUDGET)
        evidence = branch_evidence(
            near_trace=near.trace,
            fresh_trace=fresh.trace,
            near_answer=near.answer,
            fresh_answer=fresh.answer,
            near_figures=near_figures,
            fresh_figures=fresh_figures,
        )
        results.append(
            Assertion(
                "diagnostic:branch_evidence",
                OUTCOME_PASSED if evidence.form != BRANCH_NONE else OUTCOME_NO_BRANCH,
                f"{evidence.form}: {evidence.detail}",
                evidence_form=evidence.form,
            )
        )
        # The ledger figures are what makes this unearnable by paraphrase:
        # DIAGNOSTIC_TEMPLATE already contains "429", so the wording alone is
        # satisfied by an answer that only restates the question.
        near_hits = sorted(figure for figure in near_figures if figure in near.answer)
        near_lower = near.answer.lower()
        wording_hits = [word for word in ("429", "exhaust") if word in near_lower]
        _check(
            results,
            "diagnostic-near:answer_direction",
            bool(near_hits) and bool(wording_hits),
            f"expected a ledger figure from {sorted(near_figures)} (saw {near_hits}) "
            f"and 429/exhaustion wording (saw {wording_hits})",
        )
        fresh_hits = sorted(figure for figure in fresh_figures if figure in fresh.answer)
        _check(
            results,
            "diagnostic-fresh:answer_direction",
            bool(fresh_hits),
            f"expected a ledger figure from {sorted(fresh_figures)} (saw {fresh_hits})",
        )
    else:
        _skip(results, "diagnostic:branch_evidence", _FAKE_REASON)
        _skip(results, "diagnostic-near:answer_direction", _FAKE_REASON)
        _skip(results, "diagnostic-fresh:answer_direction", _FAKE_REASON)

    no_hit = records["no-hit"]
    search_calls = sum(1 for call in no_hit.trace if call["tool_name"] == SEARCH_TOOL)
    _check(
        results,
        "no-hit:at_most_one_reformulation",
        search_calls <= 2,
        f"expected <= 2 {SEARCH_TOOL} calls, saw {search_calls}",
    )
    if live:
        _check(
            results,
            "no-hit:answer_admits_missing_evidence",
            "no supporting evidence" in no_hit.answer.lower(),
            "expected the prompt's no-evidence wording in the answer",
        )
    else:
        _skip(results, "no-hit:answer_admits_missing_evidence", _FAKE_REASON)

    return results


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _lab_commit_sha() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip()


_NOT_APPLICABLE = "not_applicable"


def provider_provenance(arguments: argparse.Namespace, *, live: bool) -> dict[str, Any]:
    """Model provenance from a replayable source, or `not_applicable`.

    A deployment alias is not evidence of which model answered, so these come
    from the operator's deployment inventory plus the official documentation,
    passed in explicitly. A fake run measures nothing about the provider, so
    it records `not_applicable` rather than a plausible-looking value.
    """
    if not live:
        return {
            "provider_model_name": _NOT_APPLICABLE,
            "provider_model_version": _NOT_APPLICABLE,
            "context_window_tokens": _NOT_APPLICABLE,
            "context_window_source": _NOT_APPLICABLE,
            "context_window_source_checked_at": _NOT_APPLICABLE,
        }
    return {
        "provider_model_name": arguments.model_name,
        "provider_model_version": arguments.model_version,
        "context_window_tokens": arguments.context_window,
        "context_window_source": arguments.context_window_source,
        "context_window_source_checked_at": arguments.context_window_checked_at,
    }


# --------------------------------------------------------------------------
# Printing
#
# Console output is deliberately UNREDACTED — redaction is scoped to the
# capture file, so quote the capture into the article, never this output.
# --------------------------------------------------------------------------


def _print_run(record: RunRecord) -> None:
    measurements = record.measurements
    tools = ", ".join(
        f"{call['tool_name']}{'' if call['executed'] else '(refused)'}" for call in record.trace
    )
    print(f"[{record.label}] {record.question}")
    print(f"  tools: {tools or '(none)'}")
    shape = record.shape
    print(
        f"  stop={shape['stop_reason']} model_calls={shape['model_call_count']} "
        f"tool_rounds={shape['tool_round_count']} tool_calls={shape['tool_call_count']} "
        f"refused={shape['refused_call_count']}"
    )
    tool_ms = measurements["agent_tool_ms"]
    tool_text = "unavailable (per_round is None)" if tool_ms is None else f"{tool_ms}"
    model_ms = measurements["agent_model_ms"]
    model_text = "unavailable" if model_ms is None else f"{model_ms}"
    print(
        f"  end_to_end_ms={measurements['agent_end_to_end_ms']} "
        f"tool_ms={tool_text} model_ms={model_text}"
    )
    usage = measurements["usage"]
    print(f"  usage: {usage if usage is not None else 'not reported by the provider'}")
    print(f"  answer: {record.answer[:200].strip()}{'...' if len(record.answer) > 200 else ''}")
    print()


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Day 17 agent demo (see module docstring).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fake",
        action="store_true",
        help="force fake LLM, search and embeddings (default); zero network",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="force real LLM, search and embeddings; requires Azure credentials",
    )
    parser.add_argument(
        "--capture",
        default=DEFAULT_CAPTURE,
        help=f"path for the redacted JSON capture (default: {DEFAULT_CAPTURE})",
    )
    parser.add_argument(
        "--require-live-assertions",
        action="store_true",
        help=(
            "exit non-zero unless every assertion was verified (skipped in fake "
            "mode and no_branch_observed both count as unverified)"
        ),
    )
    parser.add_argument("--model-name", help="live only: model name from the deployment inventory")
    parser.add_argument("--model-version", help="live only: model version from the same inventory")
    parser.add_argument(
        "--context-window", type=int, help="live only: context window (C) from official docs"
    )
    parser.add_argument(
        "--context-window-source", help="live only: URL of the doc the context window came from"
    )
    parser.add_argument(
        "--context-window-checked-at",
        help="live only: ISO date the context-window source was checked",
    )
    return parser


def _require_live_configuration(settings: Settings, arguments: argparse.Namespace) -> None:
    missing = [
        name
        for name, value in (
            ("AZURE_OPENAI_ENDPOINT", settings.azure_openai_endpoint),
            ("AZURE_OPENAI_API_KEY", settings.azure_openai_api_key),
            ("AZURE_OPENAI_DEPLOYMENT_NAME", settings.azure_openai_deployment_name),
            ("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", settings.azure_openai_embedding_deployment),
            ("AZURE_SEARCH_ENDPOINT", settings.azure_search_endpoint),
            ("AZURE_SEARCH_ADMIN_KEY", settings.azure_search_admin_key),
        )
        if not value
    ]
    if missing:
        _config_error(f"--live requires {', '.join(missing)}")
    absent_evidence = [
        flag
        for flag, value in (
            ("--model-name", arguments.model_name),
            ("--model-version", arguments.model_version),
            ("--context-window", arguments.context_window),
            ("--context-window-source", arguments.context_window_source),
            ("--context-window-checked-at", arguments.context_window_checked_at),
        )
        if not value
    ]
    if absent_evidence:
        _config_error(
            "--live requires the model's provenance from a replayable source "
            f"({', '.join(absent_evidence)}) — a deployment alias is not evidence "
            "of which model answered"
        )


async def main() -> None:
    arguments = _build_parser().parse_args()
    live = bool(arguments.live)
    settings = resolve_settings(get_settings(), live=live)
    configure_logging(settings.log_level)
    if live:
        _require_live_configuration(settings, arguments)
    elif any(
        (
            arguments.model_name,
            arguments.model_version,
            arguments.context_window,
            arguments.context_window_source,
            arguments.context_window_checked_at,
        )
    ):
        print(
            "note: provider provenance flags are ignored in fake mode — a fake "
            "run measures nothing about the provider, so the manifest records "
            f"{_NOT_APPLICABLE}"
        )

    print(f"mode: {'live' if live else 'fake'}")
    started_at = datetime.now(UTC).isoformat()

    # Built one at a time, each step closing what already exists before it
    # reports a configuration error: the agent composite, the seed service and
    # the baseline service each own an independent model client, so a partial
    # construction would otherwise strand one of them (Task 13's lesson).
    store = build_conversation_store(settings)
    sink: list[ToolOutputRecord] = []
    try:
        seed_chat_service = build_chat_service(settings)
    except (ValueError, ConfigurationError) as exc:
        _config_error(str(exc))
    seed_service = ConversationChatService(seed_chat_service, store, token_budget=DEMO_TOKEN_BUDGET)
    try:
        baseline_service = build_chat_service(settings)
    except (ValueError, ConfigurationError) as exc:
        await seed_service.aclose()
        _config_error(str(exc))
    try:
        built = build_agent_toolset(
            settings, DEMO_PRINCIPAL, conversation_store=store, token_budget=DEMO_TOKEN_BUDGET
        )
    except (ValueError, ConfigurationError) as exc:
        await seed_service.aclose()
        await baseline_service.aclose()
        _config_error(str(exc))
    toolset = replace(built, tools=wrap_tools_with_recorder(built.tools, sink))
    try:
        agent_service: AgentService = build_agent_service(settings, toolset)
    except (ValueError, ConfigurationError) as exc:
        await seed_service.aclose()
        await baseline_service.aclose()
        await toolset.retriever.aclose()
        _config_error(str(exc))

    try:
        seed_outcome = await seed(store, seed_service)
        print(
            f"seed: near-exhausted spent={seed_outcome.near_spent}/{DEMO_TOKEN_BUDGET} "
            f"({seed_outcome.near_turns} turns), fresh spent={seed_outcome.fresh_spent}/"
            f"{DEMO_TOKEN_BUDGET} (1 turn)\n"
        )

        records: dict[str, RunRecord] = {}
        for label, question in build_questions(seed_outcome.near_id, seed_outcome.fresh_id):
            record = await run_question(agent_service, label, question, sink)
            records[label] = record
            _print_run(record)

        prompt = load_prompt("ops_agent")
        baseline = await run_baseline(baseline_service, prompt.text, records[BASELINE_LABEL])
        print(
            f"baseline ({baseline['baseline_kind']}, {BASELINE_LABEL}): "
            f"model_ms={baseline['baseline_model_ms']} usage={baseline['usage']}"
        )

        divergence = await run_divergence_probe(
            agent_service, records[BASELINE_LABEL].question, sink
        )
        print(
            f"divergence over K={K_REPEATS} repeats of {BASELINE_LABEL}: "
            f"{'observed' if divergence['divergence_observed'] else 'not observed'} "
            f"({divergence['distinct_traces']} distinct trace(s))\n"
        )

        assertions = assert_suite(records, settings, seed_outcome, live=live)
        print("assertions:")
        for assertion in assertions:
            marker = {
                OUTCOME_PASSED: "PASS",
                OUTCOME_FAILED: "FAIL",
                OUTCOME_SKIPPED_FAKE: "SKIP (unverified)",
                OUTCOME_NO_BRANCH: "UNVERIFIED",
            }[assertion.outcome]
            print(f"  {marker} {assertion.name} — {assertion.detail}")
        print()

        capture = {
            "run_conditions": {
                "mode": "live" if live else "fake",
                "k": K_REPEATS,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "lab_commit_sha": _lab_commit_sha(),
                "package_versions": {
                    name: _package_version(name)
                    for name in ("agent-framework-core", "agent-framework-openai", "openai")
                },
                "demo_token_budget": DEMO_TOKEN_BUDGET,
                "agent_max_iterations": settings.agent_max_iterations,
                "agent_max_tool_calls": settings.agent_max_tool_calls,
                "llm_max_output_tokens": settings.llm_max_output_tokens,
                "max_search_hits": MAX_SEARCH_HITS,
                "max_snippet_chars": MAX_SNIPPET_CHARS,
                "max_tool_result_bytes": MAX_TOOL_RESULT_BYTES,
                "max_refusal_result_bytes": MAX_REFUSAL_RESULT_BYTES,
                "agent_max_task_bytes": AGENT_MAX_TASK_BYTES,
                "prompt_name": prompt.name,
                "prompt_version": prompt.version,
                "prompt_sha256": prompt.sha256,
                **provider_provenance(arguments, live=live),
            },
            "setup": {
                "near_conversation_turns": seed_outcome.near_turns,
                "fresh_conversation_turns": 1,
                "near_spent_tokens": seed_outcome.near_spent,
                "fresh_spent_tokens": seed_outcome.fresh_spent,
                "usage_totals": seed_outcome.usage_totals,
                "note": (
                    "seed turns are setup: excluded from every per-question "
                    "measurement and from the baseline comparison"
                ),
            },
            "questions": [
                {
                    "label": record.label,
                    "question": record.question,
                    "trace": record.trace,
                    "answer": record.answer,
                    "measurements": record.measurements,
                    "shape": record.shape,
                }
                for record in records.values()
            ],
            "baseline": baseline,
            "divergence": divergence,
            "assertions": [
                {
                    "name": a.name,
                    "outcome": a.outcome,
                    "detail": a.detail,
                    # present only where a form of evidence is meaningful
                    **({} if a.evidence_form is None else {"evidence_form": a.evidence_form}),
                }
                for a in assertions
            ],
        }
        replacements = build_redactions(
            settings, near_id=seed_outcome.near_id, fresh_id=seed_outcome.fresh_id
        )
        capture_path = Path(arguments.capture)
        capture_path.write_text(
            json.dumps(redact(capture, replacements), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"capture written: {capture_path.resolve()}")

        failed = [a for a in assertions if a.outcome == OUTCOME_FAILED]
        if failed:
            # The capture is written first on purpose: a failing run is the one
            # whose evidence is most worth keeping.
            _fail(f"{len(failed)} assertion(s) failed: {', '.join(a.name for a in failed)}")
        print(summarize_assertions(assertions))
        unverified = [a for a in assertions if a.outcome in UNVERIFIED_OUTCOMES]
        if unverified and arguments.require_live_assertions:
            _fail(
                f"--require-live-assertions: {len(unverified)} assertion(s) unverified: "
                f"{', '.join(a.name for a in unverified)}"
            )
    finally:
        # Each created service closed exactly once, and every close is
        # attempted even if an earlier one raises — the agent composite owns
        # its own model client and the retriever, the seed and baseline
        # services own one model client each.
        try:
            await agent_service.aclose()
        finally:
            try:
                await seed_service.aclose()
            finally:
                await baseline_service.aclose()


if __name__ == "__main__":
    asyncio.run(main())
