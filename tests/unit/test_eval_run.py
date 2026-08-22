"""Tests for `tools/eval_run.py`'s deterministic assertion evaluators (Task 2),
`requires` propagation / exit codes (Task 3), pass A -- the corpus-seeded
`RagService`, its execution over the dataset, and `--calibrate` (Task 4) --
the judge contract's input fencing, canonical hashing, and invariant-checked
strict parsing (Task 5), and passes B/C: real second generation, N judge
repeats, stability, reporting, and the evidence sidecar (Task 6).

Table-driven: one test per rule in the design (`drafts/research/day-28-evaluation.md`
r04 §8/§7.1/§7.2/§7.3/§7.4/§7.5/§9) plus a few direct `attribute_source` tests
for its own branches. The Task 4 section below runs the real seeded pipeline
end to end (`USE_FAKE_LLM=true`, no Azure resources -- `tests/conftest.py`
pins the three fake-adapter flags for the whole suite). The Task 5 section is
pure -- no `RagService`, no provider call; it exercises `build_judge_input` /
`parse_judge_response` / `derive_judge_verdict` against hand-built
`SearchHit`s and raw response strings only. The Task 6 section never touches
the network either: it monkeypatches `eval_run.build_chat_service` with
in-memory stubs (task-6-brief.md constraint 5) -- always *after* building any
real `FakeChatService`-backed `RagService` the test needs, so the monkeypatch
only ever intercepts the calls Task 6's own code makes (pass B's generation,
pass C's judging), never pass A's.

`tools/` is not a package (no `tools/__init__.py`), so the module is loaded
by path, the same pattern `tests/unit/test_eval_cases.py` uses.
"""

import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ContentFilteredError, UpstreamTimeoutError
from azgenai_lab.models.principal import Principal
from azgenai_lab.models.rag import make_chunk_id, make_parent_id
from azgenai_lab.models.search import SearchHit
from azgenai_lab.prompts.loader import PromptTemplate, load_prompt
from azgenai_lab.services.azure_openai import ChatResult
from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR
from azgenai_lab.services.rag import RagAnswer, build_rag_service

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
JudgeRepeat = eval_run.JudgeRepeat
JudgedResult = eval_run.JudgedResult
derive_judged_result = eval_run.derive_judged_result
run_judge_repeats = eval_run.run_judge_repeats
run_judged_layer = eval_run.run_judged_layer
render_report = eval_run.render_report
evidence_document = eval_run.evidence_document

# The real lab worktree these tests run in -- test_eval_run.py lives at
# tests/unit/, two levels below the repo root.
_LAB_ROOT = Path(__file__).resolve().parents[2]


def _fresh_settings() -> Settings:
    """Settings isolated from the repo-root `.env` (same discipline
    `test_agent_toolset.py::_settings` uses): only `conftest.py`'s three
    pinned fake-adapter flags are guaranteed, everything else is still
    ambient without `_env_file=None`."""
    return Settings(_env_file=None)


def _judge_ready_settings() -> Settings:
    """`_fresh_settings()` plus a deployment name for `run_judged_layer`'s
    forced `use_fake_llm=False` branch: `build_seeded_rag_service` also
    calls `build_audit_attribution`, which independently rejects real mode
    without `azure_openai_deployment_name` -- a check Task 6's tests must
    satisfy even though the actual chat call is always stubbed via a
    monkeypatched `build_chat_service`, never real credentials.

    `use_fake_llm=False` because `--judge` under an ambient fake mode is
    refused at the CLI boundary: these tests are about what the judged layer
    does once it runs, so their settings must be ones it is allowed to run
    under. Nothing here reaches a real model regardless -- pass A forces
    `use_fake_llm=True` and both judged-layer chat services are stubbed."""
    return Settings(
        _env_file=None, use_fake_llm=False, azure_openai_deployment_name="stub-deployment"
    )


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


def test_parse_judge_response_rejects_a_forbidden_id_smuggled_into_covered() -> None:
    # Invariant 2: (covered | missing) <= expected_ids. f1 is a real,
    # case-known id -- it is this case's forbidden fact -- but it is not an
    # expected fact, so it must never appear in covered_fact_ids. This is
    # the exact regression the fix-round-1 review demonstrated against the
    # earlier (too-broad) version of this check: with expected={e1},
    # forbidden={f1}, covered=("e1","f1") used to be *accepted*, because
    # f1 is a "known" id in the expected-or-forbidden sense the old
    # invariant 4 tested. It is not a known id in the narrower
    # expected-only sense invariant 2 tests, which is what design §7.3
    # actually requires of covered_fact_ids/missing_fact_ids.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1", "f1"), missing=())
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_a_forbidden_id_smuggled_into_missing() -> None:
    # Same regression, via missing_fact_ids instead of covered_fact_ids --
    # and this shape is the more dangerous one: f1 in missing_fact_ids used
    # to be accepted by the old invariant 4 *and* then flip
    # derive_judge_verdict to a spurious "fail" on a response that in fact
    # covered its one and only expected fact (e1). Confirmed below in
    # test_derive_judge_verdict_never_reached_for_a_forbidden_id_in_missing.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1",), missing=("f1",))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_covered_and_missing_overlap() -> None:
    # Invariant 3: covered & missing == set(). Both lists claim e1: the
    # union already equals expected (invariants 1+2 alone would accept
    # this), so only invariant 3 isolates it.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1",), missing=("e1",))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_a_violated_id_absent_from_forbidden_facts() -> None:
    # Invariant 4: violated <= forbidden_ids. "e1" is a real, case-known id
    # (it is this case's expected fact) but not a forbidden one, so this is
    # only caught because invariant 4 constrains violated_fact_ids
    # specifically to forbidden_facts, not to "any known id."
    case = _one_expected_case()
    raw = _judge_response(covered=("e1",), violated=("e1",))
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_an_extra_unknown_id_alongside_a_real_one() -> None:
    # Invariant 2 again, but with a wholly fictional id (not a case-known
    # forbidden id) -- the weaker failure mode the original invariant 4
    # already caught, kept here so both shapes ("looks like a real id from
    # elsewhere in the case" and "looks like nothing at all") stay covered.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1", "not-a-real-id"), missing=())
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, case)


