"""Ephemeral Azure AI Content Safety Prompt Shields probe (Day 21).

Not app code. Sends a fixed case matrix to text:shieldPrompt and records
verdicts. Run via infra/scripts/run-content-safety-probe.sh, which owns the
create/delete lifecycle and the cleanup trap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import httpx

API_VERSION = "2024-09-01"
MAX_FIELD_CODE_POINTS = 1000
CaseKind = Literal[
    "baseline", "direct", "indirect", "false_positive",
    "zh_tw", "c4_only_user", "c4_only_docs",
]


@dataclass(frozen=True)
class Case:
    id: str
    user_prompt: str | None
    documents: tuple[str, ...]
    kind: CaseKind


@dataclass(frozen=True)
class Verdict:
    outcome: Literal["observation", "failure"]
    status: int
    user_attack_detected: bool | None
    documents_attack_detected: tuple[bool, ...]
    error_code: str | None
    error_message: str | None
    detail: str


# Derived from CaseKind, not duplicated: a kind added only here (or only
# there) used to yield a mypy-clean runtime rejection instead of a type error.
_VALID_KINDS = frozenset(get_args(CaseKind))


def load_cases(path: Path) -> tuple[Case, ...]:
    """Per-entry validation. Raises on missing file, bad types, oversized
    fields, >1 document, or an unknown kind. Matrix-shape (exactly 8 cases,
    unique ids, C4 field omissions) is enforced by validate_matrix."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))  # FileNotFoundError propagates
    cases: list[Case] = []
    for entry in raw["cases"]:
        case_id = entry["id"]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case id must be a non-empty str")
        user_prompt = entry.get("user_prompt")
        if user_prompt is not None and not isinstance(user_prompt, str):
            raise ValueError(f"case {case_id}: user_prompt must be str or null")
        # Require the key present and a real list — no falsy normalization
        # ("" / 0 / False / {} must NOT silently become []) (review P2).
        if "documents" not in entry:
            raise ValueError(f"case {case_id}: missing 'documents' (use [] for none)")
        documents_raw = entry["documents"]
        if not isinstance(documents_raw, list) or not all(
            isinstance(d, str) for d in documents_raw
        ):
            raise ValueError(f"case {case_id}: documents must be a list of str")
        documents = tuple(documents_raw)
        if entry["kind"] not in _VALID_KINDS:
            raise ValueError(f"case {case_id}: unknown kind {entry['kind']!r}")
        if user_prompt is not None and len(user_prompt) > MAX_FIELD_CODE_POINTS:
            raise ValueError(
                f"case {case_id}: user_prompt exceeds {MAX_FIELD_CODE_POINTS} code points"
            )
        if len(documents) > 1:
            raise ValueError(f"case {case_id}: at most one document per call")
        for doc in documents:
            if len(doc) > MAX_FIELD_CODE_POINTS:
                raise ValueError(
                    f"case {case_id}: document exceeds {MAX_FIELD_CODE_POINTS} code points"
                )
        cases.append(Case(case_id, user_prompt, documents, entry["kind"]))
    return tuple(cases)


def validate_matrix(cases: tuple[Case, ...]) -> None:
    """Fail-fast on the fixed 8-case matrix shape (spec §5): exactly 8 cases,
    unique ids, exactly one c4_only_user, exactly one c4_only_docs, exactly
    six regular cases, and the C4 field omissions. A mis-shaped fixture (e.g.
    5 regular + 2 c4_only_user + 1 c4_only_docs) must not produce a
    'successful' probe (Day 21 review P2 — counts, not a kind->case dict that
    silently collapses duplicates)."""
    if len(cases) != 8:
        raise ValueError(f"expected exactly 8 cases, got {len(cases)}")
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique")
    user_only = [c for c in cases if c.kind == "c4_only_user"]
    docs_only = [c for c in cases if c.kind == "c4_only_docs"]
    regular = [c for c in cases if c.kind not in ("c4_only_user", "c4_only_docs")]
    if len(user_only) != 1:
        raise ValueError("expected exactly one c4_only_user case")
    if len(docs_only) != 1:
        raise ValueError("expected exactly one c4_only_docs case")
    if len(regular) != 6:
        raise ValueError("expected exactly six regular cases")
    if user_only[0].user_prompt is None or user_only[0].documents:
        raise ValueError("c4_only_user must send user_prompt and no documents")
    if docs_only[0].user_prompt is not None or len(docs_only[0].documents) != 1:
        raise ValueError("c4_only_docs must send exactly one document and no user_prompt")
    for c in regular:
        if c.user_prompt is None or len(c.documents) != 1:
            raise ValueError(f"case {c.id}: cases 1-6 must send user_prompt and one document")


