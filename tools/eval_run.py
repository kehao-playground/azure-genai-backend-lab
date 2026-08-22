"""Day 28 GenAI evaluation: golden-question dataset and runner.

This module currently implements the dataset section only: the frozen
dataclasses describing one evaluation case, and `load_cases`, which loads
and validates `tools/eval_cases.json` against the real sample corpus (design
`drafts/research/day-28-evaluation.md` r04, §5/§6/§8; implementation plan
`plans/day-28-implementation-plan.md` Task 1). Later tasks append the
deterministic evaluators, `requires` propagation and exit codes, the judge
contract, and orchestration/reporting to this same file — they are
deliberately absent here, not stubbed.

`tools/` is not an installed package (no `tools/__init__.py`); tests load
this module by path, the same pattern `tests/unit/test_prompt_shields_probe.py`
uses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from azgenai_lab.models.principal import validate_identifier
from azgenai_lab.services.document_loader import load_documents


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