def test_parse_judge_response_rejects_a_fact_returned_as_free_text_instead_of_an_id() -> None:
    # Also invariant 2, via missing_fact_ids and both expected ids
    # legitimately classified (unlike the extra-id test above, which uses
    # an id-shaped typo) -- a natural-language sentence dropped in among
    # real ids. Isolated from invariant 4 deliberately: this uses
    # missing_fact_ids, not violated_fact_ids, so invariant 4 (which only
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


def test_derive_judge_verdict_never_reached_for_a_forbidden_id_in_missing() -> None:
    # End-to-end companion to
    # test_parse_judge_response_rejects_a_forbidden_id_smuggled_into_missing:
    # a response that covered its one expected fact (e1) but also, wrongly,
    # listed a forbidden id (f1) in missing_fact_ids raises at the parse
    # step -- there is no JudgeOutput for derive_judge_verdict to see, so it
    # can never compute the spurious "fail" the pre-fix version of
    # parse_judge_response would have let through.
    case = _one_expected_case()
    raw = _judge_response(covered=("e1",), missing=("f1",))
    with pytest.raises(JudgeParseError):
        derive_judge_verdict(parse_judge_response(raw, case))


# --- Task 6: passes B/C, stability reporting, evidence sidecar ---
#
# No test in this section touches the network. `_StaticChatService` and
# `_ScriptedChatService` stand in for build_chat_service's real branch;
# `_CannedAnswerService` stands in for a RagService's `.answer()` where a
# test needs to control pass A's RagAnswer directly rather than depend on
# the real seeded retriever's behavior for a particular question.


class _StaticChatService:
    """Every `.complete()` call returns the same `ChatResult` (or raises the
    same exception) -- for judge repeats that must all see an identical,
    well-formed response, and for pass B generation stubs."""

    def __init__(self, outcome: ChatResult | Exception) -> None:
        self._outcome = outcome
        self.calls: list[Sequence[object]] = []

    async def complete(self, items: Sequence[object]) -> ChatResult:
        self.calls.append(items)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def aclose(self) -> None:
        return None


class _ScriptedChatService:
    """One scripted `ChatResult`/exception per call, in order -- for
    sequences where a specific attempt must behave differently from the
    others (an error mid-sequence)."""

    def __init__(self, script: Sequence["ChatResult | Exception"]) -> None:
        self._script = list(script)
        self.calls: list[Sequence[object]] = []

    async def complete(self, items: Sequence[object]) -> ChatResult:
        self.calls.append(items)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        return None


class _CannedAnswerService:
    """A `.answer()`-only double standing in for the `RagService` `run_
    judged_layer` takes as `fake_service`: returns a fixed `RagAnswer`
    regardless of question/principal. Drives pass A's branches (no_answer, a
    specific hit set to compare against pass B) directly, without depending
    on the real seeded retriever's behavior for a particular question."""

    def __init__(self, answer: "RagAnswer") -> None:
        self._answer = answer
        self.calls = 0

    async def answer(self, question: str, principal: Principal) -> "RagAnswer":
        self.calls += 1
        return self._answer

    async def aclose(self) -> None:
        # Lets this double also stand in for `real_service` (pass B) via a
        # monkeypatched `build_seeded_rag_service` -- `run_judged_layer`
        # calls `await real_service.aclose()` unconditionally in its
        # `finally`.
        return None


def _stub_build_chat_service_factory(generation: object, judge: object):  # noqa: ANN201
    """A `build_chat_service`-shaped callable that hands back `generation`
    for the `rag_answer` prompt and `judge` for the `eval_judge` prompt --
    distinguished by `prompt.name`, the same way `run_judged_layer` itself
    selects between pass B's and pass C's prompts."""

    def factory(settings: object, *, prompt: PromptTemplate) -> object:
        return judge if prompt.name == "eval_judge" else generation

    return factory


def _repeat(
    attempt: int,
    outcome: str = "pass",
    answer_sha256: str = "a" * 64,
    sources_sha256: str = "s" * 64,
    judge_input_sha256: str = "j" * 64,
    raw_response: str = "{}",
) -> "JudgeRepeat":
    return JudgeRepeat(
        attempt=attempt,
        outcome=outcome,  # type: ignore[arg-type]
        answer_sha256=answer_sha256,
        sources_sha256=sources_sha256,
        judge_input_sha256=judge_input_sha256,
        raw_response=raw_response,
    )


def _judged_eval_case(
    case_id: str,
    question: str,
    tenant: str = "acme",
    expected: tuple["Fact", ...] = (),
    forbidden: tuple["Fact", ...] = (),
) -> EvalCase:
    return EvalCase(
        id=case_id,
        question=question,
        tenant=tenant,
        user="eval-agent",
        groups=(),
        protects="test coverage",
        requires=(),
        deterministic=_det(),
        judged=JudgedSpec(expected_facts=expected, forbidden_facts=forbidden, rubric=None),
        judged_skip_reason=None,
    )


# --- derive_judged_result: pure, no I/O ---


def test_derive_judged_result_raises_on_empty_repeats() -> None:
    with pytest.raises(ValueError, match="at least one repeat"):
        derive_judged_result("case-1", ())


def test_derive_judged_result_all_pass_is_judged_pass() -> None:
    repeats = (_repeat(1, "pass"), _repeat(2, "pass"), _repeat(3, "pass"))
    result = derive_judged_result("case-1", repeats)
    assert result.state == "JUDGED"
    assert result.verdict == "pass"
    assert result.reason is None
    assert result.repeats == repeats


def test_derive_judged_result_all_fail_is_judged_fail() -> None:
    repeats = (_repeat(1, "fail"), _repeat(2, "fail"))
    result = derive_judged_result("case-1", repeats)
    assert result.state == "JUDGED"
    assert result.verdict == "fail"


