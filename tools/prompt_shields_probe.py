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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

import httpx

API_VERSION = "2024-09-01"
MAX_FIELD_CODE_POINTS = 1000
# 429 handling (spec F1): narrow and strictly bounded -- this path spends
# money, so it must never grow into a general-purpose retry loop.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3.0
INTER_CALL_DELAY_SECONDS = 1.0
# A server-supplied Retry-After is honored, but not unconditionally: a
# malicious or misconfigured value (e.g. 100000) must not turn a bounded
# probe into a multi-hour sleep. This is a ceiling, not a target -- ordinary
# values stay well under it.
RETRY_CAP_SECONDS = 60.0
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
    # Populated only when a 2xx body fails strict parsing (F3): key names
    # and each analysis field's presence/type, so an unexpected shape is
    # diagnosable from the evidence file alone. Never response text.
    body_shape: dict[str, Any] | None = None


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


_EXPECTED_REGULAR_KIND_COUNTS: dict[str, int] = {
    "baseline": 1, "direct": 1, "indirect": 2, "false_positive": 1, "zh_tw": 1,
}


def validate_matrix(cases: tuple[Case, ...]) -> None:
    """Fail-fast on the fixed 8-case matrix shape (spec §5): exactly 8 cases,
    unique ids, exactly one c4_only_user, exactly one c4_only_docs, exactly
    six regular cases with the exact kind composition
    _EXPECTED_REGULAR_KIND_COUNTS, and the C4 field omissions. A mis-shaped
    fixture (e.g. 5 regular + 2 c4_only_user + 1 c4_only_docs, or six
    `baseline` regular cases) must not produce a 'successful' probe (Day 21
    review P2 — counts, not a kind->case dict that silently collapses
    duplicates)."""
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
    actual_kind_counts: dict[str, int] = {}
    for c in regular:
        actual_kind_counts[c.kind] = actual_kind_counts.get(c.kind, 0) + 1
    if actual_kind_counts != _EXPECTED_REGULAR_KIND_COUNTS:
        raise ValueError(
            "regular case kind composition mismatch: expected "
            f"{_EXPECTED_REGULAR_KIND_COUNTS}, got {actual_kind_counts}"
        )
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


def _parse_user_field(body: dict[str, Any], *, optional: bool) -> tuple[bool | None, bool]:
    """Parse userPromptAnalysis. Returns (attackDetected-or-None, ok).

    Absent or explicit null is accepted (None, ok=True) only when `optional`
    (the case sent no userPrompt, i.e. c4_only_docs) -- that omission is the
    finding the case exists to record, not a parse error. A field that is
    *present but malformed* is always a failure, optional or not: "expected
    omission" is not "anything goes".
    """
    if "userPromptAnalysis" not in body or body.get("userPromptAnalysis") is None:
        return (None, True) if optional else (None, False)
    upa = body["userPromptAnalysis"]
    if not isinstance(upa, dict):
        return None, False
    val = upa.get("attackDetected")
    if not isinstance(val, bool):  # bool is not int here: rejects 0/1 too
        return None, False
    return val, True


def _parse_documents_field(
    body: dict[str, Any], *, expected: int
) -> tuple[tuple[bool, ...], bool]:
    """Parse documentsAnalysis. Absent or null is treated as an empty list.

    That's only ever valid when `expected == 0` (the c4_only_user case, which
    sent no documents) -- the cardinality check below rejects every other
    mismatch exactly as it always has, so no separate "optional" flag is
    needed here the way it is for the user field.
    """
    dsa = body.get("documentsAnalysis")
    if dsa is None:
        dsa = []
    if not isinstance(dsa, list) or len(dsa) != expected:
        return (), False
    docs: list[bool] = []
    for entry in dsa:
        if not isinstance(entry, dict) or not isinstance(entry.get("attackDetected"), bool):
            return (), False
        docs.append(entry["attackDetected"])
    return tuple(docs), True


def _parse_verdict_body(case: Case, body: Any) -> tuple[bool | None, tuple[bool, ...]] | None:
    """Strictly parse a 2xx verdict, with the C4 partial-verdict leniency
    scoped to exactly the field each C4 case omitted (spec F2). Returns None
    (=> failure) on any other schema deviation."""
    if not isinstance(body, dict):
        return None
    user, user_ok = _parse_user_field(body, optional=case.user_prompt is None)
    if not user_ok:
        return None
    docs, docs_ok = _parse_documents_field(body, expected=len(case.documents))
    if not docs_ok:
        return None
    return user, docs


def _describe_body_shape(body: Any) -> dict[str, Any]:
    """Capture a 2xx body's *shape* for a diagnosable failure record: key
    names and, for each analysis field, whether it was absent, null, or
    present-with-what-type. Keys and types only -- never response text --
    so a future unexpected shape is diagnosable from the evidence file
    alone instead of requiring another billable run (spec F3)."""
    if not isinstance(body, dict):
        return {"top_level_type": type(body).__name__}
    shape: dict[str, Any] = {"top_level_keys": sorted(body.keys())}
    for field in ("userPromptAnalysis", "documentsAnalysis"):
        if field not in body:
            shape[field] = "absent"
        elif body[field] is None:
            shape[field] = "null"
        else:
            shape[field] = f"present:{type(body[field]).__name__}"
    return shape


