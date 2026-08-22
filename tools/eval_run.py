"""Day 28 GenAI evaluation: golden-question dataset and runner.

This module implements the dataset section (`load_cases`), the deterministic
assertion evaluator (`evaluate_deterministic`), `requires` propagation / exit
codes (`evaluation_order`, `propagate_requires`, `gate_exit_code`), and pass A
-- a corpus-seeded `RagService`, its execution over the dataset
(`build_seeded_rag_service`, `run_pass_a`), and machine-captured retrieval
calibration (`calibration_document`, wired to `--calibrate`) (design
`drafts/research/day-28-evaluation.md` r04, §4/§5/§6/§7/§7.2/§7.5/§8;
implementation plan `plans/day-28-implementation-plan.md` Tasks 1-4). Later
tasks append the judge contract and multi-pass orchestration/reporting
(`--judge`, `--repeats`) to this same file -- they are deliberately absent
here, not stubbed.

`tools/` is not an installed package (no `tools/__init__.py`); tests load
this module by path, the same pattern `tests/unit/test_prompt_shields_probe.py`
uses.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Literal

import azgenai_lab
from azgenai_lab.core.audit import build_audit_attribution
from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.models.principal import Principal, validate_identifier
from azgenai_lab.models.rag import make_parent_id
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services import agent_tools
from azgenai_lab.services.azure_openai import build_chat_service
from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR, load_documents
from azgenai_lab.services.rag import RagService


@dataclass(frozen=True)
class Fact:
    id: str
    text: str


@dataclass(frozen=True)
class JudgedSpec:
    expected_facts: tuple[Fact, ...]
    forbidden_facts: tuple[Fact, ...]
    rubric: str | None


@dataclass(frozen=True)
class DeterministicSpec:
    status: Literal["answered", "no_answer"] | None
    status_note: str | None
    must_cite: tuple[str, ...]
    citations_subset_of: tuple[str, ...] | None
    subset_note: str | None
    must_not_cite: tuple[str, ...]


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    tenant: str
    user: str
    groups: tuple[str, ...]
    protects: str
    requires: tuple[str, ...]
    deterministic: DeterministicSpec
    judged: JudgedSpec | None
    judged_skip_reason: str | None


class DatasetError(Exception):
    """The dataset file is malformed, internally inconsistent, or drifted
    from the corpus it claims to describe."""


def _corpus_keys(corpus_dir: Path) -> frozenset[tuple[str, str]]:
    """`{(tenant_id, doc_id)}` for every document under `corpus_dir`, built
    through the same loader the production indexing path uses (design §8:
    every `doc_id` must really exist in the corpus, under the tenant the
    case claims) — never a second, hand-rolled reading of the corpus
    directory."""
    documents = load_documents(corpus_dir)
    return frozenset((doc.tenant_id, doc.doc_id) for doc in documents)


def _require_str(entry: dict[str, object], key: str, ctx: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{ctx}: {key} must be a non-empty string")
    return value


def _require_str_list(entry: dict[str, object], key: str, ctx: str) -> tuple[str, ...]:
    # The key must be present and a real list -- null is a distinct,
    # rejected value here, not a synonym for "empty" (design §5.2.1: only
    # citations_subset_of may be null; must_cite/must_not_cite may not).
    if key not in entry:
        raise DatasetError(f"{ctx}: missing '{key}' (use [] for none, not omission)")
    value = entry[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DatasetError(f"{ctx}: {key} must be a list of strings, not null")
    return tuple(value)


def _optional_str_list(entry: dict[str, object], key: str, ctx: str) -> tuple[str, ...] | None:
    if key not in entry:
        raise DatasetError(f"{ctx}: missing '{key}' (use [] for empty, null for unset)")
    value = entry[key]
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DatasetError(f"{ctx}: {key} must be a list of strings or null")
    return tuple(value)


def _validate_doc_ids(
    doc_ids: tuple[str, ...],
    *,
    tenant: str,
    corpus_keys: frozenset[tuple[str, str]],
    field: str,
    ctx: str,
) -> None:
    known_doc_ids = {doc_id for _, doc_id in corpus_keys}
    for doc_id in doc_ids:
        if doc_id not in known_doc_ids:
            raise DatasetError(f"{ctx}: {field} references unknown doc_id {doc_id!r}")
        if (tenant, doc_id) not in corpus_keys:
            raise DatasetError(
                f"{ctx}: {field} doc_id {doc_id!r} is not under tenant {tenant!r}"
            )


def _parse_deterministic(
    raw: object, *, tenant: str, corpus_keys: frozenset[tuple[str, str]], ctx: str
) -> DeterministicSpec:
    if not isinstance(raw, dict):
        raise DatasetError(f"{ctx}: deterministic must be an object")

    status_raw = raw.get("status")
    status: Literal["answered", "no_answer"] | None
    if status_raw is None:
        status = None
    elif status_raw == "answered":
        status = "answered"
    elif status_raw == "no_answer":
        status = "no_answer"
    else:
        raise DatasetError(f"{ctx}: status must be 'answered', 'no_answer', or null")
    status_note_raw = raw.get("status_note")
    status_note = status_note_raw if isinstance(status_note_raw, str) else None
    if status is None and not (status_note and status_note.strip()):
        raise DatasetError(f"{ctx}: status is null, status_note must be non-empty")

    must_cite = _require_str_list(raw, "must_cite", ctx)
    must_not_cite = _require_str_list(raw, "must_not_cite", ctx)
    citations_subset_of = _optional_str_list(raw, "citations_subset_of", ctx)
    subset_note_raw = raw.get("subset_note")
    subset_note = subset_note_raw if isinstance(subset_note_raw, str) else None
    if citations_subset_of is None and not (subset_note and subset_note.strip()):
        raise DatasetError(
            f"{ctx}: citations_subset_of is null, subset_note must be non-empty"
        )

    _validate_doc_ids(
        must_cite, tenant=tenant, corpus_keys=corpus_keys, field="must_cite", ctx=ctx
    )
    _validate_doc_ids(
        must_not_cite, tenant=tenant, corpus_keys=corpus_keys, field="must_not_cite", ctx=ctx
    )
    if citations_subset_of is not None:
        _validate_doc_ids(
            citations_subset_of,
            tenant=tenant,
            corpus_keys=corpus_keys,
            field="citations_subset_of",
            ctx=ctx,
        )

    must_cite_set = set(must_cite)
    must_not_cite_set = set(must_not_cite)
    if must_cite_set & must_not_cite_set:
        raise DatasetError(
            f"{ctx}: must_cite and must_not_cite overlap on "
            f"{sorted(must_cite_set & must_not_cite_set)}"
        )
    if citations_subset_of is not None:
        subset_set = set(citations_subset_of)
        if not must_cite_set <= subset_set:
            raise DatasetError(
                f"{ctx}: must_cite {sorted(must_cite_set - subset_set)} not in "
                "citations_subset_of"
            )
        if must_not_cite_set & subset_set:
            raise DatasetError(
                f"{ctx}: must_not_cite and citations_subset_of overlap on "
                f"{sorted(must_not_cite_set & subset_set)}"
            )

    return DeterministicSpec(
        status=status,
        status_note=status_note,
        must_cite=must_cite,
        citations_subset_of=citations_subset_of,
        subset_note=subset_note,
        must_not_cite=must_not_cite,
    )


def _parse_facts(raw: object, *, field: str, ctx: str) -> tuple[Fact, ...]:
    if not isinstance(raw, list):
        raise DatasetError(f"{ctx}: judged.{field} must be a list")
    facts: list[Fact] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DatasetError(f"{ctx}: judged.{field} entries must be objects")
        fact_id = item.get("id")
        text = item.get("text")
        if not isinstance(fact_id, str) or not fact_id.strip():
            raise DatasetError(f"{ctx}: judged.{field} entry has a non-empty string 'id'")
        if not isinstance(text, str) or not text.strip():
            raise DatasetError(f"{ctx}: judged.{field} fact {fact_id!r} has empty text")
        facts.append(Fact(id=fact_id, text=text))
    return tuple(facts)


def _parse_judged(raw: object, *, ctx: str) -> JudgedSpec | None:
    if raw is None:
        return None

    if not isinstance(raw, dict):
        raise DatasetError(f"{ctx}: judged must be an object or null")

    expected_facts = _parse_facts(raw.get("expected_facts"), field="expected_facts", ctx=ctx)
    forbidden_facts = _parse_facts(raw.get("forbidden_facts"), field="forbidden_facts", ctx=ctx)

    rubric_raw = raw.get("rubric")
    rubric: str | None
    if rubric_raw is None:
        rubric = None
    elif isinstance(rubric_raw, str):
        rubric = rubric_raw
    else:
        raise DatasetError(f"{ctx}: judged.rubric must be a string or null")

    seen_fact_ids: set[str] = set()
    for fact in (*expected_facts, *forbidden_facts):
        if fact.id in seen_fact_ids:
            raise DatasetError(f"{ctx}: duplicate fact id {fact.id!r} within this case")
        seen_fact_ids.add(fact.id)

    return JudgedSpec(
        expected_facts=expected_facts,
        forbidden_facts=forbidden_facts,
        rubric=rubric,
    )


def _parse_principal(raw: object, *, ctx: str) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise DatasetError(f"{ctx}: principal must be an object")
    tenant = _require_str(raw, "tenant", ctx)
    user = _require_str(raw, "user", ctx)
    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, list) or not all(isinstance(g, str) for g in groups_raw):
        raise DatasetError(f"{ctx}: principal.groups must be a list of strings")
    try:
        validate_identifier(tenant, field="principal.tenant")
        validate_identifier(user, field="principal.user")
        for group in groups_raw:
            validate_identifier(group, field="principal.groups entry")
    except ValueError as exc:
        raise DatasetError(f"{ctx}: {exc}") from exc
    return tenant, user, tuple(groups_raw)


def _parse_case(entry: object, *, index: int, corpus_keys: frozenset[tuple[str, str]]) -> EvalCase:
    if not isinstance(entry, dict):
        raise DatasetError(f"case at index {index}: entry must be an object")

    case_id_raw = entry.get("id")
    if not isinstance(case_id_raw, str) or not case_id_raw.strip():
        raise DatasetError(f"case at index {index}: id must be a non-empty string")
    ctx = f"case {case_id_raw!r}"

    question = _require_str(entry, "question", ctx)
    tenant, user, groups = _parse_principal(entry.get("principal"), ctx=ctx)
    protects = _require_str(entry, "protects", ctx)

    requires_raw = entry.get("requires")
    if not isinstance(requires_raw, list) or not all(isinstance(r, str) for r in requires_raw):
        raise DatasetError(f"{ctx}: requires must be a list of strings")
    requires = tuple(requires_raw)

    deterministic = _parse_deterministic(
        entry.get("deterministic"), tenant=tenant, corpus_keys=corpus_keys, ctx=ctx
    )

    judged = _parse_judged(entry.get("judged"), ctx=ctx)

    judged_skip_reason_raw = entry.get("judged_skip_reason")
    judged_skip_reason = (
        judged_skip_reason_raw if isinstance(judged_skip_reason_raw, str) else None
    )
    if judged is None and not (judged_skip_reason and judged_skip_reason.strip()):
        raise DatasetError(f"{ctx}: judged is null, judged_skip_reason must be non-empty")

    return EvalCase(
        id=case_id_raw,
        question=question,
        tenant=tenant,
        user=user,
        groups=groups,
        protects=protects,
        requires=requires,
        deterministic=deterministic,
        judged=judged,
        judged_skip_reason=judged_skip_reason,
    )


def _validate_requires(cases: Sequence[EvalCase]) -> None:
    by_id = {case.id: case for case in cases}
    for case in cases:
        for req in case.requires:
            if req == case.id:
                raise DatasetError(f"case {case.id!r}: requires itself")
            if req not in by_id:
                raise DatasetError(f"case {case.id!r}: requires unknown case id {req!r}")

    # Iterative DFS with a three-colour map so a cycle deep in one branch
    # does not blow the recursion limit and so the reported cycle names
    # every id on it (design §5/Task 1 step 4).
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = {case_id: WHITE for case_id in by_id}

    for start_id in by_id:
        if colour[start_id] != WHITE:
            continue
        path: list[str] = [start_id]
        frontier: list[Iterator[str]] = [iter(by_id[start_id].requires)]
        colour[start_id] = GRAY
        while path:
            child = next(frontier[-1], None)
            if child is None:
                colour[path.pop()] = BLACK
                frontier.pop()
                continue
            if colour[child] == WHITE:
                colour[child] = GRAY
                path.append(child)
                frontier.append(iter(by_id[child].requires))
            elif colour[child] == GRAY:
                cycle = path[path.index(child) :] + [child]
                raise DatasetError(f"requires cycle: {' -> '.join(cycle)}")
            # BLACK: already fully explored elsewhere; no cycle through it.


def load_cases(path: Path, corpus_dir: Path) -> tuple[EvalCase, ...]:
    """Load and validate `tools/eval_cases.json` (design §5/§8).

    Raises `DatasetError` on the first violation found, naming the
    offending case id (or its index, for entries too malformed to have
    yielded one yet). `corpus_dir` is required because roughly half the
    rules are cross-checks against the real sample corpus, not the
    dataset file in isolation.
    """
    raw_text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise DatasetError(f"{path}: top-level object must have a 'cases' list")

    corpus_keys = _corpus_keys(corpus_dir)

    seen_ids: set[str] = set()
    cases: list[EvalCase] = []
    for index, entry in enumerate(raw["cases"]):
        case = _parse_case(entry, index=index, corpus_keys=corpus_keys)
        if case.id in seen_ids:
            raise DatasetError(f"duplicate case id: {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)

    _validate_requires(cases)

    return tuple(cases)


class Verdict(StrEnum):
    """Outcome of scoring one case against one run's output.

    `evaluate_deterministic` below only ever produces PASS or FAIL.
    INCONCLUSIVE is defined here, not added later, because Task 3's
    `requires` propagation (a case whose prerequisite failed or was itself
    inconclusive) needs a third value from this same enum.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    verdict: Verdict
    failures: tuple[str, ...]