def test_derive_judged_result_verdict_is_repeat_one_not_a_majority_vote() -> None:
    # Four of five repeats pass; repeat 1 fails. A majority-vote aggregation
    # would report "pass" here -- design's rule (derive_judged_result's own
    # docstring) is "repeat 1's outcome, never an aggregate", so this must
    # report "fail".
    repeats = (
        _repeat(1, "fail"),
        _repeat(2, "pass"),
        _repeat(3, "pass"),
        _repeat(4, "pass"),
        _repeat(5, "pass"),
    )
    result = derive_judged_result("case-1", repeats)
    assert result.state == "JUDGED"
    assert result.verdict == "fail"


def test_derive_judged_result_one_error_makes_the_case_inconclusive() -> None:
    repeats = (_repeat(1, "pass"), _repeat(2, "ERROR(upstream)"), _repeat(3, "pass"))
    result = derive_judged_result("case-1", repeats)
    assert result.state == "INCONCLUSIVE"
    assert result.verdict is None
    assert "ERROR(upstream)" in (result.reason or "")
    # The sequence is preserved even though the case is inconclusive --
    # render_report's stability line reads it regardless of state.
    assert result.repeats == repeats


def test_derive_judged_result_never_produces_a_verdict_alongside_inconclusive() -> None:
    repeats = (_repeat(1, "ERROR(parse)"),)
    result = derive_judged_result("case-1", repeats)
    assert result.verdict is None


def test_gate_exit_code_structurally_cannot_see_a_judged_layer_error() -> None:
    # Companion to gate_exit_code's own signature test above (Task 3): a
    # judged-layer INCONCLUSIVE can never even be represented in the
    # deterministic results mapping gate_exit_code takes, so it structurally
    # cannot flip the exit code (this task's ambiguity 1).
    det_results = {"case-1": _result("case-1", Verdict.PASS)}
    assert gate_exit_code(det_results) == ExitCode.OK


def test_derive_judged_result_answer_sha256_mismatch_names_the_odd_attempt() -> None:
    repeats = (
        _repeat(1, "pass", answer_sha256="a" * 64),
        _repeat(2, "pass", answer_sha256="a" * 64),
        _repeat(3, "pass", answer_sha256="b" * 64),  # the odd one out
    )
    result = derive_judged_result("case-1", repeats)
    assert result.state == "INCONCLUSIVE"
    assert result.verdict is None
    assert "3" in (result.reason or "")
    assert result.repeats == repeats


def test_derive_judged_result_sources_sha256_mismatch_is_also_inconclusive() -> None:
    # Isolated from the answer_sha256 branch above: only sources_sha256
    # differs here, proving the mismatch check's `or` has two
    # independently-necessary operands.
    repeats = (
        _repeat(1, "pass", sources_sha256="s" * 64),
        _repeat(2, "pass", sources_sha256="t" * 64),
    )
    result = derive_judged_result("case-1", repeats)
    assert result.state == "INCONCLUSIVE"
    assert "2" in (result.reason or "")


def test_derive_judged_result_mismatch_check_runs_before_the_error_check() -> None:
    # A repeat can be both hash-mismatched *and* errored; the mismatch
    # reason must win (design §7.3: identity is checked first), not be
    # silently shadowed by the error-count branch.
    repeats = (
        _repeat(1, "pass", answer_sha256="a" * 64),
        _repeat(2, "ERROR(upstream)", answer_sha256="b" * 64),
    )
    result = derive_judged_result("case-1", repeats)
    assert result.state == "INCONCLUSIVE"
    assert "mismatch" in (result.reason or "")


# --- _judge_once / run_judge_repeats: real judge-call plumbing, stubbed ---


def _pass_response(covered: tuple[str, ...] = ("e1",)) -> ChatResult:
    return ChatResult(message=_judge_response(covered=covered), model_version="stub")


async def test_judge_once_well_formed_response_computes_correct_hashes_and_verdict() -> None:
    case = _one_expected_case()
    hit = _hit(content="the source text")
    judge_chat = _StaticChatService(_pass_response(covered=("e1",)))

    repeat = await eval_run._judge_once(case, "the answer", [hit], judge_chat, lambda: "nonce", 1)

    assert repeat.attempt == 1
    assert repeat.outcome == "pass"
    assert repeat.answer_sha256 == answer_sha256("the answer")
    assert repeat.sources_sha256 == sources_sha256([hit])
    expected_input = build_judge_input(case, "the answer", [hit], "nonce")
    assert repeat.judge_input_sha256 == sha256_hex(eval_run.canonical_json(expected_input))
    assert repeat.raw_response == _judge_response(covered=("e1",))
    assert len(judge_chat.calls) == 1


async def test_judge_once_content_filtered_is_error_filtered() -> None:
    case = _one_expected_case()
    judge_chat = _StaticChatService(ContentFilteredError("blocked"))

    repeat = await eval_run._judge_once(case, "answer", [], judge_chat, lambda: "n", 1)

    assert repeat.outcome == "ERROR(filtered)"


async def test_judge_once_upstream_timeout_is_error_upstream() -> None:
    case = _one_expected_case()
    judge_chat = _StaticChatService(UpstreamTimeoutError("timed out"))

    repeat = await eval_run._judge_once(case, "answer", [], judge_chat, lambda: "n", 1)

    assert repeat.outcome == "ERROR(upstream)"


async def test_judge_once_malformed_response_is_error_parse_with_the_raw_text_preserved() -> None:
    case = _one_expected_case()
    judge_chat = _StaticChatService(ChatResult(message="not json at all", model_version="stub"))

    repeat = await eval_run._judge_once(case, "answer", [], judge_chat, lambda: "n", 1)

    assert repeat.outcome == "ERROR(parse)"
    assert repeat.raw_response == "not json at all"


async def test_run_judge_repeats_returns_exactly_repeats_entries_even_with_an_error_midway() -> (
    None
):
    case = _one_expected_case()
    script: list[ChatResult | Exception] = [
        _pass_response(covered=("e1",)),
        UpstreamTimeoutError("boom"),
        _pass_response(covered=("e1",)),
    ]
    judge_chat = _ScriptedChatService(script)

    repeats = await run_judge_repeats(
        case, "answer", [], judge_chat, repeats=3, nonce_factory=lambda: "n"
    )

    assert len(repeats) == 3
    assert [r.outcome for r in repeats] == ["pass", "ERROR(upstream)", "pass"]
    assert [r.attempt for r in repeats] == [1, 2, 3]


