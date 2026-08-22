"""Validator tests for `tools/eval_run.py`'s dataset section (Task 1).

One test per rule in the design (`drafts/research/day-28-evaluation.md`
r04 §8) plus one positive test that the shipped `tools/eval_cases.json`
loads clean against the real sample corpus.

`tools/` is not a package (no `tools/__init__.py`), so the module is loaded
by path, the same pattern `tests/unit/test_prompt_shields_probe.py` uses.
"""

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "eval_run.py"
_SPEC = importlib.util.spec_from_file_location("eval_run", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
eval_run = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = eval_run
_SPEC.loader.exec_module(eval_run)

DatasetError = eval_run.DatasetError
load_cases = eval_run.load_cases

_SHIPPED_DATASET = Path(__file__).resolve().parents[2] / "tools" / "eval_cases.json"


# --- fixture corpus: two documents, two tenants, one group-gated document ---


def _write_doc(root: Path, tenant: str, doc_id: str, *, allowed_groups: Sequence[str] = ()) -> None:
    groups = "[" + ", ".join(allowed_groups) + "]"
    text = (
        "---\n"
        f"doc_id: {doc_id}\n"
        f"title: {doc_id}\n"
        "doc_type: policy\n"
        f"tenant_id: {tenant}\n"
        "effective_date: 2026-01-01\n"
        f"allowed_groups: {groups}\n"
        "---\n\n"
        f"# {doc_id}\n\nBody text for {doc_id}.\n"
    )
    directory = root / tenant
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{doc_id}.md").write_text(text, encoding="utf-8")


def _write_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    _write_doc(root, "acme", "doc-a")
    _write_doc(root, "globex", "doc-b", allowed_groups=["oncall"])
    return root


# --- minimal valid case builders, overridden per test ---


def _valid_deterministic(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "answered",
        "status_note": None,
        "must_cite": ["doc-a"],
        "citations_subset_of": ["doc-a"],
        "subset_note": None,
        "must_not_cite": [],
    }
    base.update(overrides)
    return base


def _valid_judged(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "expected_facts": [{"id": "fact_1", "text": "doc-a says something"}],
        "forbidden_facts": [],
        "rubric": None,
    }
    base.update(overrides)
    return base


def _valid_case(case_id: str = "case-1", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": case_id,
        "question": "What does doc-a say?",
        "principal": {"tenant": "acme", "user": "eval-agent", "groups": []},
        "protects": "test coverage",
        "requires": [],
        "deterministic": _valid_deterministic(),
        "judged": _valid_judged(),
        "judged_skip_reason": None,
    }
    base.update(overrides)
    return base


def _write_dataset(tmp_path: Path, cases: list[dict[str, object]]) -> Path:
    path = tmp_path / "eval_cases.json"
    path.write_text(json.dumps({"version": 1, "cases": cases}), encoding="utf-8")
    return path


# --- tests ---


def test_duplicate_id(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(tmp_path, [_valid_case("dup"), _valid_case("dup")])
    with pytest.raises(DatasetError, match="dup"):
        load_cases(dataset, corpus_dir)


def test_doc_id_absent_from_corpus(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [_valid_case("missing-doc", deterministic=_valid_deterministic(must_cite=["no-such-doc"]))],
    )
    # Matched on the specific message, not just any DatasetError: a doc_id
    # that exists nowhere in the corpus would also fail the "wrong tenant"
    # check below it, so a bare match="missing-doc" does not isolate this
    # rule from that one.
    with pytest.raises(DatasetError, match="unknown doc_id"):
        load_cases(dataset, corpus_dir)


def test_doc_id_present_under_different_tenant(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    # doc-b belongs to globex; this case's principal is acme (the default).
    # citations_subset_of is set to null (not the default ["doc-a"]) so this
    # case does not also trip the must_cite-subset rule -- that would let
    # the test pass under a mutation that disables *this* rule's check.
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "wrong-tenant",
                deterministic=_valid_deterministic(
                    must_cite=["doc-b"], citations_subset_of=None, subset_note="not asserted here"
                ),
            )
        ],
    )
    with pytest.raises(DatasetError, match="is not under tenant"):
        load_cases(dataset, corpus_dir)


def test_must_cite_not_subset_of_citations_subset_of(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "not-subset",
                principal={"tenant": "globex", "user": "eval-agent", "groups": ["oncall"]},
                deterministic=_valid_deterministic(
                    must_cite=["doc-b"], citations_subset_of=[]
                ),
            )
        ],
    )
    with pytest.raises(DatasetError, match="not-subset"):
        load_cases(dataset, corpus_dir)


def test_must_cite_and_must_not_cite_overlap(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    # citations_subset_of is null here so this case does not also trip the
    # must_not_cite/citations_subset_of overlap rule (both default to
    # ["doc-a"], which would mask a mutation that disables *this* check).
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "cite-conflict",
                deterministic=_valid_deterministic(
                    must_cite=["doc-a"],
                    must_not_cite=["doc-a"],
                    citations_subset_of=None,
                    subset_note="not asserted here",
                ),
            )
        ],
    )
    with pytest.raises(DatasetError, match="must_cite and must_not_cite overlap"):
        load_cases(dataset, corpus_dir)