# A chunk id is `make_chunk_id(parent_id, ordinal)`: the parent id with a
# `-dddd` ordinal suffix appended (`azgenai_lab.models.rag`). `.+` is greedy,
# so it backtracks to the *last* place a trailing `-\d{4}` fits, stripping
# only that ordinal even if the parent id itself contains hyphens.
_CHUNK_ORDINAL = re.compile(r"(.+)-\d{4}")


def attribute_source(
    chunk_id: str, corpus: Mapping[str, tuple[str, str]]
) -> tuple[str, str] | None:
    """Resolve a chunk id to the `(tenant_id, doc_id)` of its source document.

    `corpus` maps `make_parent_id(tenant_id, doc_id)` (`azgenai_lab.models.rag`)
    to `(tenant_id, doc_id)`, built once by the caller from `load_documents`.
    Mapping to the pair, not to `doc_id` alone, lets one lookup recover both
    fields without this function re-deriving them from the length-prefixed
    parent id format itself.

    This strips exactly the trailing `-\\d{4}` ordinal `make_chunk_id` appends
    and looks the remainder up in `corpus`. Returns `None` when `chunk_id` has
    no such suffix, or the stripped remainder names no document in `corpus` --
    an unattributable source. Callers must score that as its own failure, not
    treat it as an absent source to skip.
    """
    match = _CHUNK_ORDINAL.fullmatch(chunk_id)
    if match is None:
        return None
    return corpus.get(match.group(1))