def classify_response(case: Case, status: int, body: Any) -> Verdict:
    is_c4 = case.kind in ("c4_only_user", "c4_only_docs")
    if 200 <= status < 300:
        parsed = _parse_verdict_body(case, body)
        if parsed is None:
            return Verdict(
                "failure", status, None, (), None, None,
                "malformed 2xx verdict body", body_shape=_describe_body_shape(body),
            )
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


def _retry_delay_seconds(retry_after: str | None) -> float:
    """Parse a Retry-After header defensively: only a well-formed,
    non-negative number of seconds is honored, and it is capped at
    RETRY_CAP_SECONDS so an absurd server value cannot turn a bounded probe
    into an hours-long sleep. A missing, malformed, or negative value falls
    back to the fixed backoff rather than sleeping for an unparseable or
    nonsensical duration."""
    if retry_after is not None:
        try:
            parsed = float(retry_after)
        except ValueError:
            parsed = -1.0
        if parsed >= 0:
            return min(parsed, RETRY_CAP_SECONDS)
    return RETRY_BACKOFF_SECONDS


def _post_with_backoff(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[httpx.Response, int]:
    """POST with a narrow, bounded retry: 429 only, up to MAX_ATTEMPTS total
    attempts, honoring Retry-After when present. No retry on any other
    status -- this path spends money, so it must never become a
    general-purpose retry loop. Returns the final response and how many
    attempts it took, so evidence can show exactly where throttling
    occurred."""
    attempts = 0
    while True:
        attempts += 1
        response = client.post(url, headers=headers, json=json_body)
        if response.status_code != 429 or attempts >= MAX_ATTEMPTS:
            return response, attempts
        sleep(_retry_delay_seconds(response.headers.get("Retry-After")))


def fixture_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_evidence_header(
    cases_file: Path, *, now: Callable[[], str] = _utc_now_iso
) -> dict[str, Any]:
    """Evidence-file fields that describe the run itself, not any one case.

    `region` and `sku` are read from AZ_LOCATION and AZ_CONTENT_SAFETY_SKU --
    the same env var names infra/scripts/create-content-safety.sh and
    run-content-safety-probe.sh already use -- and default to null when
    unset, so a manual or partial invocation is honest about not knowing
    them rather than guessing a value. run-content-safety-probe.sh forwards
    the account's ACTUAL sku (read back via `account show` after create
    returns), not the requested one, so this field is accurate even when
    create-content-safety.sh's own SKU-fallback retry (F0 -> S0) fired
    inside its child process. `started_at` is captured once, here, in UTC.

    The three JSONs committed for the 2026-08-07 live probe predate these
    fields -- their attribution is documented in prose in the evidence file,
    not machine-recorded.
    """
    return {
        "api_version": API_VERSION,
        "fixture_sha256": fixture_sha256(cases_file),
        "surface": "standalone-content-safety",
        "started_at": now(),
        "region": os.environ.get("AZ_LOCATION"),
        "sku": os.environ.get("AZ_CONTENT_SAFETY_SKU"),
    }


def _build_payload(case: Case) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if case.user_prompt is not None:
        payload["userPrompt"] = case.user_prompt
    if case.documents:
        payload["documents"] = list(case.documents)
    return payload


def run(cases_file: Path, evidence_out: Path) -> int:
    # Captured before any network call, so it reflects when the run actually
    # started, not when the evidence file happens to get written.
    header = build_evidence_header(cases_file)
    endpoint = os.environ["CONTENT_SAFETY_ENDPOINT"].rstrip("/")
    key = os.environ["CONTENT_SAFETY_KEY"]
    cases = load_cases(cases_file)
    validate_matrix(cases)  # fixed 8-case contract before any billable call
    url = f"{endpoint}/contentsafety/text:shieldPrompt?api-version={API_VERSION}"
    results: list[dict[str, Any]] = []
    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for i, case in enumerate(cases):
            if i > 0:
                # Fixed inter-call pacing: F0's rate limit is a short-window
                # one, and cases 6/7 hit it back-to-back with no pacing.
                time.sleep(INTER_CALL_DELAY_SECONDS)
            resp, attempts = _post_with_backoff(
                client, url, headers={"Ocp-Apim-Subscription-Key": key},
                json_body=_build_payload(case),
            )
            try:
                body: Any = resp.json()
            except ValueError:
                body = {"_nonjson": resp.text[:200]}
            v = classify_response(case, resp.status_code, body)
            if v.outcome == "failure":
                failures += 1
            results.append({
                "id": case.id, "kind": case.kind, "status": v.status,
                "outcome": v.outcome, "attempts": attempts,
                "user_attack_detected": v.user_attack_detected,
                "documents_attack_detected": list(v.documents_attack_detected),
                "error_code": v.error_code, "error_message": v.error_message,
                "detail": v.detail, "body_shape": v.body_shape,
                "user_prompt_len": len(case.user_prompt) if case.user_prompt else 0,
                "document_lens": [len(d) for d in case.documents],
            })
    Path(evidence_out).write_text(json.dumps({
        **header,
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
