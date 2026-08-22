"""Tests for `tools/eval_run.py`'s deterministic assertion evaluators (Task 2),
`requires` propagation / exit codes (Task 3), pass A -- the corpus-seeded
`RagService`, its execution over the dataset, and `--calibrate` (Task 4) --
and the judge contract's input fencing, canonical hashing, and
invariant-checked strict parsing (Task 5).

Table-driven: one test per rule in the design (`drafts/research/day-28-evaluation.md`
r04 §8/§7.2/§7.3/§7.5) plus a few direct `attribute_source` tests for its own
branches. The Task 4 section below runs the real seeded pipeline end to end
(`USE_FAKE_LLM=true`, no Azure resources -- `tests/conftest.py` pins the three
fake-adapter flags for the whole suite). The Task 5 section is pure --
no `RagService`, no provider call; it exercises `build_judge_input` /
`parse_judge_response` / `derive_judge_verdict` against hand-built
`SearchHit`s and raw response strings only.

`tools/` is not a package (no `tools/__init__.py`), so the module is loaded
by path, the same pattern `tests/unit/test_eval_cases.py` uses.
"""

import importlib.util
import inspect
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.rag import make_chunk_id, make_parent_id
from azgenai_lab.models.search import SearchHit
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR
from azgenai_lab.services.rag import build_rag_service

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "eval_run.py"
_SPEC = importlib.util.spec_from_file_location("eval_run", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
eval_run = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = eval_run
_SPEC.loader.exec_module(eval_run)

CaseResult = eval_run.CaseResult
DatasetError = eval_run.DatasetError
DeterministicSpec = eval_run.DeterministicSpec
EvalCase = eval_run.EvalCase
ExitCode = eval_run.ExitCode
Verdict = eval_run.Verdict
attribute_source = eval_run.attribute_source
evaluate_deterministic = eval_run.evaluate_deterministic
evaluation_order = eval_run.evaluation_order
gate_exit_code = eval_run.gate_exit_code
propagate_requires = eval_run.propagate_requires
Fact = eval_run.Fact
JudgedSpec = eval_run.JudgedSpec
JudgeOutput = eval_run.JudgeOutput
JudgeParseError = eval_run.JudgeParseError
answer_sha256 = eval_run.answer_sha256
build_judge_input = eval_run.build_judge_input
derive_judge_verdict = eval_run.derive_judge_verdict
judge_prompt_template = eval_run.judge_prompt_template
parse_judge_response = eval_run.parse_judge_response
sha256_hex = eval_run.sha256_hex
sources_sha256 = eval_run.sources_sha256

# The real lab worktree these tests run in -- test_eval_run.py lives at
# tests/unit/, two levels below the repo root.
_LAB_ROOT = Path(__file__).resolve().parents[2]


def _fresh_settings() -> Settings:
    """Settings isolated from the repo-root `.env` (same discipline
    `test_agent_toolset.py::_settings` uses): only `conftest.py`'s three
    pinned fake-adapter flags are guaranteed, everything else is still
    ambient without `_env_file=None`."""
    return Settings(_env_file=None)


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


def _case(
    case_id: str = "case-1",
    tenant: str = "acme",
    requires: tuple[str, ...] = (),
    **det_overrides: object,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        question="does not matter for these tests",
        tenant=tenant,
        user="eval-agent",
        groups=(),
        protects="test coverage",
        requires=requires,
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


# --- requires propagation (Task 3, design §7.2) ---


def _result(case_id: str, verdict: "Verdict", failures: tuple[str, ...] = ()) -> "CaseResult":
    return CaseResult(case_id=case_id, verdict=verdict, failures=failures)


def test_requires_fail_prerequisite_makes_dependent_inconclusive() -> None:
    a = _case("a")
    b = _case("b", requires=("a",))
    results = {
        "a": _result("a", Verdict.FAIL, ("prereq boom",)),
        "b": _result("b", Verdict.PASS),
    }
    propagated = propagate_requires([a, b], results)
    assert propagated["a"].verdict == Verdict.FAIL
    assert propagated["b"].verdict == Verdict.INCONCLUSIVE


def test_requires_inconclusive_prerequisite_makes_dependent_inconclusive() -> None:
    # The r02 design gap (§7.2): propagation must also fire when the
    # prerequisite is INCONCLUSIVE, not only when it is FAIL. Here "a" is
    # handed to propagate_requires as already INCONCLUSIVE -- not derived
    # transitively -- to isolate this branch from the transitive case below.
    a = _case("a")
    b = _case("b", requires=("a",))
    results = {
        "a": _result("a", Verdict.INCONCLUSIVE),
        "b": _result("b", Verdict.PASS),
    }
    propagated = propagate_requires([a, b], results)
    assert propagated["b"].verdict == Verdict.INCONCLUSIVE


def test_requires_pass_prerequisite_keeps_dependents_own_verdict() -> None:
    a = _case("a")
    b = _case("b", requires=("a",))
    c = _case("c", requires=("a",))
    results = {
        "a": _result("a", Verdict.PASS),
        "b": _result("b", Verdict.PASS),
        "c": _result("c", Verdict.FAIL, ("own failure",)),
    }
    propagated = propagate_requires([a, b, c], results)
    assert propagated["b"].verdict == Verdict.PASS
    assert propagated["c"].verdict == Verdict.FAIL
    assert propagated["c"].failures == ("own failure",)


def test_requires_two_prerequisites_one_pass_one_fail_is_inconclusive() -> None:
    a = _case("a")
    b = _case("b")
    c = _case("c", requires=("a", "b"))
    results = {
        "a": _result("a", Verdict.PASS),
        "b": _result("b", Verdict.FAIL, ("boom",)),
        "c": _result("c", Verdict.PASS),
    }
    propagated = propagate_requires([a, b, c], results)
    assert propagated["c"].verdict == Verdict.INCONCLUSIVE


def test_requires_transitive_propagation_a_fail_b_inconclusive_c_inconclusive() -> None:
    a = _case("a")
    b = _case("b", requires=("a",))
    c = _case("c", requires=("b",))
    results = {
        "a": _result("a", Verdict.FAIL, ("boom",)),
        "b": _result("b", Verdict.PASS),
        "c": _result("c", Verdict.PASS),
    }
    propagated = propagate_requires([a, b, c], results)
    assert propagated["a"].verdict == Verdict.FAIL
    assert propagated["b"].verdict == Verdict.INCONCLUSIVE
    assert propagated["c"].verdict == Verdict.INCONCLUSIVE


def test_requires_already_failing_dependent_stays_fail_not_upgraded() -> None:
    # Propagation must never turn an existing FAIL into INCONCLUSIVE --
    # that would be a downgrade of severity, not a propagation.
    a = _case("a")
    b = _case("b", requires=("a",))
    results = {
        "a": _result("a", Verdict.FAIL, ("prereq boom",)),
        "b": _result("b", Verdict.FAIL, ("own boom",)),
    }
    propagated = propagate_requires([a, b], results)
    assert propagated["b"].verdict == Verdict.FAIL
    assert propagated["b"].failures == ("own boom",)


def test_evaluation_order_places_every_prerequisite_before_its_dependents() -> None:
    a = _case("a")
    b = _case("b", requires=("a",))
    c = _case("c", requires=("b", "a"))
    # Dataset order is deliberately the reverse of dependency order.
    order = evaluation_order([c, b, a])
    index = {case.id: i for i, case in enumerate(order)}
    assert index["a"] < index["b"] < index["c"]
    assert {case.id for case in order} == {"a", "b", "c"}


def test_evaluation_order_ties_broken_by_dataset_order() -> None:
    a = _case("a")
    b = _case("b")
    assert evaluation_order([b, a]) == (b, a)
    assert evaluation_order([a, b]) == (a, b)


def test_propagate_requires_preserves_results_key_order_not_evaluation_order() -> None:
    # b depends on a, but a is declared *after* b in this dataset/results
    # order. Report order (design §7.2) is dataset order, produced
    # separately by the caller -- propagate_requires must not reorder the
    # results mapping to match evaluation_order (which would put a first).
    a = _case("a")
    b = _case("b", requires=("a",))
    dataset_order = [b, a]
    results = {
        "b": _result("b", Verdict.PASS),
        "a": _result("a", Verdict.PASS),
    }
    propagated = propagate_requires(dataset_order, results)
    assert list(propagated.keys()) == ["b", "a"]


# --- exit codes (Task 3, design §7.5) ---


def test_gate_exit_code_all_pass_is_ok() -> None:
    results = {
        "a": _result("a", Verdict.PASS),
        "b": _result("b", Verdict.PASS),
    }
    assert gate_exit_code(results) == ExitCode.OK
    assert gate_exit_code(results) == 0


def test_gate_exit_code_one_fail_is_gate_failed() -> None:
    results = {
        "a": _result("a", Verdict.PASS),
        "b": _result("b", Verdict.FAIL, ("boom",)),
    }
    assert gate_exit_code(results) == ExitCode.GATE_FAILED
    assert gate_exit_code(results) == 1


def test_gate_exit_code_one_inconclusive_is_gate_failed() -> None:
    results = {
        "a": _result("a", Verdict.PASS),
        "b": _result("b", Verdict.INCONCLUSIVE),
    }
    assert gate_exit_code(results) == ExitCode.GATE_FAILED
    assert gate_exit_code(results) == 1


def test_gate_exit_code_signature_takes_only_deterministic_results() -> None:
    # "Judged-layer outcomes are not an input to gate_exit_code at all" is
    # a claim about the function's signature, not just its behavior --
    # assert that directly rather than only inferring it from a passing
    # test.
    sig = inspect.signature(gate_exit_code)
    assert list(sig.parameters) == ["results"]
    annotation = str(sig.parameters["results"].annotation)
    assert "CaseResult" in annotation
    assert "judged" not in annotation.lower()


def test_gate_exit_code_ignores_judged_layer_failure_still_exits_zero() -> None:
    # CaseResult carries no judged-layer field at all (Task 3 brief
    # ambiguity 1): a case whose judged layer failed elsewhere in the
    # pipeline is represented here only by its deterministic verdict. As
    # long as that verdict is PASS, the gate exits 0 -- there is no way to
    # even construct a CaseResult that says "judged failed" and feed it in.
    results = {
        "case-with-a-failing-judged-layer": _result(
            "case-with-a-failing-judged-layer", Verdict.PASS
        ),
    }
    assert gate_exit_code(results) == ExitCode.OK


# --- Task 4: seeded RagService, pass A execution, --calibrate ---


def _case_by_id(case_id: str) -> EvalCase:
    cases = eval_run.load_cases(eval_run._DATASET_PATH, SAMPLE_DOCS_DIR)
    for case in cases:
        if case.id == case_id:
            return case
    raise AssertionError(f"no shipped case named {case_id!r}")


async def test_seeded_service_answers_acme_refund_window_with_returns_policy_sources() -> None:
    service = eval_run.build_seeded_rag_service(_fresh_settings(), use_fake_llm=True)
    corpus = eval_run.build_corpus_map(SAMPLE_DOCS_DIR)
    try:
        answer = await service.answer(
            "How many days does a customer have to return a standard purchase "
            "for a full refund?",
            Principal(tenant_id="acme", user_id="eval-agent", group_ids=()),
        )
    finally:
        await service.aclose()

    assert answer.status == "answered"
    assert answer.hits  # never assert on an empty result set
    for hit in answer.hits:
        assert attribute_source(hit.chunk_id, corpus) == ("acme", "returns-policy")


async def test_seeded_service_returns_no_answer_with_zero_sources_for_nonsense_question() -> None:
    service = eval_run.build_seeded_rag_service(_fresh_settings(), use_fake_llm=True)
    try:
        answer = await service.answer(
            "quantum ferret provisioning throughput",
            Principal(tenant_id="acme", user_id="eval-agent", group_ids=()),
        )
    finally:
        await service.aclose()

    assert answer.status == "no_answer"
    assert answer.hits == ()


async def test_seeded_service_gates_oncall_runbook_on_the_oncall_group() -> None:
    service = eval_run.build_seeded_rag_service(_fresh_settings(), use_fake_llm=True)
    corpus = eval_run.build_corpus_map(SAMPLE_DOCS_DIR)
    question = "How quickly must the on-call engineer acknowledge a page?"
    try:
        granted = await service.answer(
            question, Principal(tenant_id="globex", user_id="eval-agent", group_ids=("oncall",))
        )
        denied = await service.answer(
            question, Principal(tenant_id="globex", user_id="eval-agent", group_ids=())
        )
    finally:
        await service.aclose()

    granted_docs = {attribute_source(hit.chunk_id, corpus) for hit in granted.hits}
    assert ("globex", "oncall-runbook") in granted_docs

    # "Never assert on an empty result set" (tools/tenant_smoke.py:9-24): the
    # denied principal must still retrieve *something* -- the assertion is
    # that the gated document specifically is absent, not that nothing came
    # back at all.
    assert denied.hits
    denied_docs = {attribute_source(hit.chunk_id, corpus) for hit in denied.hits}
    assert ("globex", "oncall-runbook") not in denied_docs


async def test_stock_build_rag_service_has_no_seeded_documents() -> None:
    # Regression guard for design §2's whole reason to exist: build_retriever
    # (via build_rag_service) hits an *empty* FakeSearchClient in fake mode
    # (Day 13) -- without the seeded retriever, even a case the seeded
    # service answers cleanly comes back no_answer.
    service = build_rag_service(_fresh_settings())
    try:
        answer = await service.answer(
            "How many days does a customer have to return a standard purchase "
            "for a full refund?",
            Principal(tenant_id="acme", user_id="eval-agent", group_ids=()),
        )
    finally:
        await service.aclose()

    assert answer.status == "no_answer"
    assert answer.hits == ()


def test_build_corpus_map_uses_make_parent_id_keys() -> None:
    corpus = eval_run.build_corpus_map(SAMPLE_DOCS_DIR)
    assert corpus[make_parent_id("acme", "returns-policy")] == ("acme", "returns-policy")
    assert corpus[make_parent_id("globex", "oncall-runbook")] == ("globex", "oncall-runbook")


async def test_run_pass_a_scores_every_shipped_case_pass() -> None:
    # End-to-end: the actual dataset, the actual corpus, the actual seeded
    # service -- this is "make it actually run" exercised in full, not a
    # synthetic fixture.
    settings = _fresh_settings()
    corpus_dir = Path(settings.sample_docs_dir or SAMPLE_DOCS_DIR)
    cases = eval_run.load_cases(eval_run._DATASET_PATH, corpus_dir)
    corpus = eval_run.build_corpus_map(corpus_dir)
    service = eval_run.build_seeded_rag_service(settings, use_fake_llm=True)
    try:
        results = await eval_run.run_pass_a(cases, service, corpus)
    finally:
        await service.aclose()

    assert set(results) == {case.id for case in cases}
    for case_id, result in results.items():
        assert result.verdict == Verdict.PASS, (case_id, result.failures)


# --- canonical_json ---


def test_canonical_json_is_stable_under_key_reordering() -> None:
    assert eval_run.canonical_json({"b": 1, "a": 2}) == eval_run.canonical_json({"a": 2, "b": 1})


def test_canonical_json_is_not_stable_under_a_whitespace_change_inside_a_value() -> None:
    assert eval_run.canonical_json({"a": "x y"}) != eval_run.canonical_json({"a": "x  y"})


def test_canonical_json_preserves_array_order() -> None:
    assert eval_run.canonical_json([1, 2, 3]) != eval_run.canonical_json([3, 2, 1])


def test_canonical_json_uses_compact_separators_and_keeps_unicode() -> None:
    encoded = eval_run.canonical_json({"a": 1, "b": "café"})
    assert encoded == b'{"a":1,"b":"caf\xc3\xa9"}'


# --- calibration_document: fail-closed guards ---


async def test_calibration_document_rejects_a_lab_root_that_is_not_a_git_worktree(
    tmp_path: Path,
) -> None:
    service = eval_run.build_seeded_rag_service(_fresh_settings(), use_fake_llm=True)
    try:
        with pytest.raises(DatasetError, match="not a git worktree"):
            await eval_run.calibration_document([], service, _fresh_settings(), tmp_path)
    finally:
        await service.aclose()


async def test_calibration_document_rejects_a_lab_root_whose_corpus_settings_disagree() -> None:
    # The private planning repo one level up: a real git worktree that
    # azgenai_lab *does* live under (so both checks in _resolve_lab_root
    # pass -- package_root is relative_to this ancestor too), but whose
    # data/sample-docs is not where the settings-resolved corpus actually
    # is (that's one level down, inside the lab checkout). This isolates
    # _resolve_corpus_dir's own guard from _resolve_lab_root's: a lab_root
    # of a fresh empty tmp_path repo would already fail the package-root
    # check above and never reach this one.
    planning_root = _LAB_ROOT.parent
    assert (planning_root / ".git").exists()
    service = eval_run.build_seeded_rag_service(_fresh_settings(), use_fake_llm=True)
    try:
        with pytest.raises(DatasetError, match="settings resolve the corpus to"):
            await eval_run.calibration_document([], service, _fresh_settings(), planning_root)
    finally:
        await service.aclose()


async def test_calibration_document_matches_shape_for_the_real_lab_root() -> None:
    settings = _fresh_settings()
    cases = [
        _case_by_id("acme-refund-window-standard"),
        _case_by_id("zero-hit-structural-no-answer"),
    ]
    service = eval_run.build_seeded_rag_service(settings, use_fake_llm=True)
    try:
        document = await eval_run.calibration_document(cases, service, settings, _LAB_ROOT)
    finally:
        await service.aclose()

    assert document["kind"] == "day28-offline-calibration"
    assert document["corpus_dir"] == "data/sample-docs"
    assert isinstance(document["lab_commit"], str) and document["lab_commit"]
    corpus_sha256 = document["corpus_sha256"]
    assert isinstance(corpus_sha256, dict)
    assert set(corpus_sha256) == {
        "acme/returns-policy.md",
        "acme/service-sla.md",
        "globex/billing-faq.md",
        "globex/oncall-runbook.md",
        "opsdemo/error-contract.md",
        "opsdemo/streaming-sse.md",
        "opsdemo/token-budget.md",
    }
    assert document["settings"] == {
        "rag_top": settings.rag_top,
        "chunk_max_chars": settings.chunk_max_chars,
        "chunk_overlap_chars": settings.chunk_overlap_chars,
        "use_fake_embeddings_for_seed": True,
    }

    observations = document["observations"]
    assert isinstance(observations, list)
    by_id = {obs["id"]: obs for obs in observations}
    assert by_id.keys() == {"acme-refund-window-standard", "zero-hit-structural-no-answer"}

    answered = by_id["acme-refund-window-standard"]
    assert answered["hit_count"] > 0
    assert answered["hits"][0]["chunk_id"] == "t4=acmed14=returns-policy-0001"
    assert answered["principal"] == {"tenant": "acme", "groups_count": 0, "group_sha256": []}

    zero_hit = by_id["zero-hit-structural-no-answer"]
    assert zero_hit["hit_count"] == 0
    assert zero_hit["hits"] == []

    # observations_sha256 is derivable, not just present.
    assert document["observations_sha256"] == eval_run.sha256_hex(
        eval_run.canonical_json(observations)
    )


# --- main(): CLI wiring ---


def test_main_calibrate_exits_setup_failed_not_gate_failed_on_a_bad_lab_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = eval_run.main(["--calibrate", "--lab-root", str(tmp_path)])
    assert exit_code == ExitCode.SETUP_FAILED
    assert exit_code != ExitCode.GATE_FAILED
    captured = capsys.readouterr()
    assert "SETUP FAILURE" in captured.err


def test_main_calibrate_prints_the_document_for_the_real_lab_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = eval_run.main(["--calibrate", "--lab-root", str(_LAB_ROOT)])
    assert exit_code == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["kind"] == "day28-offline-calibration"
    assert {obs["id"] for obs in document["observations"]} == {
        case.id for case in eval_run.load_cases(eval_run._DATASET_PATH, SAMPLE_DOCS_DIR)
    }


def test_main_default_run_gates_on_the_real_dataset_and_exits_ok() -> None:
    assert eval_run.main([]) == ExitCode.OK


# --- Task 5: judge contract (design §7.3) ---
#
# Pure -- no RagService, no provider call. build_judge_input / parse_judge_
# response / derive_judge_verdict are exercised against hand-built EvalCases,
# SearchHits, and raw response strings only.


def _fact(fact_id: str, text: str = "some fact text") -> "Fact":
    return Fact(id=fact_id, text=text)


def _judged_case(
    case_id: str = "judged-case",
    expected: tuple["Fact", ...] = (),
    forbidden: tuple["Fact", ...] = (),
) -> EvalCase:
    return EvalCase(
        id=case_id,
        question="does this answer cover the expected facts?",
        tenant="acme",
        user="eval-agent",
        groups=(),
        protects="test coverage",
        requires=(),
        deterministic=_det(),
        judged=JudgedSpec(expected_facts=expected, forbidden_facts=forbidden, rubric=None),
        judged_skip_reason=None,
    )


def _hit(
    tenant: str = "acme",
    doc_id: str = "doc-a",
    ordinal: int = 0,
    heading_path: str = "Doc A > Section",
    content: str = "some content",
    score: float = 1.0,
) -> SearchHit:
    parent_id = make_parent_id(tenant, doc_id)
    return SearchHit(
        chunk_id=make_chunk_id(parent_id, ordinal),
        parent_id=parent_id,
        title="Doc A",
        heading_path=heading_path,
        content=content,
        score=score,
    )


# --- sha256_hex / answer_sha256 ---


def test_sha256_hex_matches_hashlib() -> None:
    import hashlib

    assert sha256_hex(b"hello") == hashlib.sha256(b"hello").hexdigest()


def test_answer_sha256_is_sha256_of_the_answers_own_utf8_bytes() -> None:
    assert answer_sha256("hello") == sha256_hex(b"hello")


def test_answer_sha256_differs_on_a_whitespace_only_change() -> None:
    assert answer_sha256("a b") != answer_sha256("a  b")


# --- sources_sha256 ---


def test_sources_sha256_unchanged_when_only_score_changes() -> None:
    hit_a = _hit(score=1.0)
    hit_b = _hit(score=99.0)
    assert sources_sha256([hit_a]) == sources_sha256([hit_b])


def test_sources_sha256_changes_when_content_changes() -> None:
    hit_a = _hit(content="alpha")
    hit_b = _hit(content="beta")
    assert sources_sha256([hit_a]) != sources_sha256([hit_b])


def test_sources_sha256_changes_when_two_hits_swap_rank() -> None:
    hit_a = _hit(doc_id="doc-a", content="alpha")
    hit_b = _hit(doc_id="doc-b", content="beta")
    assert sources_sha256([hit_a, hit_b]) != sources_sha256([hit_b, hit_a])


def test_sources_sha256_is_canonical_json_of_doc_id_chunk_id_heading_path_content() -> None:
    hit = _hit(
        tenant="acme", doc_id="doc-a", ordinal=3, heading_path="Doc A > S1", content="body text"
    )
    payload = [
        {
            "doc_id": "doc-a",
            "chunk_id": hit.chunk_id,
            "heading_path": "Doc A > S1",
            "content": "body text",
        }
    ]
    assert sources_sha256([hit]) == sha256_hex(eval_run.canonical_json(payload))


# --- build_judge_input: nonce fencing ---


def test_build_judge_input_embeds_the_nonce_in_the_answer_fence() -> None:
    case = _judged_case(expected=(_fact("f1"),))
    payload = build_judge_input(case, "the answer text", [], nonce="abc123")
    answer = payload["answer"]
    assert isinstance(answer, str)
    assert "BEGIN UNTRUSTED ANSWER abc123" in answer
    assert "END UNTRUSTED ANSWER abc123" in answer
    assert "the answer text" in answer


def test_build_judge_input_embeds_the_nonce_in_each_source_content_fence() -> None:
    case = _judged_case(expected=(_fact("f1"),))
    hit1 = _hit(doc_id="doc-a", content="alpha content")
    hit2 = _hit(doc_id="doc-b", content="beta content")
    payload = build_judge_input(case, "answer", [hit1, hit2], nonce="deadbeef")
    sources = payload["sources"]
    assert isinstance(sources, list)
    assert len(sources) == 2
    for source in sources:
        assert "deadbeef" in source["content"]
    assert "alpha content" in sources[0]["content"]
    assert "beta content" in sources[1]["content"]


def test_build_judge_input_uses_the_same_nonce_everywhere_in_one_call() -> None:
    case = _judged_case(expected=(_fact("f1"),))
    hit = _hit(content="alpha content")
    payload = build_judge_input(case, "answer text", [hit], nonce="samenonce")
    answer = payload["answer"]
    sources = payload["sources"]
    assert isinstance(answer, str)
    assert isinstance(sources, list)
    source_content = sources[0]["content"]
    # BEGIN and END each carry the nonce once -- two occurrences per fence.
    assert answer.count("samenonce") == 2
    assert source_content.count("samenonce") == 2


def test_build_judge_input_uses_a_different_nonce_on_the_next_call() -> None:
    case = _judged_case(expected=(_fact("f1"),))
    payload_a = build_judge_input(case, "answer", [], nonce="nonce-one")
    payload_b = build_judge_input(case, "answer", [], nonce="nonce-two")
    answer_a = payload_a["answer"]
    answer_b = payload_b["answer"]
    assert isinstance(answer_a, str) and isinstance(answer_b, str)
    assert "nonce-one" in answer_a and "nonce-two" not in answer_a
    assert "nonce-two" in answer_b and "nonce-one" not in answer_b


def test_build_judge_input_carries_question_and_fact_id_text_pairs() -> None:
    case = _judged_case(
        case_id="c1",
        expected=(_fact("f1", "fact one text"),),
        forbidden=(_fact("f2", "fact two text"),),
    )
    payload = build_judge_input(case, "answer", [], nonce="n")
    assert payload["question"] == case.question
    assert payload["expected_facts"] == [{"id": "f1", "text": "fact one text"}]
    assert payload["forbidden_facts"] == [{"id": "f2", "text": "fact two text"}]


def test_build_judge_input_source_carries_doc_id_and_heading_path() -> None:
    case = _judged_case(expected=(_fact("f1"),))
    hit = _hit(doc_id="doc-a", heading_path="Doc A > Section 1")
    payload = build_judge_input(case, "answer", [hit], nonce="n")
    sources = payload["sources"]
    assert isinstance(sources, list)
    assert sources[0]["doc_id"] == "doc-a"
    assert sources[0]["heading_path"] == "Doc A > Section 1"


def test_build_judge_input_rejects_a_case_with_judged_none() -> None:
    case = _case()  # existing Task 1-4 helper: judged=None, judged_skip_reason set
    with pytest.raises(ValueError, match="judged=None"):
        build_judge_input(case, "answer", [], nonce="n")


# --- judge_prompt_template: in-memory, not a prompts/ file ---


def test_judge_prompt_template_shape() -> None:
    template = judge_prompt_template()
    assert isinstance(template, PromptTemplate)
    assert template.name == "eval_judge"
    assert template.version == eval_run.JUDGE_PROMPT_VERSION
    assert template.text == eval_run.JUDGE_PROMPT
    assert template.sha256 == sha256_hex(eval_run.JUDGE_PROMPT.encode("utf-8"))


def test_judge_prompt_template_is_not_a_file_under_prompts_dir() -> None:
    prompts_dir = _LAB_ROOT / "src" / "azgenai_lab" / "prompts"
    assert not (prompts_dir / "eval_judge.md").exists()


def test_judge_prompt_states_the_only_trusted_instruction_sources() -> None:
    prompt = eval_run.JUDGE_PROMPT.lower()
    assert "only instructions" in prompt


def test_judge_prompt_states_fenced_content_is_data_never_instructions() -> None:
    prompt = eval_run.JUDGE_PROMPT.lower()
    assert "never an instruction" in prompt


def test_judge_prompt_tells_the_model_not_to_follow_fenced_instructions() -> None:
    prompt = eval_run.JUDGE_PROMPT.lower()
    assert "do not execute" in prompt


def test_judge_prompt_tells_the_model_to_reply_with_only_the_json_object() -> None:
    prompt = eval_run.JUDGE_PROMPT.lower()
    assert "nothing else" in prompt


# --- parse_judge_response: strict parsing, four id-set invariants ---


def _one_expected_case() -> EvalCase:
    return _judged_case(expected=(_fact("e1"),), forbidden=(_fact("f1"),))


def _two_expected_case() -> EvalCase:
    return _judged_case(expected=(_fact("e1"), _fact("e2")), forbidden=(_fact("f1"),))


def _judge_response(
    covered: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    violated: tuple[str, ...] = (),
    unsupported: tuple[str, ...] = (),
    rationale: str = "ok",
) -> str:
    return json.dumps(
        {
            "covered_fact_ids": list(covered),
            "missing_fact_ids": list(missing),
            "violated_fact_ids": list(violated),
            "unsupported_claims": list(unsupported),
            "rationale": rationale,
        }
    )


def test_parse_judge_response_accepts_a_well_formed_response() -> None:
    case = _one_expected_case()
    output = parse_judge_response(_judge_response(covered=("e1",)), case)
    assert output.covered_fact_ids == ("e1",)
    assert output.missing_fact_ids == ()
    assert output.violated_fact_ids == ()
    assert output.unsupported_claims == ()
    assert output.rationale == "ok"


def test_parse_judge_response_rejects_malformed_json() -> None:
    case = _one_expected_case()
    with pytest.raises(JudgeParseError):
        parse_judge_response("{not valid json", case)


def test_parse_judge_response_rejects_json_with_prose_wrapped_around_it() -> None:
    case = _one_expected_case()
    raw = "Here is my answer: " + _judge_response(covered=("e1",))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_a_missing_key() -> None:
    case = _one_expected_case()
    payload = json.loads(_judge_response(covered=("e1",)))
    del payload["rationale"]
    with pytest.raises(JudgeParseError):
        parse_judge_response(json.dumps(payload), case)


def test_parse_judge_response_rejects_when_an_expected_fact_is_classified_nowhere() -> None:
    # Invariant 1: expected_ids <= covered | missing. e2 appears in neither
    # list -- disabling *only* this check must be the thing that turns this
    # test red (verified in Step 4's mutation pass).
    case = _two_expected_case()
    raw = _judge_response(covered=("e1",), missing=())
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_the_self_contradictory_all_empty_response() -> None:
    # The response r03 was written to stop: both lists empty for a case
    # that has one expected fact. Caught by the same invariant 1 as above.
    case = _one_expected_case()
    raw = _judge_response(covered=(), missing=())
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_covered_and_missing_overlap() -> None:
    # Invariant 2: covered & missing == set(). Both lists claim e1: the
    # union already equals expected (invariant 1 alone would accept this),
    # so only invariant 2 isolates it.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1",), missing=("e1",))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_a_violated_id_absent_from_forbidden_facts() -> None:
    # Invariant 3: violated <= forbidden_ids. "e1" is a real id (it is this
    # case's expected fact) but not a forbidden one, so invariant 4's
    # "known id" check alone would not catch it -- only invariant 3 does.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1",), violated=("e1",))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_an_extra_unknown_id_alongside_a_real_one() -> None:
    # Invariant 4: (covered | missing | violated) <= known_ids. "e1" alone
    # would satisfy invariant 1 (union == expected would need exactly
    # {"e1"} here since expected has only e1 -- so adding "not-a-real-id"
    # breaks invariant 4 without breaking invariant 1's "nothing expected
    # left out" direction).
    case = _one_expected_case()
    raw = _judge_response(covered=("e1", "not-a-real-id"), missing=())
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_a_fact_returned_as_free_text_instead_of_an_id() -> None:
    # Also invariant 4, but via missing_fact_ids and both expected ids
    # legitimately classified (unlike the extra-id test above, which uses
    # an id-shaped typo) -- a natural-language sentence dropped in among
    # real ids. Isolated from invariant 3 deliberately: this uses
    # missing_fact_ids, not violated_fact_ids, so invariant 3 (which only
    # constrains violated_fact_ids) can never be the one that fires here.
    case = _two_expected_case()
    raw = _judge_response(covered=("e1",), missing=("e2", "the answer mentions 30 days"))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


# --- derive_judge_verdict ---


def _judge_output(
    covered: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    violated: tuple[str, ...] = (),
    unsupported: tuple[str, ...] = (),
) -> JudgeOutput:
    return JudgeOutput(
        covered_fact_ids=covered,
        missing_fact_ids=missing,
        violated_fact_ids=violated,
        unsupported_claims=unsupported,
        rationale="r",
    )


def test_derive_judge_verdict_pass_when_all_three_are_empty() -> None:
    assert derive_judge_verdict(_judge_output(covered=("e1",))) == "pass"


def test_derive_judge_verdict_fail_when_missing_is_non_empty() -> None:
    assert derive_judge_verdict(_judge_output(missing=("e1",))) == "fail"


def test_derive_judge_verdict_fail_when_violated_is_non_empty() -> None:
    assert derive_judge_verdict(_judge_output(covered=("e1",), violated=("f1",))) == "fail"


def test_derive_judge_verdict_fail_when_unsupported_claims_is_non_empty() -> None:
    output = _judge_output(covered=("e1",), unsupported=("an ungrounded claim",))
    assert derive_judge_verdict(output) == "fail"


# --- canonical_json (Task 4, additional Task 5 coverage) ---


def test_canonical_json_is_stable_under_key_reordering_task5() -> None:
    assert eval_run.canonical_json({"z": 1, "a": {"y": 2, "x": 3}}) == eval_run.canonical_json(
        {"a": {"x": 3, "y": 2}, "z": 1}
    )