def evaluate_deterministic(
    case: EvalCase,
    status: Literal["answered", "no_answer"],
    source_chunk_ids: Sequence[str],
    corpus: Mapping[str, tuple[str, str]],
) -> CaseResult:
    """Score one case's deterministic assertions against one run's output.

    Every violated assertion is collected into `failures` -- there is no
    early return, so a run wrong on several axes reports all of them, and
    the verdict is FAIL iff `failures` is non-empty (never INCONCLUSIVE;
    that verdict belongs to Task 3's `requires` propagation).

    Two checks apply to every source unconditionally, with no per-case
    dataset field to opt into them: a source that resolves to no known
    document is a FAIL distinct from a wrong-tenant source, and a source
    attributable to a tenant other than `case.tenant` is always a FAIL --
    whether or not `must_not_cite` happens to name it. Cross-tenant leakage
    is a structural property of the run being scored, not something a case
    author enumerates a document id to catch.
    """
    spec = case.deterministic
    failures: list[str] = []

    if spec.status is not None and status != spec.status:
        failures.append(f"status: expected {spec.status!r}, got {status!r}")

    cited_doc_ids: set[str] = set()
    for chunk_id in source_chunk_ids:
        attributed = attribute_source(chunk_id, corpus)
        if attributed is None:
            failures.append(
                f"unattributable source: chunk id {chunk_id!r} matches no known document"
            )
            continue
        tenant_id, doc_id = attributed
        if tenant_id != case.tenant:
            failures.append(
                f"cross-tenant source: chunk id {chunk_id!r} belongs to tenant "
                f"{tenant_id!r}, case tenant is {case.tenant!r}"
            )
            continue
        cited_doc_ids.add(doc_id)

    missing = sorted(set(spec.must_cite) - cited_doc_ids)
    if missing:
        failures.append(f"must_cite: missing {missing}")

    if spec.citations_subset_of is not None:
        foreign = sorted(cited_doc_ids - set(spec.citations_subset_of))
        if foreign:
            failures.append(f"citations_subset_of: unexpected {foreign}")

    forbidden = sorted(cited_doc_ids & set(spec.must_not_cite))
    if forbidden:
        failures.append(f"must_not_cite: forbidden doc(s) present {forbidden}")

    verdict = Verdict.FAIL if failures else Verdict.PASS
    return CaseResult(case_id=case.id, verdict=verdict, failures=tuple(failures))