async def test_run_judge_repeats_draws_a_fresh_nonce_per_attempt() -> None:
    case = _one_expected_case()
    nonces = iter(["n1", "n2", "n3"])
    judge_chat = _StaticChatService(_pass_response(covered=("e1",)))

    repeats = await run_judge_repeats(
        case, "answer", [], judge_chat, repeats=3, nonce_factory=lambda: next(nonces)
    )

    # Same answer/sources every attempt, but a different nonce means a
    # different judge_input -- so judge_input_sha256 must differ per attempt.
    assert len({r.judge_input_sha256 for r in repeats}) == 3
    # ...while answer_sha256/sources_sha256 (computed from answer/hits, not
    # the nonce) stay identical across attempts.
    assert len({r.answer_sha256 for r in repeats}) == 1


# --- run_judged_layer: orchestration (design §7.1/§7.4), stubbed only at
# the build_chat_service boundary ---


async def test_run_judged_layer_skipped_case_makes_no_generation_or_judge_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()  # existing Task 1-4 helper: judged=None, judged_skip_reason set
    fake_service = _CannedAnswerService(
        RagAnswer(status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None)
    )
    generation = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    judge = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    results = await run_judged_layer([case], fake_service, _judge_ready_settings(), repeats=5)  # type: ignore[arg-type]

    result = results[case.id]
    assert result.state == "SKIPPED"
    assert result.reason == case.judged_skip_reason
    assert result.verdict is None
    assert result.repeats == ()
    assert fake_service.calls == 0
    assert generation.calls == []
    assert judge.calls == []


async def test_run_judged_layer_pass_a_no_answer_is_inconclusive_with_zero_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _judged_eval_case("c1", "does not matter", expected=(_fact("f1"),))
    fake_service = _CannedAnswerService(
        RagAnswer(status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None)
    )
    generation = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    judge = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    results = await run_judged_layer([case], fake_service, _judge_ready_settings(), repeats=5)  # type: ignore[arg-type]

    result = results[case.id]
    assert result.state == "INCONCLUSIVE"
    assert result.reason == "no_answer_at_runtime"
    assert result.verdict is None
    assert result.repeats == ()
    # Pass A answered "no_answer" -- generation and judging must never run.
    assert generation.calls == []
    assert judge.calls == []


async def test_run_judged_layer_pass_a_pass_b_sources_disagreement_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case_by_id("acme-refund-window-standard")
    fabricated_hit = _hit(tenant="acme", doc_id="doc-a", content="not the real corpus content")
    fake_service = _CannedAnswerService(
        RagAnswer(
            status="answered",
            answer="[fake] pass A answer",
            hits=(fabricated_hit,),
            usage=None,
            incomplete_reason=None,
        )
    )
    generation = _StaticChatService(
        ChatResult(
            message="Standard purchases may be returned within 30 days.", model_version="stub"
        )
    )
    judge = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    results = await run_judged_layer([case], fake_service, _judge_ready_settings(), repeats=5)  # type: ignore[arg-type]

    result = results[case.id]
    assert result.state == "INCONCLUSIVE"
    assert "sources" in (result.reason or "")
    assert result.verdict is None
    assert result.repeats == ()
    # Pass B's generation *did* run (needed to know its sources); judging did not.
    assert len(generation.calls) == 1
    assert judge.calls == []


async def test_run_judged_layer_pass_b_upstream_error_is_inconclusive_with_zero_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case_by_id("acme-refund-window-standard")
    fake_service = _CannedAnswerService(
        RagAnswer(
            status="answered",
            answer="[fake] pass A answer",
            hits=(_hit(tenant="acme", doc_id="returns-policy"),),
            usage=None,
            incomplete_reason=None,
        )
    )
    generation = _StaticChatService(UpstreamTimeoutError("pass B generation timed out"))
    judge = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    results = await run_judged_layer([case], fake_service, _judge_ready_settings(), repeats=5)  # type: ignore[arg-type]

    result = results[case.id]
    assert result.state == "INCONCLUSIVE"
    assert "pass_b_generation_error" in (result.reason or "")
    assert result.repeats == ()
    assert judge.calls == []


async def test_run_judged_layer_upstream_error_text_never_reaches_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The report's contract is that it never states a rate. An upstream error
    # message is text this runner does not control, and a real Azure 429 reads
    # "Requests to the ... exceeded call rate limit ... Please retry after 86
    # seconds." Interpolating it into the INCONCLUSIVE reason would print the
    # word "rate" -- and a percentage, given a message that carries one -- in
    # a line the no-rates guard is supposed to own. Only the exception class
    # name is kept (Day 28 review, Task 6).
    case = _case_by_id("acme-refund-window-standard")
    fake_service = _CannedAnswerService(
        RagAnswer(
            status="answered",
            answer="[fake] pass A answer",
            hits=(_hit(tenant="acme", doc_id="returns-policy"),),
            usage=None,
            incomplete_reason=None,
        )
    )
    generation = _StaticChatService(
        UpstreamTimeoutError(
            "Requests to the ChatCompletions_Create Operation under Azure OpenAI API "
            "have exceeded call rate limit of 100%. Please retry after 86 seconds."
        )
    )
    judge = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    results = await run_judged_layer([case], fake_service, _judge_ready_settings(), repeats=5)  # type: ignore[arg-type]

    result = results[case.id]
    assert result.state == "INCONCLUSIVE"
    # The class name survives; the upstream sentence does not.
    assert result.reason == "pass_b_generation_error: UpstreamTimeoutError"
    report = render_report({case.id: _result(case.id, Verdict.PASS)}, results)
    assert not re.search(r"%", report)
    assert not re.search(r"rate", report, re.IGNORECASE)


