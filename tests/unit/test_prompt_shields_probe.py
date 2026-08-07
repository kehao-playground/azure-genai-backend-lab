"""Pure helpers behind `tools/prompt_shields_probe.py`'s claims.

The probe is the only thing in this repo that talks to Azure AI Content
Safety, so nothing here does: fixture parsing, matrix-shape validation,
payload field isolation, and response classification are all exercised
offline against hand-built dicts.

`tools/` is not a package (no `__init__.py`, not installed), so the module
is loaded by path, the same pattern `tests/unit/test_entra_smoke_helpers.py`
and `tests/unit/test_compare_retrieval.py` use.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "prompt_shields_probe.py"
_SPEC = importlib.util.spec_from_file_location("prompt_shields_probe", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
prompt_shields_probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = prompt_shields_probe
_SPEC.loader.exec_module(prompt_shields_probe)

classify_response = prompt_shields_probe.classify_response
fixture_sha256 = prompt_shields_probe.fixture_sha256
load_cases = prompt_shields_probe.load_cases
# Note: Case / validate_matrix / _build_payload are accessed via the module
# object locally inside the tests that use them, to keep this top-level
# block minimal.


def _write(tmp_path: Path, cases: dict) -> Path:
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(cases), encoding="utf-8")
    return p


def test_load_cases_reads_the_matrix(tmp_path: Path) -> None:
    p = _write(tmp_path, {"cases": [
        {"id": "1-baseline", "user_prompt": "hi", "documents": ["doc"], "kind": "baseline"},
    ]})
    cases = load_cases(p)
    assert cases[0].id == "1-baseline"
    assert cases[0].user_prompt == "hi"
    assert cases[0].documents == ("doc",)


def test_load_cases_fail_fast_on_overlong_user_prompt(tmp_path: Path) -> None:
    p = _write(tmp_path, {"cases": [
        {"id": "x", "user_prompt": "a" * 1001, "documents": [], "kind": "baseline"},
    ]})
    with pytest.raises(ValueError, match="1000"):
        load_cases(p)


def test_load_cases_fail_fast_on_two_documents(tmp_path: Path) -> None:
    p = _write(tmp_path, {"cases": [
        {"id": "x", "user_prompt": "hi", "documents": ["a", "b"], "kind": "baseline"},
    ]})
    with pytest.raises(ValueError, match="one document"):
        load_cases(p)


def test_load_cases_missing_file_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "nope.json")


def _case(kind: str, *, user: str | None = "hi", docs: tuple[str, ...] = ("d",)):
    Case = prompt_shields_probe.Case
    return Case(id=kind, user_prompt=user, documents=docs, kind=kind)


def test_classify_2xx_is_observation() -> None:
    body = {"userPromptAnalysis": {"attackDetected": True},
            "documentsAnalysis": [{"attackDetected": False}]}
    v = classify_response(_case("direct"), 200, body)
    assert v.outcome == "observation" and v.user_attack_detected is True
    assert v.documents_attack_detected == (False,)


def test_classify_non_bool_attack_detected_is_failure() -> None:
    for bad in ("false", 0, 1, None):
        body = {"userPromptAnalysis": {"attackDetected": bad},
                "documentsAnalysis": [{"attackDetected": False}]}
        assert classify_response(_case("direct"), 200, body).outcome == "failure"


def test_classify_malformed_document_entry_is_failure() -> None:
    body = {"userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": ["not a dict"]}
    assert classify_response(_case("indirect"), 200, body).outcome == "failure"


def test_classify_document_cardinality_mismatch_is_failure() -> None:
    # one document sent, zero verdicts back
    body = {"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": []}
    assert classify_response(_case("indirect", docs=("d",)), 200, body).outcome == "failure"
    # zero documents sent, one verdict back
    body2 = {"userPromptAnalysis": {"attackDetected": False},
             "documentsAnalysis": [{"attackDetected": False}]}
    assert classify_response(_case("c4_only_user", docs=()), 200, body2).outcome == "failure"


def test_classify_c4_400_azure_envelope_is_observation() -> None:
    body = {"error": {"code": "InvalidRequestBody", "message": "userPrompt required"}}
    v = classify_response(_case("c4_only_docs", user=None), 400, body)
    assert v.outcome == "observation"
    assert v.user_attack_detected is None  # never map non-2xx to attackDetected=false
    assert v.error_code == "InvalidRequestBody"
    # sanitized shape preserved
    assert v.error_message is not None and "userPrompt" in v.error_message


def test_classify_c4_400_non_azure_body_is_failure() -> None:
    assert classify_response(
        _case("c4_only_docs", user=None), 400, {"unexpected": "shape"}
    ).outcome == "failure"


@pytest.mark.parametrize("status", [403, 404, 409, 429, 500])
def test_classify_c4_other_non_2xx_is_failure(status: int) -> None:
    body = {"error": {"code": "X", "message": "y"}}
    v = classify_response(_case("c4_only_docs", user=None), status, body)
    assert v.outcome == "failure"


def test_classify_malformed_2xx_is_failure() -> None:
    assert classify_response(_case("baseline"), 200, {"no": "verdict"}).outcome == "failure"


def test_classify_cases_1_6_non_2xx_is_failure() -> None:
    # A non-C4 case never accepts a 4xx, even a well-formed Azure envelope.
    body = {"error": {"code": "X", "message": "y"}}
    assert classify_response(_case("baseline"), 400, body).outcome == "failure"


def test_build_payload_field_isolation() -> None:
    _build_payload = prompt_shields_probe._build_payload
    both = _build_payload(_case("baseline", user="u", docs=("d",)))
    assert both == {"userPrompt": "u", "documents": ["d"]}
    user_only = _build_payload(_case("c4_only_user", user="u", docs=()))
    assert user_only == {"userPrompt": "u"}  # no documents key
    docs_only = _build_payload(_case("c4_only_docs", user=None, docs=("d",)))
    assert docs_only == {"documents": ["d"]}  # no userPrompt key


def test_fixture_sha256_is_raw_bytes(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    p.write_bytes(b"abc")
    import hashlib
    assert fixture_sha256(p) == hashlib.sha256(b"abc").hexdigest()


def _matrix() -> tuple:
    Case = prompt_shields_probe.Case
    base = [Case(f"{i}", "u", ("d",), k) for i, k in enumerate(
        ["baseline", "direct", "indirect", "false_positive", "zh_tw", "direct"])]
    base.append(Case("6", "u", (), "c4_only_user"))
    base.append(Case("7", None, ("d",), "c4_only_docs"))
    return tuple(base)


def test_validate_matrix_accepts_wellformed_8_cases() -> None:
    prompt_shields_probe.validate_matrix(_matrix())  # no raise


def test_validate_matrix_rejects_wrong_count() -> None:
    with pytest.raises(ValueError, match="exactly 8"):
        prompt_shields_probe.validate_matrix(_matrix()[:7])


def test_validate_matrix_rejects_duplicate_ids() -> None:
    Case = prompt_shields_probe.Case
    dup = (*_matrix()[:7], Case("0", None, ("d",), "c4_only_docs"))
    with pytest.raises(ValueError, match="unique"):
        prompt_shields_probe.validate_matrix(dup)


def test_validate_matrix_rejects_c4_field_shape() -> None:
    Case = prompt_shields_probe.Case
    bad = (*_matrix()[:6], Case("6", "u", ("d",), "c4_only_user"), _matrix()[7])
    with pytest.raises(ValueError, match="c4_only_user"):
        prompt_shields_probe.validate_matrix(bad)


def test_validate_matrix_rejects_duplicate_c4_kind() -> None:
    # 5 regular + 2 c4_only_user + 1 c4_only_docs = 8 unique ids, but wrong shape.
    Case = prompt_shields_probe.Case
    cases = tuple(
        [Case(f"{i}", "u", ("d",), "baseline") for i in range(5)]
        + [Case("5", "u", (), "c4_only_user"),
           Case("6", "u", (), "c4_only_user"),
           Case("7", None, ("d",), "c4_only_docs")]
    )
    with pytest.raises(ValueError, match="one c4_only_user|six regular"):
        prompt_shields_probe.validate_matrix(cases)


def test_load_cases_rejects_falsy_documents(tmp_path: Path) -> None:
    for bad in ("", 0, False, {}):
        p = _write(tmp_path, {"cases": [
            {"id": "x", "user_prompt": "hi", "documents": bad, "kind": "baseline"},
        ]})
        with pytest.raises(ValueError, match="documents"):
            load_cases(p)


def test_load_cases_rejects_missing_documents_key(tmp_path: Path) -> None:
    p = _write(tmp_path, {"cases": [{"id": "x", "user_prompt": "hi", "kind": "baseline"}]})
    with pytest.raises(ValueError, match="documents"):
        load_cases(p)


def test_load_cases_rejects_non_string_id(tmp_path: Path) -> None:
    p = _write(tmp_path, {"cases": [
        {"id": 7, "user_prompt": "hi", "documents": [], "kind": "baseline"},
    ]})
    with pytest.raises(ValueError, match="id"):
        load_cases(p)


def test_canonical_fixture_loads_and_matches_matrix() -> None:
    # Smoke: the shipped fixture is a valid fixed 8-case matrix.
    root = Path(__file__).resolve().parents[2]  # repo root from tests/unit/
    prompt_shields_probe.validate_matrix(load_cases(root / "tools" / "prompt_shields_cases.json"))
