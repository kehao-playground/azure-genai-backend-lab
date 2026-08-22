"""Tests for `tools/eval_run.py`'s deterministic assertion evaluators (Task 2).

Table-driven: one test per rule in the design (`drafts/research/day-28-evaluation.md`
r04 §8) plus a few direct `attribute_source` tests for its own branches.

`tools/` is not a package (no `tools/__init__.py`), so the module is loaded
by path, the same pattern `tests/unit/test_eval_cases.py` uses.
"""

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path

from azgenai_lab.models.rag import make_chunk_id, make_parent_id

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "eval_run.py"
_SPEC = importlib.util.spec_from_file_location("eval_run", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
eval_run = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = eval_run
_SPEC.loader.exec_module(eval_run)

DeterministicSpec = eval_run.DeterministicSpec
EvalCase = eval_run.EvalCase
Verdict = eval_run.Verdict
attribute_source = eval_run.attribute_source
evaluate_deterministic = eval_run.evaluate_deterministic


# --- fixtures: a corpus map and minimal EvalCase/chunk-id builders ---


def _corpus_map(pairs: Sequence[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """`{make_parent_id(tenant, doc): (tenant, doc)}` for each pair -- the
    shape `attribute_source` consumes, built with the production helper
    rather than a hand-rolled key format."""
    return {make_parent_id(tenant, doc): (tenant, doc) for tenant, doc in pairs}


def _chunk_id(tenant: str, doc_id: str, ordinal: int = 0) -> str:
    return make_chunk_id(make_parent_id(tenant, doc_id), ordinal)


# doc-a exists under both acme and globex with the *same* doc_id, so tests
# can prove cross-tenant filtering is keyed on tenant, not just doc_id.
_CORPUS = _corpus_map([("acme", "doc-a"), ("acme", "doc-b"), ("globex", "doc-a")])


def _det(**overrides: object) -> DeterministicSpec:
    base: dict[str, object] = {
        "status": None,
        "status_note": None,
        "must_cite": (),
        "citations_subset_of": None,
        "subset_note": None,
        "must_not_cite": (),
    }
    base.update(overrides)
    return DeterministicSpec(**base)  # type: ignore[arg-type]


def _case(case_id: str = "case-1", tenant: str = "acme", **det_overrides: object) -> EvalCase:
    return EvalCase(
        id=case_id,
        question="does not matter for these tests",
        tenant=tenant,
        user="eval-agent",
        groups=(),
        protects="test coverage",
        requires=(),
        deterministic=_det(**det_overrides),
        judged=None,
        judged_skip_reason="deterministic-only test",
    )


# --- status ---


def test_status_match_passes() -> None:
    result = evaluate_deterministic(_case(status="answered"), "answered", [], _CORPUS)
    assert result.verdict == Verdict.PASS
    assert result.failures == ()


def test_status_mismatch_fails() -> None:
    result = evaluate_deterministic(_case(status="answered"), "no_answer", [], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert len(result.failures) == 1
    assert "status" in result.failures[0]


def test_status_none_never_fails() -> None:
    result = evaluate_deterministic(_case(status=None), "no_answer", [], _CORPUS)
    assert result.verdict == Verdict.PASS


# --- must_cite ---


def test_must_cite_satisfied_passes() -> None:
    case = _case(must_cite=("doc-a",))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.PASS


def test_must_cite_one_required_doc_missing_fails() -> None:
    case = _case(must_cite=("doc-a", "doc-b"))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("must_cite" in f and "doc-b" in f for f in result.failures)


# --- citations_subset_of ---


def test_citations_subset_of_satisfied_passes() -> None:
    case = _case(citations_subset_of=("doc-a", "doc-b"))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.PASS


def test_citations_subset_of_foreign_doc_fails() -> None:
    case = _case(citations_subset_of=("doc-a",))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-b")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("citations_subset_of" in f and "doc-b" in f for f in result.failures)


def test_citations_subset_of_empty_with_sources_fails() -> None:
    case = _case(citations_subset_of=())
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("citations_subset_of" in f for f in result.failures)


def test_citations_subset_of_empty_with_no_sources_passes() -> None:
    case = _case(citations_subset_of=())
    result = evaluate_deterministic(case, "no_answer", [], _CORPUS)
    assert result.verdict == Verdict.PASS


def test_citations_subset_of_none_never_fails() -> None:
    case = _case(citations_subset_of=None)
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-b")], _CORPUS)
    assert result.verdict == Verdict.PASS


# --- must_not_cite ---


def test_must_not_cite_satisfied_passes() -> None:
    case = _case(must_not_cite=("doc-b",))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.PASS


def test_must_not_cite_forbidden_doc_present_fails() -> None:
    case = _case(must_not_cite=("doc-b",))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-b")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("must_not_cite" in f and "doc-b" in f for f in result.failures)


# --- global cross-tenant check (no per-case field) ---


def test_cross_tenant_source_fails_even_when_no_field_mentions_it() -> None:
    # globex's doc-a shares a doc_id with acme's doc-a but is a different
    # tenant's document; nothing in this case's deterministic spec names it.
    case = _case(tenant="acme")
    result = evaluate_deterministic(case, "answered", [_chunk_id("globex", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("cross-tenant" in f and "globex" in f for f in result.failures)


def test_cross_tenant_source_does_not_satisfy_must_cite_by_doc_id() -> None:
    # Same doc_id as an acme document the case requires, but under globex --
    # must_cite must not be satisfied by a same-named foreign-tenant chunk.
    case = _case(tenant="acme", must_cite=("doc-a",))
    result = evaluate_deterministic(case, "answered", [_chunk_id("globex", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("must_cite" in f and "doc-a" in f for f in result.failures)
    assert any("cross-tenant" in f for f in result.failures)


# --- unattributable source ---


def test_unattributable_source_fails_with_a_distinct_message() -> None:
    case = _case()
    result = evaluate_deterministic(case, "answered", ["no-such-chunk-0001"], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert any("unattributable" in f for f in result.failures)
    assert not any("cross-tenant" in f for f in result.failures)


# --- no early return: all violated assertions are reported ---


def test_three_violations_all_reported_no_early_return() -> None:
    case = _case(status="no_answer", must_cite=("doc-b",), must_not_cite=("doc-a",))
    result = evaluate_deterministic(case, "answered", [_chunk_id("acme", "doc-a")], _CORPUS)
    assert result.verdict == Verdict.FAIL
    assert len(result.failures) == 3
    joined = " | ".join(result.failures)
    assert "status" in joined
    assert "must_cite" in joined
    assert "must_not_cite" in joined


# --- attribute_source, directly ---


def test_attribute_source_known_chunk_returns_tenant_and_doc_id() -> None:
    assert attribute_source(_chunk_id("acme", "doc-a", 3), _CORPUS) == ("acme", "doc-a")


def test_attribute_source_unknown_parent_returns_none() -> None:
    unknown = make_chunk_id(make_parent_id("acme", "doc-does-not-exist"), 0)
    assert attribute_source(unknown, _CORPUS) is None


def test_attribute_source_no_ordinal_suffix_returns_none() -> None:
    assert attribute_source(make_parent_id("acme", "doc-a"), _CORPUS) is None
