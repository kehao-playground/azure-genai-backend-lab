"""Day 17 agent demo: replayable suite, smoke assertions, measurements.

Modes (atomic — no mixed composition):
  --fake  (default) forces use_fake_llm/search/embeddings True; zero network.
  --live  forces all three real; requires Azure env (chat-mini + search).

Flow: build shared store -> seed the near-exhausted conversation through the
REAL ConversationChatService commit path (SEED_NEAR_TURNS turns) -> measure
what it spent and DERIVE the token budget from that measurement -> build the
toolset with the derived budget -> seed the fresh conversation -> assert both
preconditions -> run the question suite -> assert -> print measurements ->
write redacted JSON capture (--capture PATH).

The budget is derived rather than fixed because the near-exhausted state is a
window whose width is a fraction of the budget, and one real turn can cost
more than a fixed window is wide (see `derive_token_budget`).

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, NoReturn

from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.core.errors import ConfigurationError
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.search_index import INDEX_NAME
from azgenai_lab.prompts.loader import PromptTemplate, load_prompt
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
    build_agent_tool_deps,
)
from azgenai_lab.services.azure_openai import ChatService, build_chat_service
from azgenai_lab.services.conversation import ConversationChatService
from azgenai_lab.services.conversation_store import ConversationStore, build_conversation_store

# The demo's token budget is MEASURED, not fixed. A fixed budget of 400 made
# the near-exhausted window [320, 400) exactly 80 tokens wide, while one real
# turn of the deployed reasoning model costs 126-535 tokens (measured: 29 in /
# 506 out / 256 reasoning). The step is larger than the window, so the state
# the demo is built to show was reachable only by coincidence — the first live
# turn overshot the entire budget by 34%. Nothing caught it because the
# constant had been calibrated against the fake, whose deterministic ladder
# (15/50/105/180/275/390) happens to land 10 tokens under it.
#
# So the dependency is inverted: seed a fixed, small number of REAL turns,
# measure what they actually spent, and derive the budget from that
# measurement (`derive_token_budget`). The window then scales with the model's
# verbosity instead of being contradicted by it.
SEED_NEAR_TURNS = 3
NEAR_TARGET_RATIO = 0.9  # where the seeded near conversation lands in the budget
# Same constant as the tools apply, imported rather than re-typed: a demo that
# defined its own 0.8 could seed against a threshold the tools do not use.
NEAR_LOW = NEAR_EXHAUSTED_THRESHOLD
DEMO_TENANT = "opsdemo"
DEMO_PRINCIPAL = Principal(tenant_id=DEMO_TENANT, user_id="opsdemo-user", group_ids=())
K_REPEATS = 5
DEFAULT_CAPTURE = "agent-demo-capture.json"

EXIT_ASSERTION = 1
EXIT_CONFIG = 2

LEDGER_TOOL = "get_conversation_usage"
CONFIG_TOOL = "get_runtime_config"
SEARCH_TOOL = "search_docs"
NO_LEDGER_SENTINEL = [("<no-ledger-call>", "")]

# Wording family for the no-hit admission. The prompt asks for "no supporting
# evidence"; the first live run said "found no supporting material" — the same
# behaviour in a synonym. Asserting one literal phrase tests the model's word
# choice, not whether it admitted the gap, so it fails on a correct run. The
# behavioural claim is paired with a structural one below: a no-hit answer must
# carry no citation, because there was nothing to cite.
ABSENCE_MARKERS = frozenset(
    {
        "no supporting",
        "couldn't find",
        "could not find",
        "found no",
        "no documentation",
        "not documented",
        "no information",
    }
)
# Every grounded answer in this suite cites a parent id, which starts with the
# length-prefixed tenant segment. See models/rag.py's make_parent_id.
CITATION_PREFIX = "t7=opsdemo"

# Conversation-id masks. The capture keeps the two apart for a reader;
# a comparison must not tell them apart at all (see `comparison_redactions`).
# None of the three contains a digit, so no mask can ever look like a ledger
# figure to the matchers below.
CONV_NEAR = "conv-near"
CONV_FRESH = "conv-fresh"
CONV_ANY = "<conversation-id>"

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

# What the divergence probe cannot see. Its traces go through the same GUID
# mask as the capture, so two repeats that passed *different* uuid-shaped
# argument values compare equal here — genuine non-determinism of exactly the
# kind the probe exists to observe. "not observed" therefore means "no
# divergence outside the mask", and must not be quoted as "identical".
MASKED_DIVERGENCE_NOTE = (
    "GUID-shaped argument values are masked before comparison, so repeats that "
    "differ only in fabricated uuid-shaped arguments are invisible to this probe"
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


def neutralize(text: str, replacements: Sequence[tuple[str, str]]) -> str:
    """The capture's own redaction, reused as a comparison guard.

    Same substitution as `redact` applies when writing the capture — including
    its GUID mask, which catches any conversation id, seeded or not. Reused
    rather than reimplemented: a second, weaker copy of this rule is exactly
    how a conversation id gets back into a comparison.
    """
    neutral: str = redact(text, replacements)
    return neutral


def _figure_in(text: str, figure: str) -> bool:
    """Whether `text` states `figure` — on a word boundary, not as a substring.

    `168` inside `1683` is not the remainder, and a bare `in` test would say
    it was. The figures run from a two-digit remainder (fake mode) to a
    four-digit derived budget, and every one of those widths occurs inside a
    32-hex-character conversation id, so this matters at every size.
    """
    return re.search(rf"\b{re.escape(figure)}\b", text) is not None


def _normalized_arguments(
    call: Mapping[str, Any], replacements: Sequence[tuple[str, str]] = ()
) -> str:
    """Canonical argument text with the conversation id normalized out.

    Dropping the `conversation_id` *key* is not enough: the diagnostic
    question names the conversation, so the id plausibly reappears inside
    another argument's value (`search_docs(query="conversation <id> 429")`),
    where it would make two behaviourally identical traces differ. Argument
    values therefore go through the same redaction the capture uses.

    `arguments=None` means the model's arguments were unparseable; the raw
    canonical text is kept so such a call stays distinguishable from a
    genuine zero-argument call instead of collapsing into `{}`.
    """
    arguments = call.get("arguments")
    if arguments is None:
        return neutralize(str(call.get("arguments_canonical_json", "")), replacements)
    args = {k: v for k, v in arguments.items() if k != "conversation_id"}
    return json.dumps(redact(args, replacements), sort_keys=True)


def normalized_trace(
    tool_calls: Sequence[Mapping[str, Any]], replacements: Sequence[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """Whole trace as (tool_name, normalized arguments) pairs."""
    return [
        (str(call["tool_name"]), _normalized_arguments(call, replacements)) for call in tool_calls
    ]


def normalized_post_ledger_trace(
    tool_calls: Sequence[Mapping[str, Any]],
    replacements: Sequence[tuple[str, str]] = (),
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
    return normalized_trace(suffix, replacements)


def ledger_call_conversation_ids(trace: Sequence[Mapping[str, Any]]) -> list[str]:
    """`conversation_id` of every executed ledger call, raw and un-neutralized.

    Deliberately *not* normalized. Everywhere else in this file the two seeded
    ids collapse into one token, because a comparison must not tell them
    apart — and that collapsing is exactly what would hide a run that read the
    fresh conversation's ledger while answering about the near one. Here the
    raw value is the whole point.

    A call whose arguments could not be parsed contributes nothing: there is
    no id to read, and inventing one would be the fabrication this demo
    exists to avoid, so such a run simply fails to show its target.
    """
    ids: list[str] = []
    for call in trace:
        if call["tool_name"] != LEDGER_TOOL or not call["executed"]:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        value = arguments.get("conversation_id")
        if value is not None:
            ids.append(str(value))
    return ids


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
    replacements: Sequence[tuple[str, str]],
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

    `replacements` (from `comparison_redactions`) neutralizes the two seeded
    conversation ids on both channels first. Without it each channel yields a
    false positive from the ids alone: two traces that only name their own
    conversation would "diverge", and two answers that only echo their own
    conversation id would "carry" a ledger figure.
    """
    near_full = normalized_trace(near_trace, replacements)
    fresh_full = normalized_trace(fresh_trace, replacements)
    consulted_ledger = (
        normalized_post_ledger_trace(near_trace, replacements) != NO_LEDGER_SENTINEL
        or normalized_post_ledger_trace(fresh_trace, replacements) != NO_LEDGER_SENTINEL
    )
    traces_differ = near_full != fresh_full
    if traces_differ and consulted_ledger:
        return BranchEvidence(
            BRANCH_TRACE,
            f"full traces differ: near={near_full} fresh={fresh_full}",
        )
    near_only = sorted(near_figures - fresh_figures)
    fresh_only = sorted(fresh_figures - near_figures)
    near_clean = neutralize(near_answer, replacements)
    fresh_clean = neutralize(fresh_answer, replacements)
    near_hits = [figure for figure in near_only if _figure_in(near_clean, figure)]
    fresh_hits = [figure for figure in fresh_only if _figure_in(fresh_clean, figure)]
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