def evaluation_order(cases: Sequence[EvalCase]) -> tuple[EvalCase, ...]:
    """Return `cases` in *a* topological order over the `requires` DAG.

    Acyclicity is `load_cases`'s job (`_validate_requires`); this function
    assumes it already holds and does not re-check it. A DAG in general has
    more than one valid topological order (design §7.2, r03: r02's "acyclic
    implies a unique order" was wrong), so this only guarantees that every
    prerequisite precedes its dependents — not any particular order beyond
    that. Ties (cases with no ordering constraint between them) are broken
    by dataset order, i.e. the order `cases` was given in, which is what a
    plain depth-first placement over `cases` in sequence naturally produces.

    This is *not* the order results get reported in -- report order is
    dataset order, produced separately by iterating `cases` itself, not by
    calling this function.
    """
    by_id = {case.id: case for case in cases}
    placed: set[str] = set()
    order: list[EvalCase] = []

    def place(case: EvalCase) -> None:
        if case.id in placed:
            return
        for req_id in case.requires:
            place(by_id[req_id])
        placed.add(case.id)
        order.append(case)

    for case in cases:
        place(case)

    return tuple(order)


def propagate_requires(
    cases: Sequence[EvalCase], results: Mapping[str, CaseResult]
) -> dict[str, CaseResult]:
    """Apply `requires` propagation to one pass's deterministic `results`.

    Design §7.2: propagation is decided purely by the *deterministic*
    verdict of each prerequisite — the judged layer never participates,
    because letting it in would let judged-layer noise contaminate the
    gate. Any prerequisite that is not PASS (FAIL *or* INCONCLUSIVE, r02's
    FAIL-only rule was a gap the r02 review round caught) turns a
    dependent's *own* PASS into INCONCLUSIVE. A dependent that already
    failed on its own merits is never touched: propagation only ever
    introduces INCONCLUSIVE, it never upgrades an existing FAIL into
    anything, and it never downgrades a PASS into anything worse than
    INCONCLUSIVE either.

    Processing happens in `evaluation_order` so that a prerequisite's own
    propagated result is already final by the time a dependent looks it up
    -- that is what makes a transitive chain (A FAIL -> B INCONCLUSIVE ->
    C INCONCLUSIVE) propagate correctly in one pass instead of needing a
    fixed-point loop.

    Returns a new mapping; `results` and `cases` are not mutated. The
    returned mapping preserves `results`'s own key order — it is not
    reordered to `evaluation_order` or to dataset order. Report order is a
    separate concern the caller owns (design §7.2).
    """
    propagated: dict[str, CaseResult] = dict(results)

    for case in evaluation_order(cases):
        own = propagated[case.id]
        if own.verdict == Verdict.FAIL:
            continue

        blocking = [
            req_id for req_id in case.requires if propagated[req_id].verdict != Verdict.PASS
        ]
        if not blocking:
            continue

        reasons = tuple(
            f"requires: prerequisite {req_id!r} is {propagated[req_id].verdict.value}"
            for req_id in blocking
        )
        propagated[case.id] = CaseResult(
            case_id=case.id,
            verdict=Verdict.INCONCLUSIVE,
            failures=own.failures + reasons,
        )

    return propagated


