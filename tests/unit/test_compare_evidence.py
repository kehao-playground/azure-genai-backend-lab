"""Evidence redaction: no raw filter, no group id, no false "verbatim" claim.

`tools/compare_retrieval.py`'s sidecar used to write `SearchDiagnostics
.request_body` straight through. It now writes a redacted copy — the ACL
`filter` (which spells out the querying principal's tenant id and group ids)
is replaced with `filter_present`/`filter_sha256`/`vector_filter_mode`
evidence fields — and the tool's own footer must stop claiming the bodies
are verbatim, since after redaction they no longer are.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "compare_retrieval.py"
_SPEC = importlib.util.spec_from_file_location("compare_retrieval", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
compare_retrieval = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = compare_retrieval
_SPEC.loader.exec_module(compare_retrieval)

_redact_request_body = compare_retrieval._redact_request_body
Evidence = compare_retrieval.Evidence

_RAW_FILTER = (
    "tenant_id eq 'acme' and (not allowed_groups/any() "
    "or allowed_groups/any(g: search.in(g, 'oncall')))"
)


def test_redact_strips_the_raw_filter_and_adds_evidence_fields() -> None:
    body = {
        "search": "how do I escalate a Sev 1 outage at 3am?",
        "top": 5,
        "filter": _RAW_FILTER,
        "vectorFilterMode": "preFilter",
        "vectorQueries": [
            {"kind": "vector", "vector": [0.1, 0.2], "fields": "content_vector", "k": 50}
        ],
    }
    redacted = _redact_request_body(body)
    assert redacted is not None
    assert "filter" not in redacted
    assert "vectorFilterMode" not in redacted
    assert redacted["filter_present"] is True
    assert redacted["filter_sha256"] == hashlib.sha256(_RAW_FILTER.encode("utf-8")).hexdigest()
    assert redacted["vector_filter_mode"] == "preFilter"
    # Everything else travels through untouched.
    assert redacted["search"] == body["search"]
    assert redacted["vectorQueries"] == body["vectorQueries"]


def test_redact_never_mutates_the_original_body() -> None:
    body = {"filter": _RAW_FILTER, "vectorFilterMode": "preFilter", "top": 5}
    original = dict(body)
    _redact_request_body(body)
    assert body == original


def test_redact_of_none_is_none() -> None:
    assert _redact_request_body(None) is None


def test_redact_of_a_keyword_mode_body_with_no_vector_filter_mode() -> None:
    # KEYWORD mode never carries a vector query, so it never carries
    # `vectorFilterMode` either — the redacted field must still be present,
    # just `None`, not silently absent.
    body = {"search": "text", "filter": _RAW_FILTER, "top": 5}
    redacted = _redact_request_body(body)
    assert redacted is not None
    assert redacted["vector_filter_mode"] is None
    assert redacted["filter_present"] is True


def test_rendered_evidence_never_mutates_the_original_request_body_object(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path / "out.md", total_queries=1)
    body = {"filter": _RAW_FILTER, "vectorFilterMode": "preFilter", "top": 5}
    original = dict(body)
    evidence.record_request("Q1", body)
    assert body == original


def test_rendered_evidence_contains_no_raw_filter_string_and_no_group_id(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path / "out.md", total_queries=1)
    body = {
        "search": "q",
        "top": 5,
        "filter": _RAW_FILTER,
        "vectorFilterMode": "preFilter",
    }
    evidence.record_request("Q1", body)
    evidence.add("# report")
    evidence.flush()

    sidecar_text = evidence.sidecar.read_text(encoding="utf-8")
    markdown_text = evidence.out.read_text(encoding="utf-8")
    for text in (sidecar_text, markdown_text):
        assert "allowed_groups" not in text
        assert "oncall" not in text
        assert _RAW_FILTER not in text

    payload = json.loads(sidecar_text)
    assert payload[0]["body"]["filter_present"] is True
    assert "filter" not in payload[0]["body"]


def test_rendered_evidence_never_claims_the_bodies_are_verbatim(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path / "out.md", total_queries=1)
    evidence.record_request("Q1", {"filter": _RAW_FILTER, "top": 5})
    evidence.add("# report")
    evidence.flush()

    markdown_text = evidence.out.read_text(encoding="utf-8")
    assert "verbatim" not in markdown_text.lower()


@pytest.mark.parametrize("module_text", [Path(_MODULE_PATH).read_text(encoding="utf-8")])
def test_module_docstring_and_comments_never_claim_verbatim_either(module_text: str) -> None:
    # The one remaining, unrelated use of the word ("Azure error bodies
    # routinely echo the resource name back verbatim") describes upstream
    # error text, not this tool's own request-body claim — so the assertion
    # is scoped to the sentence that used to make the false claim.
    assert "written verbatim" not in module_text
    assert "Verbatim request bodies" not in module_text