def derive_token_budget(near_spent: int) -> int:
    """The budget that puts a measured spend at `NEAR_TARGET_RATIO` of itself.

    This is the whole point of the change recorded at the top of the file: the
    near-exhausted window is `[NEAR_LOW * budget, budget)`, so fixing the
    budget fixes the window's width, and a model whose single turn costs more
    than that width can only land inside it by luck. Deriving the budget from
    what the seed actually spent makes the window scale with the model —
    at 0.9 of a `NEAR_LOW = 0.8` budget the measurement sits in the middle of
    the window with room on both sides, whatever the turn cost.

    The result is checked against the very contract it exists to satisfy and
    the derivation refuses rather than returning a budget that fails it. Two
    inputs reach that guard: a spend of 0 (a provider that reported no usage
    at all — with no measurement there is nothing to derive from), and spends
    of 1-4 tokens, where rounding `spent / 0.9` back down to `spent` would
    describe an *exhausted* conversation, not a near-exhausted one. Both are
    derivation failures the caller must report loudly; silently handing such a
    number to the tools would produce a demo that proves nothing.
    """
    budget = round(near_spent / NEAR_TARGET_RATIO)
    if not seed_precondition_ok("near_exhausted", spent=near_spent, budget=budget):
        raise ValueError(
            f"cannot derive a token budget from a measured spend of {near_spent}: "
            f"round({near_spent} / {NEAR_TARGET_RATIO}) = {budget}, which does not put "
            f"{near_spent} inside the near-exhausted window "
            f"[{NEAR_LOW * budget}, {budget})"
        )
    return budget


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


