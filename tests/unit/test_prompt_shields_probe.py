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

import httpx
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


# --- F2: C4 partial-verdict 2xx must be a recordable observation ---------


def test_classify_c4_only_docs_2xx_missing_user_analysis_is_observation() -> None:
    # No userPrompt was sent; userPromptAnalysis simply isn't in the body.
    # This is exactly the finding case 8 exists to make, not a parse error.
    body = {"documentsAnalysis": [{"attackDetected": True}]}
    v = classify_response(_case("c4_only_docs", user=None, docs=("d",)), 200, body)
    assert v.outcome == "observation"
    assert v.user_attack_detected is None  # absent, never coerced to False
    assert v.documents_attack_detected == (True,)


def test_classify_c4_only_docs_2xx_null_user_analysis_is_observation() -> None:
    # Same finding, but the service sends an explicit null instead of
    # omitting the key. Both must be treated as the expected omission.
    body = {"userPromptAnalysis": None, "documentsAnalysis": [{"attackDetected": False}]}
    v = classify_response(_case("c4_only_docs", user=None, docs=("d",)), 200, body)
    assert v.outcome == "observation"
    assert v.user_attack_detected is None
    assert v.documents_attack_detected == (False,)


def test_classify_c4_only_user_2xx_missing_documents_analysis_is_observation() -> None:
    # Mirror of the above: no documents were sent, so documentsAnalysis is
    # absent. userPromptAnalysis must still carry a real bool.
    body = {"userPromptAnalysis": {"attackDetected": True}}
    v = classify_response(_case("c4_only_user", user="u", docs=()), 200, body)
    assert v.outcome == "observation"
    assert v.user_attack_detected is True
    assert v.documents_attack_detected == ()


def test_classify_c4_only_docs_2xx_wrong_document_cardinality_is_failure() -> None:
    # c4_only_docs sends exactly one document; two verdicts back is a real
    # cardinality mismatch, not the expected-omission leniency.
    body = {"documentsAnalysis": [{"attackDetected": True}, {"attackDetected": False}]}
    v = classify_response(_case("c4_only_docs", user=None, docs=("d",)), 200, body)
    assert v.outcome == "failure"


def test_classify_non_c4_2xx_missing_analysis_is_still_failure() -> None:
    # Cases 1-6 keep the strict contract: the partial-verdict leniency is
    # scoped to the two C4 cases only.
    body = {"documentsAnalysis": [{"attackDetected": True}]}
    v = classify_response(_case("direct"), 200, body)
    assert v.outcome == "failure"


# --- F3: malformed 2xx must capture the body's shape, not just reject it --


def test_classify_malformed_2xx_records_body_shape() -> None:
    body = {"unexpected": "shape", "userPromptAnalysis": "not-a-dict"}
    v = classify_response(_case("baseline"), 200, body)
    assert v.outcome == "failure"
    assert v.body_shape is not None
    assert v.body_shape["top_level_keys"] == ["unexpected", "userPromptAnalysis"]
    assert v.body_shape["userPromptAnalysis"] == "present:str"
    assert v.body_shape["documentsAnalysis"] == "absent"


def test_classify_2xx_success_leaves_body_shape_none() -> None:
    body = {"userPromptAnalysis": {"attackDetected": True},
            "documentsAnalysis": [{"attackDetected": False}]}
    v = classify_response(_case("direct"), 200, body)
    assert v.body_shape is None


# --- F1: bounded pacing/retry on 429 --------------------------------------


def _http_response(
    status: int, *, json_body: dict | None = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    request = httpx.Request("POST", "https://cs.example/contentsafety/text:shieldPrompt")
    if json_body is not None:
        return httpx.Response(status, request=request, json=json_body, headers=headers)
    return httpx.Response(status, request=request, headers=headers)


class _FakeClient:
    """Stands in for httpx.Client: returns queued responses in call order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def post(self, url: str, *, headers: dict[str, str], json: dict) -> httpx.Response:
        self.calls += 1
        return self._responses.pop(0)


def test_post_with_backoff_retries_on_429_then_succeeds() -> None:
    client = _FakeClient([
        _http_response(429),
        _http_response(200, json_body={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": False}],
        }),
    ])
    sleeps: list[float] = []
    response, attempts = prompt_shields_probe._post_with_backoff(
        client, "https://cs.example/x", headers={}, json_body={}, sleep=sleeps.append
    )
    assert attempts == 2
    assert response.status_code == 200
    assert len(sleeps) == 1
    v = classify_response(_case("direct"), response.status_code, response.json())
    assert v.outcome == "observation"


def test_post_with_backoff_honors_retry_after_header() -> None:
    client = _FakeClient([
        _http_response(429, headers={"Retry-After": "7"}),
        _http_response(200, json_body={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": False}],
        }),
    ])
    sleeps: list[float] = []
    prompt_shields_probe._post_with_backoff(
        client, "https://cs.example/x", headers={}, json_body={}, sleep=sleeps.append
    )
    assert sleeps == [7.0]


def test_post_with_backoff_falls_back_on_malformed_retry_after() -> None:
    client = _FakeClient([
        _http_response(429, headers={"Retry-After": "not-a-number"}),
        _http_response(200, json_body={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": False}],
        }),
    ])
    sleeps: list[float] = []
    prompt_shields_probe._post_with_backoff(
        client, "https://cs.example/x", headers={}, json_body={}, sleep=sleeps.append
    )
    assert sleeps == [prompt_shields_probe.RETRY_BACKOFF_SECONDS]


def test_post_with_backoff_exhausts_bound_and_stays_429() -> None:
    client = _FakeClient([_http_response(429) for _ in range(10)])
    sleeps: list[float] = []
    response, attempts = prompt_shields_probe._post_with_backoff(
        client, "https://cs.example/x", headers={}, json_body={}, sleep=sleeps.append
    )
    assert response.status_code == 429
    assert attempts == prompt_shields_probe.MAX_ATTEMPTS
    assert client.calls == prompt_shields_probe.MAX_ATTEMPTS
    assert len(sleeps) == prompt_shields_probe.MAX_ATTEMPTS - 1  # no sleep after the last attempt
    v = classify_response(
        _case("c4_only_docs", user=None), 429, {"error": {"code": "x", "message": "y"}}
    )
    assert v.outcome == "failure"