class ExitCode(IntEnum):
    """The runner's process exit code (design §7.5).

    Deliberately three-way, not a bool: `0` means the deterministic gate
    was actually evaluated and every case passed it; `1` means it was
    evaluated and found a problem with the thing under test (a FAIL or an
    INCONCLUSIVE); `2` is reserved for setup/configuration failures that
    happen *before* any verdict exists at all (invalid dataset, corpus
    that will not load, `--judge` requested without credentials) -- a
    later task raises those directly, this enum just names the code they
    exit with. Collapsing `2` into `0` would make a run that never
    executed the gate look green.
    """

    OK = 0
    GATE_FAILED = 1
    SETUP_FAILED = 2


def gate_exit_code(results: Mapping[str, CaseResult]) -> ExitCode:
    """Map deterministic-gate results to a process exit code (design §7.5).

    Takes only the deterministic-layer results mapping -- there is no
    parameter here for judged-layer outcomes, by construction, so a run
    where every case's deterministic verdict is PASS exits 0 regardless of
    what the judged layer (scored separately, never gating) found. The
    judged layer is reported alongside the gate result, never folded into
    it.
    """
    if any(result.verdict != Verdict.PASS for result in results.values()):
        return ExitCode.GATE_FAILED
    return ExitCode.OK


# --- Pass A: corpus-seeded retrieval, execution, calibration (Task 4) ---