def _conversation_pairs(
    near_id: str, fresh_id: str, near_mask: str, fresh_mask: str
) -> list[tuple[str, str]]:
    # An empty id is dropped: `"".replace` would inject the mask between every
    # character rather than protect anything.
    return [(cid, mask) for cid, mask in ((near_id, near_mask), (fresh_id, fresh_mask)) if cid]


def _redactions(
    settings: Settings, conversation_pairs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    replacements = list(conversation_pairs)
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


def build_redactions(settings: Settings, *, near_id: str, fresh_id: str) -> list[tuple[str, str]]:
    """Literal replacements applied to every string in the capture.

    Every literal replacement here runs before the GUID mask in `redact`, so a
    conversation id becomes its own stable placeholder instead of being
    swallowed by `<redacted-guid>`; ordering *within* this list is by needle
    length (longest first) so a deployment name that is a substring of the
    endpoint cannot shadow the endpoint's own replacement. `opsdemo` is
    deliberately *not* masked: it is a sample-corpus tenant published in this
    repo, not a customer or Entra tenant id — those are GUIDs and are masked
    by pattern.
    """
    return _redactions(settings, _conversation_pairs(near_id, fresh_id, CONV_NEAR, CONV_FRESH))


def comparison_redactions(
    settings: Settings, *, near_id: str, fresh_id: str
) -> list[tuple[str, str]]:
    """The capture's redaction, with both conversation ids collapsed into one.

    The same substitution mechanism, aimed at a different question. A capture
    is *read*, so it keeps the two conversations distinguishable (`conv-near`
    vs `conv-fresh`). An assertion *compares*, and there the ids are precisely
    what must not count: an answer that merely echoes the question would
    otherwise "carry" a ledger figure, and two traces naming their own
    conversation would otherwise "diverge".

    The figures are no longer the one- or two-digit remainders a fixed budget
    of 400 produced; a derived budget and its remainder are typically three or
    four digits. That makes the collision *more* likely to survive matching,
    not less. A conversation id is 32 hex characters, so a three-digit run
    still appears inside one most of the time — and a uuid's middle groups are
    exactly four characters wide, so a four-digit figure can be a whole group
    (`...-1512-...`), where it is `\\b`-delimited and word-boundary matching
    cannot reject it at all. Neutralizing both ids into the same token is what
    rejects it, and it is why the boundary matcher is not enough on its own.
    """
    return _redactions(settings, _conversation_pairs(near_id, fresh_id, CONV_ANY, CONV_ANY))


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
class NearSeed:
    """The near conversation as seeded, before any budget exists.

    `spent` is the measurement the budget is derived from, so this type is
    what separates "what the model actually cost" from "what the demo then
    calls the budget" — the two used to be the same hardcoded number.
    """

    conversation_id: str
    spent: int
    turns: int
    usage_totals: dict[str, int] = field(default_factory=dict)


@dataclass
class SeedOutcome:
    near_id: str
    fresh_id: str
    near_spent: int
    fresh_spent: int
    near_turns: int
    # The derived budget, carried with the measurement it came from: the same
    # number the toolset was built with, so an assertion can never compare a
    # spend against a budget the tools did not report.
    token_budget: int
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


async def seed_near(store: ConversationStore, seed_service: ConversationChatService) -> NearSeed:
    """`SEED_NEAR_TURNS` real turns, then read what they spent.

    Seeding goes through the real commit path — `ChatService.complete` never
    commits, and `store` is the same instance the toolset closes over, so what
    this writes is exactly what `get_conversation_usage` later reads. Going
    through the real ledger is the design, not a convenience: a demo that
    poked a number into the store would be demonstrating its own fixture.

    The turn count is FIXED rather than a loop against a threshold. There is
    nothing to loop against any more — the threshold is a fraction of a budget
    this measurement has yet to produce — and a fixed count also makes a live
    run's cost predictable instead of "up to SEED_MAX_TURNS turns, depending
    on how terse the model felt". (`SEED_MAX_TURNS` existed only as that
    loop's safety cap and is gone with it.)
    """
    totals: dict[str, int] = {}
    conversation_id, result = await seed_service.complete(
        "seed turn 1", None, principal=DEMO_PRINCIPAL
    )
    _add_usage(totals, result.usage)
    for turn in range(2, SEED_NEAR_TURNS + 1):
        _, result = await seed_service.complete(
            f"seed turn {turn}", conversation_id, principal=DEMO_PRINCIPAL
        )
        _add_usage(totals, result.usage)
    conversation = await store.get(DEMO_TENANT, conversation_id)
    if conversation is None:
        _fail(f"seed contract: conversation {conversation_id} vanished from the store")
    return NearSeed(
        conversation_id=conversation_id,
        spent=conversation.total_tokens,
        turns=SEED_NEAR_TURNS,
        usage_totals=totals,
    )


async def seed_fresh(
    store: ConversationStore,
    seed_service: ConversationChatService,
    near: NearSeed,
    *,
    budget: int,
) -> SeedOutcome:
    """One fresh turn, then both preconditions as a hard contract.

    The near conversation lands at `NEAR_TARGET_RATIO` of `budget` by
    construction and the fresh one clears `NEAR_LOW * budget` with roughly
    `SEED_NEAR_TURNS - 1` turns of margin — but both are asserted anyway. A
    bug in the derivation must fail loudly here rather than silently produce a
    demo whose "near-exhausted" conversation is nothing of the sort; that
    silent version is exactly what the fixed budget shipped.

    The near ledger is re-read rather than trusted from `near.spent`: the
    budget was derived from that figure, so if the two disagree the budget
    describes a state the store no longer holds.
    """
    totals = dict(near.usage_totals)
    fresh_id, result = await seed_service.complete("hello", None, principal=DEMO_PRINCIPAL)
    _add_usage(totals, result.usage)

    near_conversation = await store.get(DEMO_TENANT, near.conversation_id)
    fresh_conversation = await store.get(DEMO_TENANT, fresh_id)
    if near_conversation is None or fresh_conversation is None:
        _fail("seed contract: a seeded conversation is not visible in the shared store")
    near_spent = near_conversation.total_tokens
    fresh_spent = fresh_conversation.total_tokens
    if near_spent != near.spent:
        _fail(
            f"seed contract: the near ledger moved between measurement and assertion "
            f"({near.spent} -> {near_spent}), so budget {budget} was derived from a "
            "spend the store no longer reports"
        )
    if not seed_precondition_ok("near_exhausted", spent=near_spent, budget=budget):
        _fail(
            f"seed contract: near-exhausted spent={near_spent} budget={budget} "
            f"after {near.turns} turns"
        )
    if not seed_precondition_ok("fresh", spent=fresh_spent, budget=budget):
        _fail(f"seed contract: fresh spent={fresh_spent} budget={budget}")
    return SeedOutcome(
        near_id=near.conversation_id,
        fresh_id=fresh_id,
        near_spent=near_spent,
        fresh_spent=fresh_spent,
        near_turns=near.turns,
        token_budget=budget,
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
        result = await service.run(question, (), principal=DEMO_PRINCIPAL)
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
    chat_service: ChatService, prompt: PromptTemplate, record: RunRecord
) -> dict[str, Any]:
    """One model call over the same instructions and the agent's tool outputs.

    This is a **model/token loop-overhead comparison, not an end-to-end
    latency baseline**: the tool work is already done and simply pasted in.
    """
    items = [{"role": "user", "content": _baseline_input(prompt.text, record)}]
    started = time.perf_counter()
    result = await chat_service.complete(items)
    baseline_model_ms = (time.perf_counter() - started) * 1000
    usage = result.usage
    return {
        "baseline_kind": "model_token_overhead_comparison",
        "for_question": record.label,
        # The ops_agent prompt reaches this call in BOTH modes (it travels
        # inside the input item above, not as the provider's `instructions`),
        # so its provenance belongs here rather than under `run_conditions`,
        # where it would read as the agent's — and in fake mode the agent
        # applied no prompt at all (see `agent_prompt_provenance`).
        "prompt_name": prompt.name,
        "prompt_version": prompt.version,
        "prompt_sha256": prompt.sha256,
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
        "masking_caveat": MASKED_DIVERGENCE_NOTE,
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
        # not only model-behaviour claims any more: the ledger-target checks
        # skip because the fake's conversation id is hardcoded
        reasons.append("claims that need a real provider require --live")
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

_FAKE_LEDGER_TARGET_REASON = (
    "FakeAgentService calls the ledger with its own hardcoded conversation id, "
    "never a seeded one, so there is no target to match here (asserted in --live)"
)


def assert_suite(
    records: Mapping[str, RunRecord], settings: Settings, seed_outcome: SeedOutcome, *, live: bool
) -> list[Assertion]:
    results: list[Assertion] = []
    # Built once, for every answer this function matches figures in. The
    # config questions carry no conversation id today, but nothing enforces
    # that, and a seeded uuid whose group is literally "1000"-shaped is a
    # `\b`-delimited digit run like any other.
    replacements = comparison_redactions(
        settings, near_id=seed_outcome.near_id, fresh_id=seed_outcome.fresh_id
    )

    config_only = records["config-only"]
    max_output = str(settings.llm_max_output_tokens)
    _check(
        results,
        "config-only:answer_contains_llm_max_output_tokens",
        _figure_in(neutralize(config_only.answer, replacements), max_output),
        f"expected {max_output!r} in the answer",
    )
    _check(
        results,
        "config-only:runtime_config_executed",
        _executed_calls(config_only.trace, CONFIG_TOOL) >= 1,
        f"expected an executed {CONFIG_TOOL} call",
    )

    config_docs = records["config+docs"]
    budget = str(seed_outcome.token_budget)
    _check(
        results,
        "config+docs:answer_contains_demo_token_budget",
        _figure_in(neutralize(config_docs.answer, replacements), budget),
        # Known residual, stated where it is asserted. It used to be specific:
        # the budget was the constant 400, which is also an HTTP status code,
        # so EVERY run carried a collision a word boundary could not resolve.
        # The budget is now derived from the seed's measured spend, so nothing
        # ties it to a status code and that particular collision is gone. The
        # class is not: any derived value can coincide with a figure the answer
        # states for another reason (an HTTP status such as 429, a latency in
        # ms, a byte count), and a boundary rule cannot tell those apart
        # either. So this stays a CORROBORATING SIGNAL, not proof; the
        # structural proof that the agent read the figure from
        # get_runtime_config is config+docs:runtime_config_executed, which runs
        # in both modes.
        f"expected {budget!r} in the answer — corroborating signal only: a bare "
        f"{budget!r} on a word boundary is indistinguishable from the same digits "
        "used as an HTTP status, a latency or a byte count, so the structural "
        "proof is config+docs:runtime_config_executed",
    )
    _check(
        results,
        "config+docs:runtime_config_executed",
        _executed_calls(config_docs.trace, CONFIG_TOOL) >= 1,
        f"expected an executed {CONFIG_TOOL} call",
    )

    near = records["diagnostic-near"]
    fresh = records["diagnostic-fresh"]
    for label, record, seeded_id in (
        ("near", near, seed_outcome.near_id),
        ("fresh", fresh, seed_outcome.fresh_id),
    ):
        _check(
            results,
            f"diagnostic-{label}:ledger_call_executed",
            _executed_calls(record.trace, LEDGER_TOOL) >= 1,
            f"expected an executed {LEDGER_TOOL} call",
        )
        # Which conversation the ledger call named, checked on the RAW trace:
        # every comparison in this file collapses the two seeded ids into one
        # token, so a run that read the other conversation's ledger normalizes
        # exactly like a correct one. Only the raw argument shows the target.
        targeted_name = f"diagnostic-{label}:ledger_call_targeted_the_seeded_conversation"
        if live:
            targeted = ledger_call_conversation_ids(record.trace)
            _check(
                results,
                targeted_name,
                seeded_id in targeted,
                f"expected an executed {LEDGER_TOOL} call naming the seeded "
                f"{label} conversation {seeded_id!r}; the executed ledger calls "
                f"named {targeted} (matched raw, before neutralization)",
            )
        else:
            _skip(results, targeted_name, _FAKE_LEDGER_TARGET_REASON)

    if live:
        near_figures = ledger_figures(seed_outcome.near_spent, seed_outcome.token_budget)
        fresh_figures = ledger_figures(seed_outcome.fresh_spent, seed_outcome.token_budget)
        evidence = branch_evidence(
            near_trace=near.trace,
            fresh_trace=fresh.trace,
            near_answer=near.answer,
            fresh_answer=fresh.answer,
            near_figures=near_figures,
            fresh_figures=fresh_figures,
            replacements=replacements,
        )
        results.append(
            Assertion(
                "diagnostic:branch_evidence",
                OUTCOME_PASSED if evidence.form != BRANCH_NONE else OUTCOME_NO_BRANCH,
                f"{evidence.form}: {evidence.detail}",
                evidence_form=evidence.form,
            )
        )
        # The ledger figure is what makes this unearnable by paraphrase:
        # DIAGNOSTIC_TEMPLATE already contains "429", so wording alone is
        # satisfied by an answer that only restates the question. The
        # "429"/"exhaust" wording is recorded for the article but is not part
        # of the pass condition — a correct live answer phrased without that
        # vocabulary must not fail on a wording preference.
        #
        # The same restatement carries the conversation id, and at a derived
        # budget the remainder is typically three or four digits — wide enough
        # to sit inside a 32-hex-character id, and at four digits wide enough
        # to BE one of its dash-delimited groups, where a word boundary would
        # accept it. So the id is neutralized before matching *and* the figure
        # must land on a word boundary; either rule alone lets the paraphrase
        # earn the assertion again, one token further along.
        near_clean = neutralize(near.answer, replacements)
        near_hits = sorted(figure for figure in near_figures if _figure_in(near_clean, figure))
        near_lower = near_clean.lower()
        wording_hits = [word for word in ("429", "exhaust") if word in near_lower]
        wording_note = f"observed (saw {wording_hits})" if wording_hits else "not observed"
        _check(
            results,
            "diagnostic-near:answer_direction",
            bool(near_hits),
            f"expected a ledger figure from {sorted(near_figures)} (saw {near_hits}); "
            f"429/exhaustion wording {wording_note}",
        )
        fresh_clean = neutralize(fresh.answer, replacements)
        fresh_hits = sorted(figure for figure in fresh_figures if _figure_in(fresh_clean, figure))
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
        lowered = no_hit.answer.lower()
        admissions = sorted(m for m in ABSENCE_MARKERS if m in lowered)
        cited = CITATION_PREFIX in no_hit.answer
        _check(
            results,
            "no-hit:answer_admits_missing_evidence",
            bool(admissions) and not cited,
            f"expected an absence admission from {sorted(ABSENCE_MARKERS)} "
            f"(saw {admissions}) and no fabricated citation "
            f"(cited={cited}). The prompt asks for the words 'no supporting "
            f"evidence', but a synonym is the same behaviour — what this "
            f"asserts is that the agent admitted the gap and did not answer "
            f"from general knowledge. The absence of a citation is the "
            f"structural half: every grounded answer in this suite carries a "
            f"{CITATION_PREFIX!r} source, and a no-hit run has nothing to cite.",
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
    """HEAD, marked `+dirty` when the working tree differs from it.

    A capture that names a commit it does not correspond to is a false
    provenance claim, and it is not hypothetical: the Day 17 live run that
    produced the first real evidence ran HEAD *plus* an uncommitted
    assertion fix, and the capture said only `21a1c42`.
    """
    repo_root = Path(__file__).resolve().parents[1]

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        return "unknown"
    sha = head.stdout.strip()
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        return f"{sha}+dirty_unknown"
    return f"{sha}+dirty" if status.stdout.strip() else sha


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


def agent_prompt_provenance(prompt: PromptTemplate, *, live: bool) -> dict[str, Any]:
    """The prompt the *agent* applied, or `not_applicable`.

    `AgentFrameworkService` loads `ops_agent` and hands it to the framework as
    the provider's `instructions`, so in live mode these three fields are the
    agent run's own prompt provenance. `FakeAgentService` applies no prompt at
    all, so a fake run has none to report — recording the loaded values under
    `run_conditions` anyway would read as "the agent ran on this prompt", which
    is exactly what did not happen. The same prompt does still reach the
    baseline call in both modes; that copy's provenance is recorded under
    `baseline`, where it is unambiguously the baseline's.
    """
    if not live:
        return {
            "agent_prompt_name": _NOT_APPLICABLE,
            "agent_prompt_version": _NOT_APPLICABLE,
            "agent_prompt_sha256": _NOT_APPLICABLE,
        }
    return {
        "agent_prompt_name": prompt.name,
        "agent_prompt_version": prompt.version,
        "agent_prompt_sha256": prompt.sha256,
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
    # Loaded once: both the seed and baseline services get the same
    # default_chat PromptTemplate instance (Day 22) — this tool never drives
    # them through the /chat audit finalizer, but build_chat_service's
    # signature no longer loads its own copy either way.
    default_chat_prompt = load_prompt("default_chat")
    try:
        seed_chat_service = build_chat_service(settings, prompt=default_chat_prompt)
    except (ValueError, ConfigurationError) as exc:
        _config_error(str(exc))
    # No budget on the seed service: the budget is DERIVED from what these
    # turns spend, so it does not exist yet — and a gate here would reject the
    # very turns that define it. The tools' budget is unaffected; it is the
    # derived number, threaded from the single place it is computed below.
    seed_service = ConversationChatService(seed_chat_service, store, token_budget=None)
    try:
        baseline_service = build_chat_service(settings, prompt=default_chat_prompt)
    except (ValueError, ConfigurationError) as exc:
        await seed_service.aclose()
        _config_error(str(exc))

    # Ordering forced by the derivation: both budget-carrying tools take the
    # budget at construction, so the near conversation must be seeded and
    # measured BEFORE the toolset exists. Nothing downstream has been built
    # yet, so a failure here closes only these two services.
    try:
        near_seed = await seed_near(store, seed_service)
        try:
            budget = derive_token_budget(near_seed.spent)
        except ValueError as exc:
            _fail(f"seed contract: {exc}")
    except BaseException:
        await seed_service.aclose()
        await baseline_service.aclose()
        raise
    print(
        f"budget: {budget} derived from {near_seed.spent} tokens spent over "
        f"{near_seed.turns} seed turns (target {NEAR_TARGET_RATIO:.0%} of budget, "
        f"near-exhausted window [{NEAR_LOW * budget:g}, {budget}))"
    )

    try:
        deps = build_agent_tool_deps(
            settings, conversation_store=store, token_budget=budget
        )
    except (ValueError, ConfigurationError) as exc:
        await seed_service.aclose()
        await baseline_service.aclose()
        _config_error(str(exc))
    # TODO(day18): the principal now binds per-run inside the adapters'
    # run() (agent_framework.py), so wrap_tools_with_recorder cannot wrap the
    # tools the service actually calls at this call site — `sink` stays empty
    # until a recorder seam is threaded through the per-run binding.

    # The fresh conversation and both preconditions, now that there is a budget
    # to hold them against. Still before the agent service exists, so its own
    # cleanup is not owed yet — the retriever is, since the toolset holds it.
    try:
        seed_outcome = await seed_fresh(store, seed_service, near_seed, budget=budget)
    except BaseException:
        await seed_service.aclose()
        await baseline_service.aclose()
        await deps.retriever.aclose()
        raise
    print(
        f"seed: near-exhausted spent={seed_outcome.near_spent}/{budget} "
        f"({seed_outcome.near_turns} turns), fresh spent={seed_outcome.fresh_spent}/"
        f"{budget} (1 turn)\n"
    )

    prompt = load_prompt("ops_agent")
    try:
        agent_service: AgentService = build_agent_service(settings, deps, prompt=prompt)
    except (ValueError, ConfigurationError) as exc:
        await seed_service.aclose()
        await baseline_service.aclose()
        await deps.retriever.aclose()
        _config_error(str(exc))

    try:
        records: dict[str, RunRecord] = {}
        for label, question in build_questions(seed_outcome.near_id, seed_outcome.fresh_id):
            record = await run_question(agent_service, label, question, sink)
            records[label] = record
            _print_run(record)

        baseline = await run_baseline(baseline_service, prompt, records[BASELINE_LABEL])
        print(
            f"baseline ({baseline['baseline_kind']}, {BASELINE_LABEL}): "
            f"model_ms={baseline['baseline_model_ms']} usage={baseline['usage']}"
        )

        divergence = await run_divergence_probe(
            agent_service, records[BASELINE_LABEL].question, sink
        )
        observed = bool(divergence["divergence_observed"])
        print(
            f"divergence over K={K_REPEATS} repeats of {BASELINE_LABEL}: "
            f"{'observed' if observed else 'not observed'} "
            f"({divergence['distinct_traces']} distinct trace(s))"
        )
        if not observed:
            # Say what "not observed" does not cover, next to the claim itself.
            print(f"  caveat: {divergence['masking_caveat']}")
        print()

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
                # Derived, not configured — so the manifest says so rather
                # than letting a reader take it for a constant of the demo.
                "demo_token_budget": seed_outcome.token_budget,
                "demo_token_budget_source": (
                    f"derived: round(near_spent / {NEAR_TARGET_RATIO}), where near_spent "
                    f"is what {SEED_NEAR_TURNS} real seed turns actually spent"
                ),
                "seed_near_turns": SEED_NEAR_TURNS,
                "near_target_ratio": NEAR_TARGET_RATIO,
                "near_exhausted_threshold": NEAR_LOW,
                "agent_max_iterations": settings.agent_max_iterations,
                "agent_max_tool_calls": settings.agent_max_tool_calls,
                "llm_max_output_tokens": settings.llm_max_output_tokens,
                "max_search_hits": MAX_SEARCH_HITS,
                "max_snippet_chars": MAX_SNIPPET_CHARS,
                "max_tool_result_bytes": MAX_TOOL_RESULT_BYTES,
                "max_refusal_result_bytes": MAX_REFUSAL_RESULT_BYTES,
                "agent_max_task_bytes": AGENT_MAX_TASK_BYTES,
                **agent_prompt_provenance(prompt, live=live),
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
                    "measurement and from the baseline comparison. "
                    "run_conditions.demo_token_budget is DERIVED from "
                    "near_spent_tokens (see demo_token_budget_source), so the "
                    "near conversation sits at the target ratio of the budget "
                    "whatever the model's turns cost — it is not a constant the "
                    "seed had to reach"
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