async def test_run_judged_layer_pass_b_structural_no_answer_is_inconclusive_with_zero_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pass A answered (so its own no_answer check above did not fire), but
    # pass B's real generation call -- built from `build_seeded_rag_service`,
    # which `run_judged_layer` does not take as an injectable parameter --
    # structurally no-answers instead of raising. This is a second, distinct
    # no_answer branch from pass A's (mutation-verified separately: with the
    # dataset-driven pass-A no_answer check alone disabled, this scenario's
    # own test above still stubs pass B to answer, so it never exercises
    # this branch -- confirmed empty-coverage before this test existed).
    case = _case_by_id("acme-refund-window-standard")
    fake_service = _CannedAnswerService(
        RagAnswer(
            status="answered",
            answer="[fake] pass A answer",
            hits=(_hit(tenant="acme", doc_id="returns-policy"),),
            usage=None,
            incomplete_reason=None,
        )
    )
    pass_b_service = _CannedAnswerService(
        RagAnswer(status="no_answer", answer=None, hits=(), usage=None, incomplete_reason=None)
    )
    monkeypatch.setattr(
        eval_run,
        "build_seeded_rag_service",
        lambda settings, *, use_fake_llm: pass_b_service,
    )
    generation = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    judge = _StaticChatService(ChatResult(message="unused", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    results = await run_judged_layer([case], fake_service, _judge_ready_settings(), repeats=5)  # type: ignore[arg-type]

    result = results[case.id]
    assert result.state == "INCONCLUSIVE"
    assert result.reason == "no_answer_at_runtime"
    assert result.verdict is None
    assert result.repeats == ()
    # Pass B's generation ran (it's the one that returned no_answer);
    # judging never does.
    assert pass_b_service.calls == 1
    assert judge.calls == []


async def test_run_judged_layer_builds_both_passes_from_forced_real_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard for a real bug: the generation service applied the
    # `use_fake_llm=False` override (inside `build_seeded_rag_service`) but the
    # judge was built from the caller's bare settings, so an ambient fake mode
    # produced real answers graded by a fake echo -- every repeat unparseable,
    # every case INCONCLUSIVE, and the run still exited 0. Asserting on both
    # recorded settings, not just the judge's, is what makes this a guard
    # against the two passes diverging rather than against one line's value.
    settings = Settings(
        _env_file=None, use_fake_llm=True, azure_openai_deployment_name="stub-deployment"
    )
    case = _case_by_id("acme-refund-window-standard")
    fake_service = eval_run.build_seeded_rag_service(settings, use_fake_llm=True)

    generation = _StaticChatService(
        ChatResult(
            message="A standard purchase may be returned within 30 days of delivery.",
            model_version="stub",
        )
    )
    judge = _StaticChatService(_pass_response(covered=("fact_standard_window_30_days",)))
    seen: dict[str, bool] = {}

    def _recording(settings_arg: Settings, *, prompt: PromptTemplate) -> object:
        seen[prompt.name] = settings_arg.use_fake_llm
        return judge if prompt.name == "eval_judge" else generation

    monkeypatch.setattr(eval_run, "build_chat_service", _recording)

    try:
        await run_judged_layer([case], fake_service, settings, repeats=1)
    finally:
        await fake_service.aclose()

    assert seen == {"rag_answer": False, "eval_judge": False}


async def test_run_judged_layer_matching_sources_runs_judging_and_reports_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end wiring: a real seeded fake_service (built before the
    # monkeypatch below, so its own real FakeChatService is unaffected) and
    # a real dataset case -- only the two build_chat_service calls Task 6's
    # own code makes (pass B, pass C) are stubbed.
    settings = _judge_ready_settings()
    case = _case_by_id("acme-refund-window-standard")
    fake_service = eval_run.build_seeded_rag_service(settings, use_fake_llm=True)

    generation = _StaticChatService(
        ChatResult(
            message="A standard purchase may be returned within 30 days of delivery.",
            model_version="stub",
        )
    )
    judge = _StaticChatService(_pass_response(covered=("fact_standard_window_30_days",)))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    try:
        results = await run_judged_layer([case], fake_service, settings, repeats=3)
    finally:
        await fake_service.aclose()

    result = results[case.id]
    assert result.state == "JUDGED"
    assert result.verdict == "pass"
    assert result.reason is None
    assert len(result.repeats) == 3
    assert [r.outcome for r in result.repeats] == ["pass", "pass", "pass"]
    # Real generation ran exactly once (pass B); judging ran once per repeat.
    assert len(generation.calls) == 1
    assert len(judge.calls) == 3


# --- render_report: three labelled lines per case, sequence not a rate ---


def test_render_report_three_labelled_lines_per_case_for_every_state() -> None:
    det = {
        "skipped-case": _result("skipped-case", Verdict.PASS),
        "inconclusive-case": _result("inconclusive-case", Verdict.PASS),
        "judged-case": _result("judged-case", Verdict.PASS),
    }
    judged = {
        "skipped-case": JudgedResult("skipped-case", None, "SKIPPED", "no judged block", ()),
        "inconclusive-case": JudgedResult(
            "inconclusive-case", None, "INCONCLUSIVE", "no_answer_at_runtime", ()
        ),
        "judged-case": JudgedResult(
            "judged-case", "pass", "JUDGED", None, (_repeat(1, "pass"), _repeat(2, "pass"))
        ),
    }

    report = render_report(det, judged)

    labelled = [
        line.strip()
        for line in report.splitlines()
        if line.strip().startswith(("deterministic:", "judged:", "stability:"))
    ]
    assert len(labelled) == 9  # three labels x three cases
    assert sum(line.startswith("deterministic:") for line in labelled) == 3
    assert sum(line.startswith("judged:") for line in labelled) == 3
    assert sum(line.startswith("stability:") for line in labelled) == 3
    # Each block must be headed by its own case id, in `det` order. Nothing
    # asserted this before (Day 28 review, Task 6 re-review round 4): the
    # header is the only thing telling a human which case a block describes,
    # and it is the surface people read without opening the JSON sidecar.
    headers = [line for line in report.splitlines() if line and not line.startswith(" ")]
    assert headers == ["skipped-case", "inconclusive-case", "judged-case"]


def test_render_report_stability_not_measured_when_repeats_empty() -> None:
    det = {"c1": _result("c1", Verdict.PASS)}
    judged = {"c1": JudgedResult("c1", None, "SKIPPED", "no judged block", ())}

    report = render_report(det, judged)

    assert "stability:     NOT MEASURED" in report


def test_render_report_prints_the_actual_sequence_and_never_a_rate() -> None:
    det = {"c1": _result("c1", Verdict.PASS)}
    repeats = (
        _repeat(1, "pass"),
        _repeat(2, "pass"),
        _repeat(3, "fail"),
        _repeat(4, "pass"),
        _repeat(5, "pass"),
    )
    judged = {"c1": JudgedResult("c1", None, "INCONCLUSIVE", "example", repeats)}

    report = render_report(det, judged)

    assert "pass,pass,fail,pass,pass" in report
    assert not re.search(r"%", report)
    assert not re.search(r"rate", report, re.IGNORECASE)


def test_render_report_skipped_case_shows_the_datasets_skip_reason() -> None:
    det = {"c1": _result("c1", Verdict.PASS)}
    judged = {"c1": JudgedResult("c1", None, "SKIPPED", "no corpus coverage yet", ())}

    report = render_report(det, judged)

    assert "SKIPPED(no corpus coverage yet)" in report


def test_render_report_inconclusive_case_shows_its_reason() -> None:
    det = {"c1": _result("c1", Verdict.PASS)}
    judged = {"c1": JudgedResult("c1", None, "INCONCLUSIVE", "no_answer_at_runtime", ())}

    report = render_report(det, judged)

    assert "INCONCLUSIVE(no_answer_at_runtime)" in report


def test_render_report_judged_case_shows_its_verdict() -> None:
    det = {"c1": _result("c1", Verdict.PASS)}
    judged = {"c1": JudgedResult("c1", "pass", "JUDGED", None, (_repeat(1, "pass"),))}

    report = render_report(det, judged)

    assert "JUDGED(pass)" in report


def test_render_report_case_missing_from_judged_mapping_is_not_run_not_blank() -> None:
    # A case whose judged layer was never invoked at all (e.g. --judge was
    # not passed) must show a named state, not a blank line pretending to
    # be a measurement.
    det = {"c1": _result("c1", Verdict.PASS)}

    report = render_report(det, {})

    assert "judged:        NOT RUN" in report
    assert "stability:     NOT MEASURED" in report


def test_render_report_deterministic_failures_are_included_on_their_own_line() -> None:
    det = {"c1": _result("c1", Verdict.FAIL, ("must_cite: missing ['doc-a']",))}

    report = render_report(det, {})

    assert "FAIL (must_cite: missing ['doc-a'])" in report


def test_render_report_joins_multiple_failures_on_one_line() -> None:
    # A case can violate several assertions at once -- `evaluate_deterministic`
    # collects them all rather than returning early -- so the join format is
    # real output, not a hypothetical. Only the single-failure shape was
    # covered before (Day 28 review, Task 6 re-review round 4).
    det = {
        "c1": _result(
            "c1",
            Verdict.FAIL,
            ("must_cite: missing ['doc-a']", "must_not_cite: present ['doc-b']"),
        )
    }

    report = render_report(det, {})

    assert (
        "FAIL (must_cite: missing ['doc-a']; must_not_cite: present ['doc-b'])" in report
    )


# --- evidence_document: Task 10's live-run sidecar shape ---


async def test_evidence_document_shape_for_the_real_lab_root() -> None:
    settings = _fresh_settings()
    case = _case_by_id("acme-refund-window-standard")
    det = {case.id: _result(case.id, Verdict.PASS)}
    judged = {
        case.id: JudgedResult(
            case.id, "pass", "JUDGED", None, (_repeat(1, "pass", raw_response="raw text one"),)
        )
    }

    document = evidence_document(
        run_id="test-run-1",
        started_at="2026-08-22T00:00:00Z",
        completed_at="2026-08-22T00:05:00Z",
        lab_root=_LAB_ROOT,
        settings=settings,
        cases=[case],
        det=det,
        judged=judged,
    )

    assert document["kind"] == "day28-judged-evaluation-run"
    assert document["run_id"] == "test-run-1"
    assert document["started_at"] == "2026-08-22T00:00:00Z"
    assert document["completed_at"] == "2026-08-22T00:05:00Z"
    assert isinstance(document["lab_commit"], str) and document["lab_commit"]
    assert document["dataset_sha256"] == sha256_hex(eval_run._DATASET_PATH.read_bytes())

    corpus_manifest = document["corpus_manifest"]
    assert isinstance(corpus_manifest, dict)
    # Every path's hash pinned against the real file, not just the key's
    # presence: the manifest exists so a later reader can prove the corpus
    # has not moved under the run, and a manifest of constant values would
    # satisfy a presence-only check while proving nothing.
    corpus_dir = Path(settings.sample_docs_dir or SAMPLE_DOCS_DIR)
    assert corpus_manifest == {
        str(path.relative_to(corpus_dir)): sha256_hex(path.read_bytes())
        for path in sorted(corpus_dir.glob("*/*.md"))
    }
    assert "acme/returns-policy.md" in corpus_manifest

    rag_prompt = document["rag_prompt"]
    assert isinstance(rag_prompt, dict)
    assert rag_prompt["name"] == "rag_answer"
    # Pinned to the loaded template, the same way judge_prompt is below: a
    # version or sha256 that silently blanked would leave the evidence file
    # unable to say which prompt produced the answers it records.
    _loaded_rag_prompt = load_prompt("rag_answer")
    assert rag_prompt["version"] == _loaded_rag_prompt.version
    assert rag_prompt["sha256"] == _loaded_rag_prompt.sha256
    judge_prompt = document["judge_prompt"]
    assert isinstance(judge_prompt, dict)
    assert judge_prompt["version"] == eval_run.JUDGE_PROMPT_VERSION
    assert judge_prompt["sha256"] == sha256_hex(eval_run.JUDGE_PROMPT.encode("utf-8"))

    cases_doc = document["cases"]
    assert isinstance(cases_doc, list)
    assert len(cases_doc) == 1
    case_doc = cases_doc[0]
    assert case_doc["id"] == case.id
    assert case_doc["principal"] == {"tenant": "acme", "groups_count": 0, "group_sha256": []}
    assert case_doc["deterministic"] == {"verdict": "PASS", "failures": []}
    judged_doc = case_doc["judged"]
    assert judged_doc["state"] == "JUDGED"
    assert judged_doc["verdict"] == "pass"
    repeat_doc = judged_doc["repeats"][0]
    assert repeat_doc["raw_response"] == "raw text one"
    # `outcome` is the pass/fail/error value this whole layer exists to
    # produce, and `attempt` is what orders the sequence the stability line
    # prints. Neither was asserted at all before (Day 28 review, Task 6
    # re-review round 3).
    assert repeat_doc["attempt"] == 1
    assert repeat_doc["outcome"] == "pass"
    # Every hash the repeat carries must reach the document intact. Blanking
    # any one of them left the whole suite green until this assertion existed
    # (Day 28 review, Task 6 mutation backfill M25): the evidence file is what
    # a later reader replays from, and a hash that silently became "" would
    # make the run unreplayable while still looking well-formed.
    assert repeat_doc["answer_sha256"] == "a" * 64
    assert repeat_doc["sources_sha256"] == "s" * 64
    assert repeat_doc["judge_input_sha256"] == "j" * 64

    assert document["cases_sha256"] == sha256_hex(eval_run.canonical_json(cases_doc))


async def test_evidence_document_records_the_reason_an_inconclusive_case_has_no_verdict() -> None:
    # For an INCONCLUSIVE or SKIPPED case the reason is the ONLY text saying
    # why no verdict exists. Nothing asserted it until this test: the shape
    # test above covers a JUDGED case, whose reason is None (Day 28 review,
    # Task 6 re-review). An evidence file that lost it would still look
    # well-formed while being unreplayable -- the same failure M25 exposed
    # for the repeat hashes.
    settings = _fresh_settings()
    case = _case_by_id("acme-refund-window-standard")
    det = {case.id: _result(case.id, Verdict.PASS)}
    judged = {
        case.id: JudgedResult(
            case.id, None, "INCONCLUSIVE", "pass_a_pass_b_sources_sha256_mismatch", ()
        )
    }

    document = evidence_document(
        run_id="test-run-2",
        started_at="2026-08-22T00:00:00Z",
        completed_at="2026-08-22T00:05:00Z",
        lab_root=_LAB_ROOT,
        settings=settings,
        cases=[case],
        det=det,
        judged=judged,
    )

    cases_doc = document["cases"]
    assert isinstance(cases_doc, list)
    judged_doc = cases_doc[0]["judged"]
    assert judged_doc["state"] == "INCONCLUSIVE"
    assert judged_doc["verdict"] is None
    assert judged_doc["reason"] == "pass_a_pass_b_sources_sha256_mismatch"
    assert judged_doc["repeats"] == []


async def test_evidence_document_rejects_a_lab_root_that_is_not_a_git_worktree(
    tmp_path: Path,
) -> None:
    with pytest.raises(DatasetError, match="not a git worktree"):
        evidence_document(
            run_id="r",
            started_at="t0",
            completed_at="t1",
            lab_root=tmp_path,
            settings=_fresh_settings(),
            cases=[],
            det={},
            judged={},
        )


async def test_evidence_document_redacts_group_ids_to_count_and_sha256() -> None:
    case = EvalCase(
        id="c1",
        question="q",
        tenant="acme",
        user="u",
        groups=("secret-group-name",),
        protects="p",
        requires=(),
        deterministic=_det(),
        judged=None,
        judged_skip_reason="test",
    )

    document = evidence_document(
        run_id="r",
        started_at="t0",
        completed_at="t1",
        lab_root=_LAB_ROOT,
        settings=_fresh_settings(),
        cases=[case],
        det={},
        judged={},
    )

    cases_doc = document["cases"]
    assert isinstance(cases_doc, list)
    principal = cases_doc[0]["principal"]
    assert principal["groups_count"] == 1
    assert principal["group_sha256"] == [sha256_hex(b"secret-group-name")]
    dumped = json.dumps(document)
    assert "secret-group-name" not in dumped


async def test_evidence_document_raw_response_only_ever_appears_inside_a_repeats_entry() -> None:
    case = _case_by_id("acme-refund-window-standard")
    marker = "UNIQUE_RAW_RESPONSE_MARKER_12345"
    repeat = _repeat(1, "pass", raw_response=marker)
    judged = {case.id: JudgedResult(case.id, "pass", "JUDGED", None, (repeat,))}

    document = evidence_document(
        run_id="r",
        started_at="t0",
        completed_at="t1",
        lab_root=_LAB_ROOT,
        settings=_fresh_settings(),
        cases=[case],
        det={case.id: _result(case.id, Verdict.PASS)},
        judged=judged,
    )

    cases_doc = document["cases"]
    assert isinstance(cases_doc, list)
    case_doc = cases_doc[0]
    judged_doc = case_doc["judged"]
    assert judged_doc["repeats"][0]["raw_response"] == marker
    without_repeats = {k: v for k, v in judged_doc.items() if k != "repeats"}
    assert marker not in json.dumps(without_repeats)
    without_judged = {k: v for k, v in case_doc.items() if k != "judged"}
    assert marker not in json.dumps(without_judged)


def test_evidence_document_computes_cases_sha256_from_canonical_json_of_cases() -> None:
    case = _case_by_id("zero-hit-structural-no-answer")

    document = evidence_document(
        run_id="r",
        started_at="t0",
        completed_at="t1",
        lab_root=_LAB_ROOT,
        settings=_fresh_settings(),
        cases=[case],
        det={},
        judged={},
    )

    assert document["cases_sha256"] == sha256_hex(eval_run.canonical_json(document["cases"]))


# --- main(): --judge / --repeats CLI wiring ---


def test_main_judge_flag_wires_report_and_still_exits_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(eval_run, "get_settings", _judge_ready_settings)
    generation = _StaticChatService(
        ChatResult(message="a real-sounding answer", model_version="stub")
    )
    judge = _StaticChatService(ChatResult(message="not valid json", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    exit_code = eval_run.main(["--judge", "--repeats", "1"])

    assert exit_code == ExitCode.OK
    captured = capsys.readouterr()
    assert "deterministic:" in captured.out
    assert "judged:" in captured.out
    assert "stability:" in captured.out


def test_main_repeats_flag_is_the_value_run_judged_layer_actually_receives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `--repeats`'s default (5) is also what the shipped code would use if
    # the CLI value were silently dropped -- a run with a non-default value
    # is the only way to catch that. Stubs `run_judged_layer` itself (rather
    # than driving it end to end) so this pins exactly the CLI-to-call-site
    # wiring, independent of dataset composition or judge-call counting.
    monkeypatch.setattr(eval_run, "get_settings", _judge_ready_settings)
    captured_kwargs: dict[str, object] = {}

    async def _fake_run_judged_layer(
        cases: object, service: object, settings: object, *, repeats: int, **_: object
    ) -> dict[str, object]:
        captured_kwargs["repeats"] = repeats
        return {}

    monkeypatch.setattr(eval_run, "run_judged_layer", _fake_run_judged_layer)

    exit_code = eval_run.main(["--judge", "--repeats", "7"])

    assert exit_code == ExitCode.OK
    assert captured_kwargs["repeats"] == 7


def test_main_evidence_out_writes_a_canonical_replayable_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `evidence_document` was fully implemented and unit-tested but nothing
    # called it -- the runner printed a console report and left no replayable
    # record, which is exactly what the live run's evidence step needs
    # (Day 28 Task 7 review). This pins the wiring.
    monkeypatch.setattr(eval_run, "get_settings", _judge_ready_settings)
    generation = _StaticChatService(
        ChatResult(message="a real-sounding answer", model_version="stub")
    )
    judge = _StaticChatService(ChatResult(message="not valid json", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )
    out = tmp_path / "evidence.json"

    exit_code = eval_run.main(["--judge", "--repeats", "1", "--evidence-out", str(out)])

    assert exit_code == ExitCode.OK
    raw = out.read_bytes()
    # Canonical bytes, not pretty-printed: the sidecar is hashed and diffed.
    assert raw == eval_run.canonical_json(json.loads(raw))
    document = json.loads(raw)
    assert document["kind"] == "day28-judged-evaluation-run"
    assert document["run_id"].startswith("run-")
    assert document["started_at"].endswith("Z")
    assert document["completed_at"].endswith("Z")
    shipped = eval_run.load_cases(eval_run._DATASET_PATH, SAMPLE_DOCS_DIR)
    assert len(document["cases"]) == len(shipped)


def test_main_without_evidence_out_writes_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(eval_run, "get_settings", _judge_ready_settings)
    generation = _StaticChatService(
        ChatResult(message="a real-sounding answer", model_version="stub")
    )
    judge = _StaticChatService(ChatResult(message="not valid json", model_version="stub"))
    monkeypatch.setattr(
        eval_run, "build_chat_service", _stub_build_chat_service_factory(generation, judge)
    )

    exit_code = eval_run.main(["--judge", "--repeats", "1"])

    assert exit_code == ExitCode.OK
    assert list(tmp_path.iterdir()) == []


def test_run_id_is_unique_per_call() -> None:
    # The human verdict recorded in an evidence file is bound to run_id, so a
    # collision would attach one run's adjudication to another run's answers.
    ids = {eval_run._run_id() for _ in range(64)}
    assert len(ids) == 64


def test_main_judge_flag_with_fake_llm_exits_setup_failed_making_no_provider_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The incoherent combination: `--judge` forces real generation for pass B,
    # so under an ambient fake mode the run would spend provider calls on
    # answers and grade them with a fake echo. The refusal must land before
    # anything is built -- exit 2, and `build_chat_service` never reached.
    monkeypatch.setattr(eval_run, "get_settings", _fresh_settings)

    def _never(settings: Settings, *, prompt: PromptTemplate) -> object:
        raise AssertionError("no chat service may be built once --judge is refused")

    monkeypatch.setattr(eval_run, "build_chat_service", _never)

    exit_code = eval_run.main(["--judge"])

    assert exit_code == ExitCode.SETUP_FAILED
    assert exit_code != ExitCode.OK
    captured = capsys.readouterr()
    assert "SETUP FAILURE" in captured.err
    assert "USE_FAKE_LLM" in captured.err


def test_main_judge_flag_missing_credentials_exits_setup_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `_judge_ready_settings` (real ambient mode) so this reaches the
    # credentials check rather than stopping at the fake-mode refusal above:
    # both exit 2 with "SETUP FAILURE", so nothing in the assertions below
    # would notice the difference.
    monkeypatch.setattr(eval_run, "get_settings", _judge_ready_settings)
    real_build_chat_service = eval_run.build_chat_service

    def _guarded(settings: Settings, *, prompt: PromptTemplate) -> object:
        if not settings.use_fake_llm:
            raise ValueError(
                "USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_DEPLOYMENT_NAME"
            )
        return real_build_chat_service(settings, prompt=prompt)

    monkeypatch.setattr(eval_run, "build_chat_service", _guarded)

    exit_code = eval_run.main(["--judge"])

    assert exit_code == ExitCode.SETUP_FAILED
    assert exit_code != ExitCode.GATE_FAILED
    captured = capsys.readouterr()
    assert "SETUP FAILURE" in captured.err


def test_main_without_judge_flag_never_reaches_the_real_llm_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard for the default path: without --judge, run_judged_
    # layer (which forces use_fake_llm=False) must never run at all.
    real_build_chat_service = eval_run.build_chat_service

    def _guarded(settings: Settings, *, prompt: PromptTemplate) -> object:
        if not settings.use_fake_llm:
            raise AssertionError("the real (use_fake_llm=False) branch must not be reached")
        return real_build_chat_service(settings, prompt=prompt)

    monkeypatch.setattr(eval_run, "get_settings", _fresh_settings)
    monkeypatch.setattr(eval_run, "build_chat_service", _guarded)

    assert eval_run.main([]) == ExitCode.OK