# The dataset lives alongside this module, not passed on the command line
# (design §7's usage examples take no --dataset flag): one file, one runner.
_DATASET_PATH = Path(__file__).resolve().parent / "eval_cases.json"


def build_corpus_map(corpus_dir: Path) -> dict[str, tuple[str, str]]:
    """`{make_parent_id(tenant_id, doc_id): (tenant_id, doc_id)}` for every
    document under `corpus_dir` -- the shape `attribute_source` (and
    therefore `evaluate_deterministic`) consumes. Built once per run,
    through the production loader, the same discipline `_corpus_keys` above
    already follows for dataset validation; this is retrieval time's
    counterpart, keyed by parent id rather than left as a bare set.
    """
    return {
        make_parent_id(doc.tenant_id, doc.doc_id): (doc.tenant_id, doc.doc_id)
        for doc in load_documents(corpus_dir)
    }


def build_seeded_rag_service(settings: Settings, *, use_fake_llm: bool) -> RagService:
    """Build a `RagService` whose retriever is seeded from the real sample
    corpus.

    Mirrors `azgenai_lab.services.rag.build_rag_service` -- the same one
    `PromptTemplate` instance goes to `build_chat_service`, to
    `build_audit_attribution`, and to this function's byte-cost calculation
    (Day 22: "a second load of the same file is not the same instance") --
    in every respect except retrieval. `build_rag_service` calls
    `build_retriever`, which in fake mode returns an **empty**
    `FakeSearchClient` (Day 13): a wiring demo over zero documents would
    prove nothing about this dataset's assertions. This calls
    `agent_tools._seeded_fake_retriever` instead (`services/agent_tools.py`),
    reached by module attribute access -- `from azgenai_lab.services import
    agent_tools` above, then `agent_tools._seeded_fake_retriever(...)` below
    -- rather than a `from ... import _seeded_fake_retriever` that would pull
    the private name into this module's own namespace.

    This was a decision, not a default (user, 2026-08-22). The alternatives:
    the public `build_agent_tool_deps` reaches the same retriever but demands
    an unused `conversation_store` and files "the RAG eval's retriever"
    under "agent tool dependencies"; promoting the helper to public would
    overturn this plan's no-production-changes constraint; re-implementing
    the seeding here would be a second encoding of the same rule, which the
    Day 12/15 lesson says drifts from the first. A copy would drift; this
    reuses the one seeding path instead.

    `use_fake_llm` is threaded through a copy of `settings` -- this
    function's only mutation of its input -- rather than as a second branch
    here, so `build_chat_service`'s own `settings.use_fake_llm` check
    remains the single place fake/real chat selection happens.
    """
    run_settings = settings.model_copy(update={"use_fake_llm": use_fake_llm})
    prompt = load_prompt("rag_answer")
    return RagService(
        agent_tools._seeded_fake_retriever(run_settings),
        build_chat_service(run_settings, prompt=prompt),
        instructions_bytes=len(prompt.text.encode("utf-8")),
        audit_attribution=build_audit_attribution(run_settings, prompt),
    )