def test_must_not_cite_and_citations_subset_of_overlap(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "subset-conflict",
                deterministic=_valid_deterministic(
                    must_cite=[], must_not_cite=["doc-a"], citations_subset_of=["doc-a"]
                ),
            )
        ],
    )
    with pytest.raises(DatasetError, match="subset-conflict"):
        load_cases(dataset, corpus_dir)


def test_status_null_without_status_note(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [_valid_case("no-status-note", deterministic=_valid_deterministic(status=None))],
    )
    with pytest.raises(DatasetError, match="no-status-note"):
        load_cases(dataset, corpus_dir)


def test_citations_subset_of_null_without_subset_note(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "no-subset-note",
                deterministic=_valid_deterministic(citations_subset_of=None),
            )
        ],
    )
    with pytest.raises(DatasetError, match="no-subset-note"):
        load_cases(dataset, corpus_dir)


def test_judged_null_without_judged_skip_reason(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(tmp_path, [_valid_case("no-skip-reason", judged=None)])
    with pytest.raises(DatasetError, match="no-skip-reason"):
        load_cases(dataset, corpus_dir)


def test_must_cite_null_rejected(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [_valid_case("must-cite-null", deterministic=_valid_deterministic(must_cite=None))],
    )
    with pytest.raises(DatasetError, match="must-cite-null"):
        load_cases(dataset, corpus_dir)


def test_must_not_cite_null_rejected(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "must-not-cite-null", deterministic=_valid_deterministic(must_not_cite=None)
            )
        ],
    )
    with pytest.raises(DatasetError, match="must-not-cite-null"):
        load_cases(dataset, corpus_dir)


def test_requires_unknown_id(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path, [_valid_case("has-requires", requires=["does-not-exist"])]
    )
    with pytest.raises(DatasetError, match="has-requires"):
        load_cases(dataset, corpus_dir)


def test_requires_self_reference(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(tmp_path, [_valid_case("self-ref", requires=["self-ref"])])
    # A self-reference is also a one-node cycle, so the general cycle
    # detector below would catch it too (with a different message) --
    # matching on the dedicated "requires itself" text isolates the
    # early, explicit check from that backstop.
    with pytest.raises(DatasetError, match="requires itself"):
        load_cases(dataset, corpus_dir)


def test_requires_two_node_cycle(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case("cycle-a", requires=["cycle-b"]),
            _valid_case("cycle-b", requires=["cycle-a"]),
        ],
    )
    with pytest.raises(DatasetError, match="cycle"):
        load_cases(dataset, corpus_dir)


def test_duplicate_fact_id_within_case(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "dup-fact-id",
                judged=_valid_judged(
                    expected_facts=[{"id": "fact_x", "text": "one thing"}],
                    forbidden_facts=[{"id": "fact_x", "text": "a different thing"}],
                ),
            )
        ],
    )
    with pytest.raises(DatasetError, match="dup-fact-id"):
        load_cases(dataset, corpus_dir)


def test_empty_fact_text(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "empty-fact-text",
                judged=_valid_judged(
                    expected_facts=[{"id": "fact_1", "text": "   "}]
                ),
            )
        ],
    )
    with pytest.raises(DatasetError, match="empty-fact-text"):
        load_cases(dataset, corpus_dir)


def test_principal_group_invalid_format(tmp_path: Path) -> None:
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "bad-group-format",
                principal={"tenant": "acme", "user": "eval-agent", "groups": ["not a group!"]},
            )
        ],
    )
    with pytest.raises(DatasetError, match="bad-group-format"):
        load_cases(dataset, corpus_dir)


def test_principal_group_format_valid_but_ungranted_is_accepted(tmp_path: Path) -> None:
    """A format-legal group id the corpus never grants anything to is still a
    legal principal -- it lets a dataset assert what an unprivileged-but-valid
    caller sees, which is exactly what `globex-oncall-ack-window-denied`-style
    cases need (design §8)."""
    corpus_dir = _write_corpus(tmp_path)
    dataset = _write_dataset(
        tmp_path,
        [
            _valid_case(
                "ungranted-group",
                principal={
                    "tenant": "acme",
                    "user": "eval-agent",
                    "groups": ["some-group-nobody-grants"],
                },
            )
        ],
    )
    cases = load_cases(dataset, corpus_dir)
    assert [case.id for case in cases] == ["ungranted-group"]
    assert cases[0].groups == ("some-group-nobody-grants",)


def test_shipped_dataset_loads_clean_against_real_corpus() -> None:
    cases = load_cases(_SHIPPED_DATASET, SAMPLE_DOCS_DIR)
    assert len(cases) == 10
    assert len({case.id for case in cases}) == 10