def _is_azure_error_envelope(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("error"), dict)
        and isinstance(body["error"].get("code"), str)
        and isinstance(body["error"].get("message"), str)
    )


def _parse_verdict_body(body: Any, expected_docs: int) -> tuple[bool, tuple[bool, ...]] | None:
    """Strictly parse a 2xx verdict. Returns None (=> failure) on any schema
    deviation: attackDetected must be a real bool (not "false"/0/1/None), each
    documentsAnalysis entry must be a dict with a bool attackDetected, and the
    verdict count must equal the number of documents actually sent."""
    if not isinstance(body, dict):
        return None
    upa = body.get("userPromptAnalysis")
    dsa = body.get("documentsAnalysis")
    if not isinstance(upa, dict) or not isinstance(dsa, list):
        return None
    user = upa.get("attackDetected")
    if not isinstance(user, bool):  # bool is not int here: rejects 0/1 too
        return None
    if len(dsa) != expected_docs:
        return None
    docs: list[bool] = []
    for entry in dsa:
        if not isinstance(entry, dict) or not isinstance(entry.get("attackDetected"), bool):
            return None
        docs.append(entry["attackDetected"])
    return user, tuple(docs)


def classify_response(case: Case, status: int, body: Any) -> Verdict:
    is_c4 = case.kind in ("c4_only_user", "c4_only_docs")
    if 200 <= status < 300:
        parsed = _parse_verdict_body(body, expected_docs=len(case.documents))
        if parsed is None:
            return Verdict("failure", status, None, (), None, None, "malformed 2xx verdict body")
        user, docs = parsed
        return Verdict("observation", status, user, docs, None, None, "ok")
    if is_c4 and status == 400 and _is_azure_error_envelope(body):
        # Documented missing-field rejection is a valid C4 observation; never
        # map non-2xx to attackDetected=false (Day 21 review P2). Preserve the
        # machine-readable code and message shape for the evidence file.
        code = body["error"]["code"]
        message = body["error"]["message"]
        return Verdict("observation", status, None, (), code, message, f"c4 400 {code}")
    return Verdict("failure", status, None, (), None, None, f"unexpected status {status}")


def fixture_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _build_payload(case: Case) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if case.user_prompt is not None:
        payload["userPrompt"] = case.user_prompt
    if case.documents:
        payload["documents"] = list(case.documents)
    return payload


def run(cases_file: Path, evidence_out: Path) -> int:
    endpoint = os.environ["CONTENT_SAFETY_ENDPOINT"].rstrip("/")
    key = os.environ["CONTENT_SAFETY_KEY"]
    cases = load_cases(cases_file)
    validate_matrix(cases)  # fixed 8-case contract before any billable call
    url = f"{endpoint}/contentsafety/text:shieldPrompt?api-version={API_VERSION}"
    results: list[dict[str, Any]] = []
    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for case in cases:
            resp = client.post(url, headers={"Ocp-Apim-Subscription-Key": key},
                               json=_build_payload(case))
            try:
                body: Any = resp.json()
            except ValueError:
                body = {"_nonjson": resp.text[:200]}
            v = classify_response(case, resp.status_code, body)
            if v.outcome == "failure":
                failures += 1
            results.append({
                "id": case.id, "kind": case.kind, "status": v.status,
                "outcome": v.outcome, "user_attack_detected": v.user_attack_detected,
                "documents_attack_detected": list(v.documents_attack_detected),
                "error_code": v.error_code, "error_message": v.error_message,
                "detail": v.detail,
                "user_prompt_len": len(case.user_prompt) if case.user_prompt else 0,
                "document_lens": [len(d) for d in case.documents],
            })
    Path(evidence_out).write_text(json.dumps({
        "api_version": API_VERSION,
        "fixture_sha256": fixture_sha256(cases_file),
        "surface": "standalone-content-safety",
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-file", required=True, type=Path)
    parser.add_argument("--evidence-out", required=True, type=Path)
    args = parser.parse_args()
    return run(args.cases_file, args.evidence_out)


if __name__ == "__main__":
    raise SystemExit(main())