async def run_pass_a(
    cases: Sequence[EvalCase], service: RagService, corpus: Mapping[str, tuple[str, str]]
) -> dict[str, CaseResult]:
    """Run pass A (design §7.1): every case's question through `service`
    (built with a fake LLM), each answer scored by `evaluate_deterministic`.

    Iterates `cases` in the order given (dataset order, per `load_cases`) --
    scoring one case never depends on another's outcome; only
    `propagate_requires`, run separately by the caller over this function's
    result, does that.
    """
    results: dict[str, CaseResult] = {}
    for case in cases:
        principal = Principal(tenant_id=case.tenant, user_id=case.user, group_ids=case.groups)
        answer = await service.answer(case.question, principal)
        source_chunk_ids = tuple(hit.chunk_id for hit in answer.hits)
        results[case.id] = evaluate_deterministic(case, answer.status, source_chunk_ids, corpus)
    return results


def canonical_json(value: object) -> bytes:
    """Canonical JSON encoding for content-addressing eval artifacts: UTF-8,
    sorted keys, no separator padding, array order preserved (retrieval rank
    is meaningful data, not incidental ordering). Matches
    `reviews/evidence/day28/calibrate_probe.py`'s `canonical_sha256` byte
    for byte. Defined here as this task's first consumer; Task 5 reuses it
    for judge-transcript hashing.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_lab_root(lab_root: Path) -> Path:
    """Fail-closed guard 1/2 (mirrors `calibrate_probe.py`'s `resolve_lab_root`):
    `lab_root` must be a git worktree, and the `azgenai_lab` package actually
    running must live under it -- otherwise this run would stamp one tree's
    commit onto another tree's corpus. Raises `DatasetError`; the caller maps
    that to `ExitCode.SETUP_FAILED`, never `GATE_FAILED` (design §7.5: this
    happens before any verdict exists)."""
    resolved = lab_root.resolve()
    if not (resolved / ".git").exists():
        raise DatasetError(f"--lab-root is not a git worktree: {resolved}")
    package_root = Path(azgenai_lab.__file__).resolve().parent
    if not package_root.is_relative_to(resolved):
        raise DatasetError(
            "the imported azgenai_lab does not live under --lab-root "
            f"(imported from {package_root}, lab root {resolved}); this run "
            "would stamp one tree's commit onto another tree's corpus"
        )
    return resolved


def _resolve_corpus_dir(settings: Settings, lab_root: Path) -> Path:
    """Fail-closed guard 2/2: the corpus `settings` resolves to must be the
    one under `lab_root`. Raises `DatasetError` otherwise (see
    `_resolve_lab_root`)."""
    from_settings = Path(settings.sample_docs_dir or SAMPLE_DOCS_DIR).resolve()
    expected = (lab_root / "data" / "sample-docs").resolve()
    if from_settings != expected:
        raise DatasetError(
            f"settings resolve the corpus to {from_settings}, but --lab-root "
            f"implies {expected}"
        )
    return expected


def _git_describe(lab_root: Path) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=lab_root, check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=lab_root, check=False
    ).stdout.strip()
    return commit + ("+dirty" if dirty else "")


async def calibration_document(
    cases: Sequence[EvalCase], service: RagService, settings: Settings, lab_root: Path
) -> dict[str, object]:
    """Emit the same document shape as
    `reviews/evidence/day28/calibrate_probe.py`'s output: retrieval-only
    observations for every case, plus everything needed to tell whether a
    later run saw the same corpus, dataset wording, and settings this
    dataset was calibrated against.

    Deliberately reads `service`'s retriever directly
    (`service._retriever.retrieve(...)` -- the same access pattern
    `RagService`'s own module already sanctions for tests that need the
    real composed value, per its docstring on `audit_attribution`) rather
    than calling `service.answer()`: calibration exists to "fix the literal
    question wording" against retrieval (design §4), not against a
    generated answer, and `service.answer()` would also invoke the chat
    service and, for status "answered", could trim hits to the prompt byte
    budget -- a result this document does not claim to reproduce.

    Same fail-closed environment guards as the probe: see
    `_resolve_lab_root` / `_resolve_corpus_dir`.
    """
    resolved_root = _resolve_lab_root(lab_root)
    corpus_dir = _resolve_corpus_dir(settings, resolved_root)

    observations: list[dict[str, object]] = []
    for case in cases:
        principal = Principal(tenant_id=case.tenant, user_id=case.user, group_ids=case.groups)
        result = await service._retriever.retrieve(case.question, principal)
        observations.append(
            {
                "id": case.id,
                "question": case.question,
                "principal": {
                    "tenant": case.tenant,
                    "groups_count": len(case.groups),
                    # Day 15: group ids never enter logs or evidence.
                    "group_sha256": [_sha256_hex(group.encode("utf-8")) for group in case.groups],
                },
                "hit_count": len(result.hits),
                "hits": [
                    {
                        "rank": rank,
                        "chunk_id": hit.chunk_id,
                        "score": hit.score,
                        "heading_path": hit.heading_path,
                        "content_sha256": _sha256_hex(hit.content.encode("utf-8")),
                    }
                    for rank, hit in enumerate(result.hits, start=1)
                ],
            }
        )

    document: dict[str, object] = {
        "kind": "day28-offline-calibration",
        "lab_commit": _git_describe(resolved_root),
        "corpus_dir": str(corpus_dir.relative_to(resolved_root)),
        "corpus_sha256": {
            str(path.relative_to(corpus_dir)): _sha256_hex(path.read_bytes())
            for path in sorted(corpus_dir.glob("*/*.md"))
        },
        "questions_sha256": _sha256_hex(_DATASET_PATH.read_bytes()),
        "settings": {
            "rag_top": settings.rag_top,
            "chunk_max_chars": settings.chunk_max_chars,
            "chunk_overlap_chars": settings.chunk_overlap_chars,
            # Seeding always uses FakeEmbeddingClient regardless of
            # settings.use_fake_embeddings (agent_tools._seeded_fake_retriever's
            # own docstring) -- a literal, not settings.use_fake_embeddings.
            "use_fake_embeddings_for_seed": True,
        },
        "note": (
            "FakeSearchClient scores lexically in every mode; these hits say "
            "nothing about real Search retrieval quality. They exist only to "
            "fix the literal question wording of the Day 28 dataset."
        ),
        "observations": observations,
    }
    document["observations_sha256"] = _sha256_hex(canonical_json(observations))
    return document


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Day 28 golden-question evaluation runner (deterministic pass A)."
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help=(
            "Emit a machine-captured retrieval calibration document (same "
            "shape as reviews/evidence/day28/*-offline-calibration.json) "
            "instead of running the deterministic gate."
        ),
    )
    parser.add_argument(
        "--lab-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repo root the corpus and git identity are read from (--calibrate only).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    corpus_dir = Path(settings.sample_docs_dir or SAMPLE_DOCS_DIR)

    try:
        cases = load_cases(_DATASET_PATH, corpus_dir)
    except DatasetError as exc:
        print(f"SETUP FAILURE: {exc}", file=sys.stderr)
        return ExitCode.SETUP_FAILED

    service = build_seeded_rag_service(settings, use_fake_llm=True)
    try:
        if args.calibrate:
            try:
                document = asyncio.run(
                    calibration_document(cases, service, settings, args.lab_root)
                )
            except DatasetError as exc:
                print(f"SETUP FAILURE: {exc}", file=sys.stderr)
                return ExitCode.SETUP_FAILED
            print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
            return ExitCode.OK

        corpus = build_corpus_map(corpus_dir)
        results = asyncio.run(run_pass_a(cases, service, corpus))
        propagated = propagate_requires(cases, results)
        for case in cases:
            result = propagated[case.id]
            print(f"{result.verdict.value}\t{case.id}")
            for failure in result.failures:
                print(f"\t{failure}")
        return gate_exit_code(propagated)
    finally:
        asyncio.run(service.aclose())


if __name__ == "__main__":
    sys.exit(main())
