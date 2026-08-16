"""Pure helpers behind `tools/entra_smoke.py`'s claims.

The live smoke tool is the only thing in this repo that talks to a real Entra
tenant, so nothing here does: every helper it rests on — the OAuth device-code
polling rules, the response-contract assertions, the server-log join, and above
all the redaction that keeps a secret out of `--evidence-out` — is exercised
offline with hand-built `httpx.Response` objects and fake clocks.

`tools/` is not a package (no `__init__.py`, not installed), so the module is
loaded by path, the same pattern `tests/unit/test_agent_demo_helpers.py` and
`tests/unit/test_index_recreate.py` use.
"""

import base64
import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "entra_smoke.py"
_SPEC = importlib.util.spec_from_file_location("entra_smoke", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
entra_smoke = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = entra_smoke
_SPEC.loader.exec_module(entra_smoke)

Check = entra_smoke.Check
SmokeError = entra_smoke.SmokeError
accepted_token_checks = entra_smoke.accepted_token_checks
claim_keys = entra_smoke.claim_keys
decode_claims_unverified = entra_smoke.decode_claims_unverified
gate_backoff_seconds = entra_smoke.gate_backoff_seconds
identity_checks = entra_smoke.identity_checks
identity_log_check = entra_smoke.identity_log_check
poll_chat_gate = entra_smoke.poll_chat_gate
poll_device_token = entra_smoke.poll_device_token
redact_sensitive = entra_smoke.redact_sensitive
rejection_checks = entra_smoke.rejection_checks
render_claim_inventory = entra_smoke.render_claim_inventory
render_evidence = entra_smoke.render_evidence
role_absent_check = entra_smoke.role_absent_check
role_present_check = entra_smoke.role_present_check

# Realistic shapes, because the redaction tests below are only worth anything
# if the strings they push through the renderer look like the real values. A
# tenant id and a user `oid` are GUIDs; an Entra client secret is a ~40
# character run containing a tilde; an access token is a three-segment JWT.
TENANT_ID = "9f4a1c62-8f3e-4d1a-9b77-2c5d6e7f8a90"
USER_ID = "3b2e5d71-4c8a-4e2b-9f10-6d7c8b9a0e11"
CLIENT_APP_ID = "5c1f9a3d-7b2e-4a6c-8d90-1e2f3a4b5c6d"
CLIENT_SECRET = "Xy8Q~aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX"  # noqa: S105 - fake, shaped like a real one
ACCESS_TOKEN = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9"
    ".eyJhdWQiOiJhcGkiLCJvaWQiOiJ1c2VyIn0"
    ".c2lnbmF0dXJlLXRoYXQtbXVzdC1uZXZlci1iZS1yZWNvcmRlZA"
)


def unsigned_test_token(claims: dict[str, Any]) -> str:
    """A JWT-shaped string with no signature.

    `claim_keys` must never verify anything — it is diagnostics-only — so an
    unsigned token is the honest fixture: if the helper ever grew a
    verification step it would fail here rather than silently start needing a
    tenant.
    """

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}."


def _response(
    status: int,
    *,
    json_body: dict[str, Any] | None = None,
    text: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("POST", "https://api.example/api/v1/chat")
    if json_body is not None:
        return httpx.Response(status, request=request, json=json_body, headers=headers)
    return httpx.Response(status, request=request, text=text or "", headers=headers)


UNAUTHORIZED_CHALLENGE = "Bearer"
INSUFFICIENT_SCOPE_CHALLENGE = 'Bearer error="insufficient_scope"'


def _accepted_response(correlation_id: str) -> httpx.Response:
    return _response(
        200, json_body={"message": "ok"}, headers={"X-Correlation-Id": correlation_id}
    )


class _StubClient:
    """Stands in for `httpx.Client` in the `main()` wiring tests.

    Answers every request with `_StubClient.response`, which defaults to the
    403 the `no-role` phase expects — so a run through it passes every check,
    and any non-zero exit in those tests comes from the thing under test.
    """

    response: httpx.Response = _response(
        403,
        json_body={"error": {"code": "insufficient_scope", "message": "..."}},
        headers={"WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return _StubClient.response


# ---------------------------------------------------------------------------
# Claim inventory and evidence redaction: the committed-artifact boundary.
# ---------------------------------------------------------------------------


def test_claim_inventory_contains_sorted_keys_not_values() -> None:
    token = unsigned_test_token({"oid": "secret-oid", "tid": "secret-tid", "scp": "scope"})
    assert claim_keys(token) == ["oid", "scp", "tid"]
    assert "secret-oid" not in render_claim_inventory("delegated", claim_keys(token))


def test_evidence_renderer_never_contains_sensitive_values() -> None:
    rendered = render_evidence(
        phase="full",
        checks=[Check("delegated 200", True, "")],
        delegated_claim_keys=["aud", "oid", "scp", "tid"],
        app_claim_keys=["aud", "oid", "roles", "tid"],
    )
    for forbidden in ("access-token", "client-secret", TENANT_ID, USER_ID):
        assert forbidden not in rendered


def test_evidence_masks_a_secret_that_reaches_a_check_detail() -> None:
    """The discriminating half of the test above.

    The renderer's own arguments carry no secrets today, so a test that only
    passes clean input would pass with the redaction deleted. Check details are
    the one channel built from live material — a status line, a challenge
    header, an OAuth error — so this pushes the four real value shapes through
    that channel and requires the renderer to mask each one. Delete any of the
    three patterns in `redact_sensitive` and this fails.
    """
    rendered = render_evidence(
        phase="full",
        checks=[
            Check("delegated 200", False, f"server said tid={TENANT_ID} oid={USER_ID}"),
            Check("app-only 200", False, f"sent Authorization: Bearer {ACCESS_TOKEN}"),
            Check("token acquisition", False, f"client_secret={CLIENT_SECRET} was rejected"),
            Check(f"audience {CLIENT_APP_ID}", False, ""),
        ],
    )
    # every one of these is actually present in the input above — an assertion
    # about a string no input contains passes with the redaction deleted
    for forbidden in (TENANT_ID, USER_ID, CLIENT_APP_ID, CLIENT_SECRET, ACCESS_TOKEN):
        assert forbidden not in rendered, forbidden
    # masked, not merely dropped — the evidence still says a value was there
    assert "<redacted-guid>" in rendered
    assert "<redacted-jwt>" in rendered
    assert "<redacted-secret>" in rendered


def test_redaction_leaves_the_vocabulary_the_evidence_is_made_of() -> None:
    """The other side of the same coin: a masker broad enough to eat the
    evidence is not a win. These are the exact strings the checks below emit,
    and every one of them must survive verbatim."""
    for kept in (
        "insufficient_scope",
        "unauthorized",
        "authorization_pending",
        'Bearer error="insufficient_scope"',
        "expected 403, got 200",
        "identity resolved",
    ):
        assert redact_sensitive(kept) == kept


def test_evidence_states_failure_when_any_check_failed() -> None:
    passing = render_evidence(phase="no-role", checks=[Check("a", True, "")])
    assert "result: PASS" in passing
    mixed = render_evidence(phase="no-role", checks=[Check("a", True, ""), Check("b", False, "x")])
    assert "result: FAIL" in mixed
    assert "result: PASS" not in mixed


def test_a_run_that_decided_nothing_is_a_failure_not_a_pass() -> None:
    # `all([])` is True, so the empty case has to be excluded explicitly or a
    # run that never got as far as its first check reports PASS.
    empty = render_evidence(phase="full", checks=[])
    assert "result: FAIL" in empty
    assert "checks: 0/0 passed" in empty


def test_claim_keys_refuses_a_string_that_is_not_a_jwt() -> None:
    # `not-a-jwt` is deliberately sent to the API in phase `full`; if it ever
    # reached the decoder, guessing at claims would be fabricating evidence.
    for malformed in ("not-a-jwt", "", "a.b", f"{'x' * 8}.!!!not-base64!!!.sig"):
        with pytest.raises(SmokeError):
            claim_keys(malformed)


def test_decode_is_unverified_and_says_so() -> None:
    # The signature segment is empty and the alg is "none": anything that
    # verified would reject this. The helper must still read the claims, and
    # its docstring must own that this is diagnostics-only.
    claims = decode_claims_unverified(unsigned_test_token({"roles": ["Api.Access"]}))
    assert claims == {"roles": ["Api.Access"]}
    assert "diagnostics" in (decode_claims_unverified.__doc__ or "").lower()


# ---------------------------------------------------------------------------
# Response-contract checks: 401 / 403 challenge and error code.
# ---------------------------------------------------------------------------


def _passed(checks: list[Any]) -> dict[str, bool]:
    return {check.name: check.passed for check in checks}


def test_insufficient_scope_rejection_requires_status_code_and_exact_challenge() -> None:
    response = _response(
        403,
        json_body={
            "error": {"code": "insufficient_scope", "message": "..."},
            "correlation_id": "abc",
        },
        headers={"WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
    )
    checks = rejection_checks(
        "app-only without role",
        response,
        status=403,
        code="insufficient_scope",
        challenge=INSUFFICIENT_SCOPE_CHALLENGE,
    )
    assert len(checks) == 3
    assert all(check.passed for check in checks)


def test_unauthorized_rejection_requires_the_bare_bearer_challenge() -> None:
    response = _response(
        401,
        json_body={"error": {"code": "unauthorized", "message": "..."}, "correlation_id": "abc"},
        headers={"WWW-Authenticate": UNAUTHORIZED_CHALLENGE},
    )
    checks = rejection_checks(
        "id token",
        response,
        status=401,
        code="unauthorized",
        challenge=UNAUTHORIZED_CHALLENGE,
    )
    assert all(check.passed for check in checks)


@pytest.mark.parametrize(
    ("status", "body", "headers", "failing"),
    [
        # right code and challenge, wrong status: a 401 where 403 is required
        # is exactly the defect the whole no-role phase exists to detect.
        (
            401,
            {"error": {"code": "insufficient_scope"}},
            {"WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
            "app-only without role: status 403",
        ),
        # right status, wrong error code
        (
            403,
            {"error": {"code": "forbidden"}},
            {"WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
            "app-only without role: error code",
        ),
        # right status and code, bare challenge instead of the scoped one
        (
            403,
            {"error": {"code": "insufficient_scope"}},
            {"WWW-Authenticate": "Bearer"},
            "app-only without role: WWW-Authenticate challenge",
        ),
        # the challenge header missing altogether
        (
            403,
            {"error": {"code": "insufficient_scope"}},
            {},
            "app-only without role: WWW-Authenticate challenge",
        ),
        # an envelope whose `error` is not an object at all
        (
            403,
            {"error": "insufficient_scope"},
            {"WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
            "app-only without role: error code",
        ),
    ],
)
def test_each_part_of_the_rejection_contract_can_fail_on_its_own(
    status: int, body: dict[str, Any], headers: dict[str, str], failing: str
) -> None:
    checks = rejection_checks(
        "app-only without role",
        _response(status, json_body=body, headers=headers),
        status=403,
        code="insufficient_scope",
        challenge=INSUFFICIENT_SCOPE_CHALLENGE,
    )
    outcomes = _passed(checks)
    assert outcomes[failing] is False
    assert all(passed for name, passed in outcomes.items() if name != failing)


def test_a_non_json_body_fails_the_error_code_check_without_quoting_the_body() -> None:
    # An HTML error page from a proxy in front of the API is the realistic
    # case, and pasting its body into evidence is how a bearer token ends up
    # in a committed file.
    response = _response(
        403,
        text=f"<html>token {ACCESS_TOKEN} rejected</html>",
        headers={"Content-Type": "text/html", "WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
    )
    checks = rejection_checks(
        "app-only without role",
        response,
        status=403,
        code="insufficient_scope",
        challenge=INSUFFICIENT_SCOPE_CHALLENGE,
    )
    outcomes = _passed(checks)
    assert outcomes["app-only without role: error code"] is False
    joined = " ".join(check.detail for check in checks)
    assert ACCESS_TOKEN not in joined
    assert "rejected" not in joined


def test_a_truncated_json_body_is_not_quoted_back_either() -> None:
    # The other half of the same guarantee, and a realistic one: the API does
    # declare `application/json`, the body is cut short mid-flight, and the
    # bytes that did arrive include the Authorization header's token because
    # something upstream echoed the request.
    response = _response(
        403,
        text=f'{{"error": {{"code": "insufficient_scope", "token": "{ACCESS_TOKEN}"',
        headers={"Content-Type": "application/json", "WWW-Authenticate": "Bearer"},
    )
    check = rejection_checks(
        "app-only without role",
        response,
        status=403,
        code="insufficient_scope",
        challenge=INSUFFICIENT_SCOPE_CHALLENGE,
    )[1]
    assert not check.passed
    assert ACCESS_TOKEN not in check.detail
    assert "insufficient_scope" not in check.detail


def test_the_error_envelope_is_only_read_from_a_json_response() -> None:
    # The sharp case for the content-type guard, and the one a body-shaped
    # test cannot reach: something in front of the API answers with a body
    # that *parses* as our envelope while declaring itself as text/html.
    # Without the guard that check passes and the run reports a contract the
    # API never stated.
    response = _response(
        403,
        text=json.dumps({"error": {"code": "insufficient_scope", "message": "..."}}),
        headers={"Content-Type": "text/html", "WWW-Authenticate": INSUFFICIENT_SCOPE_CHALLENGE},
    )
    check = rejection_checks(
        "app-only without role",
        response,
        status=403,
        code="insufficient_scope",
        challenge=INSUFFICIENT_SCOPE_CHALLENGE,
    )[1]
    assert check.name == "app-only without role: error code"
    assert not check.passed
    assert "text/html" in check.detail


# ---------------------------------------------------------------------------
# The bounded vocabularies.
#
# `redact_sensitive` is a denylist of three shapes, and these two values are
# the holes in it: an opaque refresh token and a device `user_code` are neither
# GUID-, JWT- nor tilde-shaped, so masking would not save them. What keeps them
# out of the evidence is that the four channels carrying a provider- or
# server-supplied VALUE — media type, `error.code`, the WWW-Authenticate
# challenge, and the OAuth `error` field — compare it against a known set and
# *describe* it rather than quote it when it does not belong. These tests pin
# that property on each of those four.
#
# Scope, stated because the sentence above is easy to over-read: claim KEY
# NAMES also originate in provider data and reach the evidence through
# `render_evidence` gated by `redact_sensitive` alone, not by `describe`. That
# channel carries names, never values, which is why it is bounded by what it
# transports rather than by a set.
# ---------------------------------------------------------------------------

REFRESH_TOKEN = "0.AXoAV2K3mQx1TEqYtNQ8jRZ2AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
USER_CODE = "F7HKM8QZ4"


def test_the_masker_alone_would_not_save_these_values() -> None:
    # Stated outright, because the guarantee below rests on it being true: if
    # either of these ever reached a detail, the redactor would pass it
    # through untouched. Nothing routes them there — that is the point.
    assert redact_sensitive(REFRESH_TOKEN) == REFRESH_TOKEN
    assert redact_sensitive(USER_CODE) == USER_CODE


def test_an_unrecognized_error_code_is_described_not_quoted() -> None:
    response = _response(
        403,
        json_body={"error": {"code": REFRESH_TOKEN}},
        headers={"Content-Type": "application/json"},
    )
    check = entra_smoke.error_code_check("x", response, "insufficient_scope")
    assert not check.passed
    assert REFRESH_TOKEN not in check.detail
    assert "an unrecognized error code" in check.detail


def test_a_recognized_error_code_is_still_named() -> None:
    # The constraint must not cost the diagnostic: a code this API documents
    # is exactly the thing an operator needs to read.
    response = _response(
        403,
        json_body={"error": {"code": "upstream_error"}},
        headers={"Content-Type": "application/json"},
    )
    check = entra_smoke.error_code_check("x", response, "insufficient_scope")
    assert not check.passed
    assert "upstream_error" in check.detail


def test_an_unrecognized_media_type_is_described_not_quoted() -> None:
    response = _response(403, text="...", headers={"Content-Type": f"x-code/{USER_CODE}"})
    check = entra_smoke.error_code_check("x", response, "insufficient_scope")
    assert not check.passed
    assert USER_CODE not in check.detail
    assert "an unrecognized media type" in check.detail


def test_an_unrecognized_challenge_is_described_not_echoed() -> None:
    # RFC 6750 §3 lets a challenge carry an `error_description`, so the header
    # an unknown intermediary sets is arbitrary text on the wire.
    response = _response(
        401, json_body={}, headers={"WWW-Authenticate": f'Bearer error_description="{USER_CODE}"'}
    )
    check = entra_smoke.challenge_check("x", response, UNAUTHORIZED_CHALLENGE)
    assert not check.passed
    assert USER_CODE not in check.detail
    assert "an unrecognized WWW-Authenticate challenge" in check.detail
    assert UNAUTHORIZED_CHALLENGE in check.detail  # the expectation is still stated


def test_a_challenge_this_api_could_have_sent_is_named() -> None:
    # The diagnostic the constraint must not cost: `invalid_token` where
    # `insufficient_scope` was expected is the difference between "the server
    # rejected the token" and "the server rejected the permission", and a run
    # that only said "it differed" would leave that undebuggable.
    response = _response(
        401, json_body={}, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
    )
    check = entra_smoke.challenge_check("x", response, INSUFFICIENT_SCOPE_CHALLENGE)
    assert not check.passed
    assert 'Bearer error="invalid_token"' in check.detail


def test_a_missing_challenge_is_distinguishable_from_a_different_one() -> None:
    check = entra_smoke.challenge_check("x", _response(401, json_body={}), "Bearer")
    assert not check.passed
    assert "no WWW-Authenticate challenge" in check.detail


def test_an_unrecognized_oauth_error_is_described_not_quoted() -> None:
    request = httpx.Request("POST", "https://login.example/token")

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(400, request=request, json={"error": REFRESH_TOKEN})

    with pytest.raises(SmokeError) as raised:
        poll_device_token(
            FakeClient(),  # type: ignore[arg-type]
            token_url="https://login.example/token",
            client_id="client-id",
            device_code="device-code",
            interval=1,
            expires_in=60,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
    assert REFRESH_TOKEN not in str(raised.value)
    assert "an unrecognized OAuth error" in str(raised.value)


@pytest.mark.parametrize("code", ["consent_required", "interaction_required", "invalid_resource"])
def test_the_tenant_policy_refusals_are_named_not_described(code: str) -> None:
    # These three are the likeliest first-run failure against a real tenant, so
    # they are exactly the codes worth naming: "an unrecognized OAuth error"
    # would send an operator hunting for a fault that Entra already named.
    request = httpx.Request("POST", "https://login.example/token")

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(400, request=request, json={"error": code})

    with pytest.raises(SmokeError) as raised:
        poll_device_token(
            FakeClient(),  # type: ignore[arg-type]
            token_url="https://login.example/token",
            client_id="client-id",
            device_code="device-code",
            interval=1,
            expires_in=60,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
    assert code in str(raised.value)


def test_describe_is_the_single_gate_every_channel_passes_through() -> None:
    assert entra_smoke.describe("slow_down", entra_smoke.OAUTH_ERROR_CODES, "OAuth error") == (
        "'slow_down'"
    )
    assert (
        entra_smoke.describe(USER_CODE, entra_smoke.OAUTH_ERROR_CODES, "OAuth error")
        == "an unrecognized OAuth error"
    )
    assert entra_smoke.describe(None, entra_smoke.API_ERROR_CODES, "error code") == "no error code"


# ---------------------------------------------------------------------------
# Server-log join: one line, exact ids.
# ---------------------------------------------------------------------------

_LOG_TEMPLATE = (
    "2026-08-04 09:00:00,000 INFO azgenai_lab.api.principal "
    "correlation_id={cid} tenant_id={tid} user_id={uid} identity resolved"
)


def test_identity_log_check_matches_one_line_carrying_every_field() -> None:
    lines = [
        "2026-08-04 08:59:59,000 INFO uvicorn.access - 200 OK",
        _LOG_TEMPLATE.format(cid="corr-1", tid=TENANT_ID, uid=USER_ID),
    ]
    check = identity_log_check(
        "delegated",
        lines=lines,
        correlation_id="corr-1",
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )
    assert check.passed


def test_identity_log_check_refuses_fields_scattered_over_several_lines() -> None:
    # The claim is "one request produced one identity line carrying all three
    # ids". Fields spread across the file satisfy a naive per-field search and
    # prove nothing about any single request.
    lines = [
        _LOG_TEMPLATE.format(cid="corr-1", tid="other-tenant", uid="other-user"),
        _LOG_TEMPLATE.format(cid="corr-2", tid=TENANT_ID, uid=USER_ID),
    ]
    check = identity_log_check(
        "delegated",
        lines=lines,
        correlation_id="corr-1",
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )
    assert not check.passed


def test_identity_log_check_requires_the_whole_id_not_a_prefix() -> None:
    # `tenant_id=9f4a1c62-...` contains `tenant_id=9f4a` as a substring, so a
    # substring test would accept a line about a different tenant whose id
    # merely starts the same way.
    lines = [_LOG_TEMPLATE.format(cid="corr-1", tid=f"{TENANT_ID}-shadow", uid=USER_ID)]
    check = identity_log_check(
        "delegated",
        lines=lines,
        correlation_id="corr-1",
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )
    assert not check.passed


def test_identity_log_check_requires_the_message_not_just_the_fields() -> None:
    # Every log record carries correlation_id/tenant_id/user_id (the record
    # factory stamps them), so the fields alone match any line the request
    # emitted. Only "identity resolved" says the resolver ran.
    lines = [
        "2026-08-04 09:00:00,000 INFO azgenai_lab.services.azure_openai "
        f"correlation_id=corr-1 tenant_id={TENANT_ID} user_id={USER_ID} upstream call"
    ]
    check = identity_log_check(
        "delegated",
        lines=lines,
        correlation_id="corr-1",
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )
    assert not check.passed


def test_identity_log_check_failure_detail_carries_no_ids() -> None:
    check = identity_log_check(
        "delegated",
        lines=[],
        correlation_id="corr-1",
        tenant_id=TENANT_ID,
        user_id=USER_ID,
    )
    assert not check.passed
    assert TENANT_ID not in check.detail
    assert USER_ID not in check.detail


def test_identity_checks_always_report_both_halves_of_the_join() -> None:
    token = unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID})
    lines = [_LOG_TEMPLATE.format(cid="corr-1", tid=TENANT_ID, uid=USER_ID)]
    checks = identity_checks("delegated", token=token, lines=lines, correlation_id="corr-1")
    assert len(checks) == 2
    assert all(check.passed for check in checks)


def test_identity_checks_fail_rather_than_skip_when_the_claims_are_missing() -> None:
    # A token with no `tid`/`oid` leaves nothing to join on. Dropping the log
    # check here would turn "we could not verify this" into a silently shorter,
    # all-green check list — the exact downgrade the brief forbids.
    for claims in ({}, {"tid": TENANT_ID}, {"tid": 1234, "oid": USER_ID}):
        checks = identity_checks(
            "delegated",
            token=unsigned_test_token(claims),
            lines=[_LOG_TEMPLATE.format(cid="corr-1", tid=TENANT_ID, uid=USER_ID)],
            correlation_id="corr-1",
        )
        assert len(checks) == 2, claims
        assert not any(check.passed for check in checks), claims


def test_identity_checks_fail_on_an_undecodable_token() -> None:
    checks = identity_checks("delegated", token="not-a-jwt", lines=[], correlation_id="corr-1")
    assert len(checks) == 2
    assert not any(check.passed for check in checks)


def test_a_rejected_token_still_contributes_an_explicit_failed_check(tmp_path: Path) -> None:
    # Contributing nothing here would make the evidence file's check list
    # silently shorter — which reads as a smaller run, not a failed one. The
    # status check next to it already failed; this says what was not evaluated
    # and why.
    log = tmp_path / "server.log"
    log.write_text("", encoding="utf-8")
    checks = accepted_token_checks(
        "app-only",
        token=unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID, "roles": ["Api.Access"]}),
        response=_response(403, json_body={"error": {"code": "insufficient_scope"}}),
        log_path=log,
        role="Api.Access",
        sleep=lambda _seconds: None,
    )
    assert len(checks) == 1
    assert not checks[0].passed
    assert "not evaluated" in checks[0].detail


def test_an_accepted_token_gets_the_role_and_both_identity_checks(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text(
        _LOG_TEMPLATE.format(cid="corr-9", tid=TENANT_ID, uid=USER_ID) + "\n", encoding="utf-8"
    )
    checks = accepted_token_checks(
        "app-only",
        token=unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID, "roles": ["Api.Access"]}),
        response=_accepted_response("corr-9"),
        log_path=log,
        role="Api.Access",
        sleep=lambda _seconds: None,
    )
    assert [check.name for check in checks] == [
        "app-only: roles claim contains the configured role",
        "app-only: token carries tid and oid",
        "app-only: identity log line",
    ]
    assert all(check.passed for check in checks)


def test_the_delegated_leg_asserts_no_app_role(tmp_path: Path) -> None:
    # A delegated token legitimately carries no `roles` claim, so requiring one
    # there would fail every correct run.
    log = tmp_path / "server.log"
    log.write_text(
        _LOG_TEMPLATE.format(cid="corr-9", tid=TENANT_ID, uid=USER_ID) + "\n", encoding="utf-8"
    )
    checks = accepted_token_checks(
        "delegated",
        token=unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID, "scp": "access_as_user"}),
        response=_accepted_response("corr-9"),
        log_path=log,
        sleep=lambda _seconds: None,
    )
    assert [check.name for check in checks] == [
        "delegated: token carries tid and oid",
        "delegated: identity log line",
    ]
    assert all(check.passed for check in checks)


def test_a_missing_log_line_stays_a_failure_after_every_retry(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text("nothing relevant\n", encoding="utf-8")
    sleeps: list[float] = []
    checks = accepted_token_checks(
        "delegated",
        token=unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID}),
        response=_accepted_response("corr-9"),
        log_path=log,
        sleep=sleeps.append,
    )
    assert [check.passed for check in checks] == [True, False]
    # bounded, and it really did retry rather than give up on the first read
    assert len(sleeps) == entra_smoke.LOG_JOIN_ATTEMPTS - 1


def test_an_unusable_token_is_not_retried_at_all(tmp_path: Path) -> None:
    # Only the log join is in flight. A token with no `tid`/`oid` is a fact
    # about the token, and the verdict is already final on the first read —
    # sleeping through the whole budget over it would cost seconds and change
    # nothing.
    log = tmp_path / "server.log"
    log.write_text("nothing relevant\n", encoding="utf-8")
    sleeps: list[float] = []
    checks = accepted_token_checks(
        "delegated",
        token=unsigned_test_token({"scp": "access_as_user"}),  # no tid, no oid
        response=_accepted_response("corr-9"),
        log_path=log,
        sleep=sleeps.append,
    )
    assert [check.passed for check in checks] == [False, False]
    assert sleeps == []


# ---------------------------------------------------------------------------
# App-role claim checks: present after assignment, absent before it.
# ---------------------------------------------------------------------------


def test_role_present_check_requires_an_exact_role_value() -> None:
    assert role_present_check(
        "app-only", token=unsigned_test_token({"roles": ["Api.Access"]}), role="Api.Access"
    ).passed
    for claims in (
        {},
        {"roles": []},
        # substring, not the role: `in` on the joined string would accept it
        {"roles": ["Api.Access.Extended"]},
        # a bare string instead of the documented array
        {"roles": "Api.Access"},
        {"roles": [None]},
    ):
        check = role_present_check(
            "app-only", token=unsigned_test_token(claims), role="Api.Access"
        )
        assert not check.passed, claims


def test_role_absent_check_is_about_the_key_not_its_contents() -> None:
    # The whole `--phase no-role` proof is "the token endpoint issued a
    # valid-audience token that carries no `roles` claim at all". An empty
    # array is still the claim being emitted, so it is not the state under test.
    assert role_absent_check("app-only", token=unsigned_test_token({"aud": "api"})).passed
    assert not role_absent_check("app-only", token=unsigned_test_token({"roles": []})).passed
    assert not role_absent_check(
        "app-only", token=unsigned_test_token({"roles": ["Api.Access"]})
    ).passed


def test_role_checks_fail_on_an_undecodable_token() -> None:
    assert not role_present_check("app-only", token="not-a-jwt", role="Api.Access").passed
    assert not role_absent_check("app-only", token="not-a-jwt").passed


# ---------------------------------------------------------------------------
# main(): the wiring an operator actually depends on.
# ---------------------------------------------------------------------------


def test_a_prerequisite_failure_still_writes_evidence_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that dies before its first check must not die silently.

    The token endpoint refusing the client secret is the realistic case, and
    the operator needs the same artifact a passing run produces — with the
    failure in it, and still no secret. Zero network: the acquisition step is
    replaced with the failure it would have raised.
    """

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise SmokeError("client credentials request: invalid_client (HTTP 401)")

    monkeypatch.setattr(entra_smoke, "acquire_app_token", refuse)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", CLIENT_SECRET)
    evidence = tmp_path / "evidence.txt"

    exit_code = entra_smoke.main(
        [
            "--phase", "no-role",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--evidence-out", str(evidence),
        ]
    )

    assert exit_code == 1
    written = evidence.read_text(encoding="utf-8")
    assert "result: FAIL" in written
    assert "no-role: prerequisite step" in written
    assert "invalid_client" in written
    # the secret was in this process's environment the whole time
    assert CLIENT_SECRET not in written


def test_a_transport_failure_still_writes_evidence_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The likeliest operator mistake of all: the server is not running.

    `httpx.ConnectError` is not a `SmokeError`, so before this it escaped
    `main` — no evidence file, and a traceback carrying the token URL (and
    with it the tenant GUID) on stderr. In `--phase full` that happens after
    the interactive sign-in has already been completed.
    """

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise httpx.ConnectError(f"All connection attempts failed for {TENANT_ID}")

    monkeypatch.setattr(entra_smoke, "acquire_app_token", refuse)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", CLIENT_SECRET)
    evidence = tmp_path / "evidence.txt"

    exit_code = entra_smoke.main(
        [
            "--phase", "no-role",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--evidence-out", str(evidence),
        ]
    )

    assert exit_code == 1
    written = evidence.read_text(encoding="utf-8")
    assert "result: FAIL" in written
    assert "ConnectError" in written
    # the class, not the message — httpx messages embed the request URL
    assert TENANT_ID not in written
    assert "All connection attempts failed" not in written


def test_the_transport_failure_message_reaches_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kept out of the file, but not thrown away.

    `ConnectError` alone does not say WHICH of the token endpoint, the API and
    the server log failed, and in `--phase full` the operator learns this after
    already completing an interactive sign-in. So the message goes to stderr
    under the same terminal-only warning `_oauth_failure` prints — the one
    place it is allowed to name the tenant.
    """

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise httpx.ConnectError(f"All connection attempts failed for {TENANT_ID}")

    monkeypatch.setattr(entra_smoke, "acquire_app_token", refuse)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", CLIENT_SECRET)

    entra_smoke.main(
        [
            "--phase", "no-role",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
        ]
    )

    captured = capsys.readouterr()
    assert "All connection attempts failed" in captured.err
    assert "terminal only" in captured.err
    # and still not on stdout, which is the evidence text an operator copies
    assert "All connection attempts failed" not in captured.out


def test_an_unwritable_evidence_path_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A directory where a file was asked for: `write_text` raises `IsADirectoryError`.
    # Escaping as a traceback would be the third way this tool can die after a
    # completed sign-in, and it would print the path in a stack frame anyway.
    monkeypatch.setattr(
        entra_smoke, "acquire_app_token", lambda *_a, **_k: unsigned_test_token({"aud": "api"})
    )
    monkeypatch.setattr(entra_smoke.httpx, "Client", _StubClient)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", CLIENT_SECRET)
    blocked = tmp_path / "evidence.txt"
    blocked.mkdir()

    exit_code = entra_smoke.main(
        [
            "--phase", "no-role",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--evidence-out", str(blocked),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Could not write" in captured.err
    # the evidence itself still reached stdout, so nothing measured was lost
    assert "# Entra ID live smoke evidence" in captured.out


def test_a_rotated_log_file_is_a_failed_check_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `read_log_lines` runs between the API calls and the verdict; a log
    # rotated in that window used to take the whole run out with an OSError.
    def vanish(*_args: object, **_kwargs: object) -> list[str]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(entra_smoke, "read_log_lines", vanish)
    monkeypatch.setattr(
        entra_smoke,
        "acquire_app_token",
        lambda *_a, **_k: unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID}),
    )
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", CLIENT_SECRET)
    evidence = tmp_path / "evidence.txt"
    log = tmp_path / "server.log"
    log.write_text("", encoding="utf-8")

    monkeypatch.setattr(_StubClient, "response", _accepted_response("corr-1"))
    monkeypatch.setattr(entra_smoke.httpx, "Client", _StubClient)
    monkeypatch.setattr(
        entra_smoke,
        "request_device_code",
        lambda *_a, **_k: {
            "device_code": "d",
            "user_code": USER_CODE,
            "verification_uri": "https://example.invalid/device",
            "interval": 1,
            "expires_in": 60,
        },
    )
    monkeypatch.setattr(
        entra_smoke,
        "poll_device_token",
        lambda *_a, **_k: {
            "access_token": unsigned_test_token({"tid": TENANT_ID, "oid": USER_ID}),
            "id_token": unsigned_test_token({"aud": "client"}),
        },
    )

    exit_code = entra_smoke.main(
        [
            "--phase", "full",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--server-log", str(log),
            "--evidence-out", str(evidence),
        ]
    )

    assert exit_code == 1
    written = evidence.read_text(encoding="utf-8")
    assert "FileNotFoundError" in written
    assert "result: FAIL" in written


def test_a_missing_client_secret_is_a_configuration_exit_not_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exit 2, and no evidence file: nothing was measured, so writing a FAIL
    # artifact would claim a run that never happened.
    monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)
    evidence = tmp_path / "evidence.txt"
    exit_code = entra_smoke.main(
        [
            "--phase", "no-role",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--evidence-out", str(evidence),
        ]
    )
    assert exit_code == 2
    assert not evidence.exists()


# ---------------------------------------------------------------------------
# Device-code polling.
# ---------------------------------------------------------------------------


def test_device_poll_handles_pending_and_slow_down_without_printing_tokens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = httpx.Request("POST", "https://login.example/token")
    responses = iter(
        [
            httpx.Response(400, request=request, json={"error": "authorization_pending"}),
            httpx.Response(400, request=request, json={"error": "slow_down"}),
            # A second pending AFTER slow_down: RFC 8628 §3.5 says the interval
            # "MUST be increased by 5 seconds for this AND ALL SUBSEQUENT
            # requests", so the raise is permanent. Without this third response
            # the test passes whether the implementation adds 5 seconds once or
            # keeps it forever.
            httpx.Response(400, request=request, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                request=request,
                json={"access_token": "sensitive-access-token", "token_type": "Bearer"},
            ),
        ]
    )

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            assert url == "https://login.example/token"
            assert data["device_code"] == "device-code"
            return next(responses)

    sleeps: list[float] = []
    result = poll_device_token(
        FakeClient(),  # type: ignore[arg-type]
        token_url="https://login.example/token",
        client_id="client-id",
        device_code="device-code",
        interval=2,
        expires_in=60,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
    )
    assert result["access_token"] == "sensitive-access-token"
    assert sleeps == [2, 7, 7]  # raised permanently, not just for the next poll
    assert "sensitive-access-token" not in capsys.readouterr().out


def _device_code_client(payload: dict[str, Any], status: int = 200) -> Any:
    request = httpx.Request("POST", "https://login.example/devicecode")

    class FakeClient:
        sent: list[dict[str, str]] = []

        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            FakeClient.sent.append(data)
            return httpx.Response(status, request=request, json=payload)

    return FakeClient()


_DEVICE_CODE_RESPONSE: dict[str, Any] = {
    "device_code": "d",
    "user_code": USER_CODE,
    "verification_uri": "https://example.invalid/device",
    "interval": 5,
    "expires_in": 900,
}


def test_device_code_request_validates_the_whole_response_shape() -> None:
    payload = entra_smoke.request_device_code(
        _device_code_client(_DEVICE_CODE_RESPONSE),
        tenant_id=TENANT_ID,
        client_id=CLIENT_APP_ID,
        api_app_id=CLIENT_APP_ID,
        scope="access_as_user",
    )
    assert payload["user_code"] == USER_CODE


@pytest.mark.parametrize(
    "broken",
    [
        {"interval": "5"},  # the v1 endpoint's string shape
        {"expires_in": "900"},
        {"interval": True},  # a bool is an int in Python; it is not a poll interval
        {"verification_uri": ""},
        {"device_code": None},
    ],
)
def test_a_malformed_device_code_response_fails_before_the_operator_signs_in(
    broken: dict[str, Any],
) -> None:
    # The caller prints the sign-in prompt as soon as this returns, so every
    # field the poll loop depends on is validated *here*. Validating `interval`
    # at the poll site instead would send the operator to a browser, wait for
    # them to authorize, and only then crash on a field that was already wrong.
    with pytest.raises(SmokeError):
        entra_smoke.request_device_code(
            _device_code_client({**_DEVICE_CODE_RESPONSE, **broken}),
            tenant_id=TENANT_ID,
            client_id=CLIENT_APP_ID,
            api_app_id=CLIENT_APP_ID,
            scope="access_as_user",
        )


def test_device_poll_sends_the_device_code_grant() -> None:
    request = httpx.Request("POST", "https://login.example/token")
    sent: list[dict[str, str]] = []

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            sent.append(data)
            return httpx.Response(200, request=request, json={"access_token": "t"})

    poll_device_token(
        FakeClient(),  # type: ignore[arg-type]
        token_url="https://login.example/token",
        client_id="client-id",
        device_code="device-code",
        interval=5,
        expires_in=60,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert sent == [
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": "client-id",
            "device_code": "device-code",
        }
    ]


def test_device_poll_raises_on_a_terminal_oauth_error() -> None:
    request = httpx.Request("POST", "https://login.example/token")

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                400,
                request=request,
                json={
                    "error": "authorization_declined",
                    # Microsoft's descriptions carry tenant GUIDs and trace
                    # ids; the raised message must not become the channel that
                    # walks one into an evidence file.
                    "error_description": f"AADSTS65004 tenant {TENANT_ID}",
                },
            )

    with pytest.raises(SmokeError) as raised:
        poll_device_token(
            FakeClient(),  # type: ignore[arg-type]
            token_url="https://login.example/token",
            client_id="client-id",
            device_code="device-code",
            interval=1,
            expires_in=60,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
    assert "authorization_declined" in str(raised.value)
    assert TENANT_ID not in str(raised.value)


def test_the_provider_description_goes_to_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stream matters, not just the exclusion from the exception.

    stdout is the evidence text an operator copies out of the terminal, so a
    description naming the tenant must not land there. stderr is the one
    channel it is allowed on, under the warning.
    """
    request = httpx.Request("POST", "https://login.example/token")

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                400,
                request=request,
                json={
                    "error": "invalid_client",
                    "error_description": f"AADSTS7000215 tenant {TENANT_ID}",
                },
            )

    with pytest.raises(SmokeError):
        poll_device_token(
            FakeClient(),  # type: ignore[arg-type]
            token_url="https://login.example/token",
            client_id="client-id",
            device_code="device-code",
            interval=1,
            expires_in=60,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
    captured = capsys.readouterr()
    assert TENANT_ID in captured.err
    assert "terminal only" in captured.err
    assert TENANT_ID not in captured.out
    assert "AADSTS7000215" not in captured.out


def test_device_poll_stops_at_expiry_instead_of_looping_forever() -> None:
    request = httpx.Request("POST", "https://login.example/token")
    clock = iter([0.0, 1.0, 400.0])
    # A bounded supply of pending responses, so a poll loop that lost its
    # deadline check fails here loudly instead of spinning forever. (It does
    # spin: the user never authorizes, so `authorization_pending` is the only
    # answer the endpoint will ever give.)
    remaining = iter(range(3))

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            next(remaining)
            return httpx.Response(400, request=request, json={"error": "authorization_pending"})

    with pytest.raises(SmokeError) as raised:
        poll_device_token(
            FakeClient(),  # type: ignore[arg-type]
            token_url="https://login.example/token",
            client_id="client-id",
            device_code="device-code",
            interval=5,
            expires_in=300,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(clock),
        )
    assert "expired" in str(raised.value)


def test_device_poll_rejects_a_non_json_error_body_without_quoting_it() -> None:
    request = httpx.Request("POST", "https://login.example/token")

    class FakeClient:
        def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                502, request=request, text=f"<html>gateway error {ACCESS_TOKEN}</html>"
            )

    with pytest.raises(SmokeError) as raised:
        poll_device_token(
            FakeClient(),  # type: ignore[arg-type]
            token_url="https://login.example/token",
            client_id="client-id",
            device_code="device-code",
            interval=1,
            expires_in=60,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )
    assert "502" in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)


# ---------------------------------------------------------------------------
# Gate mode: the ACA deploy script's readiness gate (Task 8).
#
# Retry policy is a deliberate correction of the original brief, which said
# "retry on 401/503". A 401 from THIS API's own bearer-token check is decided
# before anything touches Azure OpenAI (see `principal.py`), so it can never
# become a 200 by waiting -- unlike a 5xx, which is exactly the shape of the
# role-assignment-not-propagated-yet case Day 20 measured (14m44s). So the
# tests below retry 429/5xx/connection errors and treat 401/403 as terminal.
# ---------------------------------------------------------------------------


def _chat_gate_response(
    status: int,
    *,
    json_body: dict[str, Any] | None = None,
    correlation_id: str = "corr-gate",
) -> httpx.Response:
    return _response(
        status,
        json_body=json_body,
        headers={"X-Correlation-Id": correlation_id},
    )


class _SequenceClient:
    """Answers each `.post()` with the next response in a fixed sequence.

    Raising the sentinel on exhaustion (rather than a generic `StopIteration`)
    is what makes "the loop retried more times than the test expected" fail
    with a readable message instead of an opaque `RuntimeError` from a
    generator being driven past its end.
    """

    def __init__(self, responses: Sequence[httpx.Response | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        self.calls.append(url)
        if not self._responses:
            raise AssertionError(f"gate polled more times than expected (call {len(self.calls)})")
        next_response = self._responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def test_gate_backoff_schedule_is_5_10_20_then_30_cap() -> None:
    assert [gate_backoff_seconds(n) for n in (1, 2, 3, 4, 5, 100)] == [
        5.0,
        10.0,
        20.0,
        30.0,
        30.0,
        30.0,
    ]


def test_gate_retries_503_and_429_then_succeeds_on_200() -> None:
    client = _SequenceClient(
        [
            _chat_gate_response(503, json_body={"error": {"code": "upstream_error"}}),
            _chat_gate_response(429, json_body={"error": {"code": "upstream_throttled"}}),
            _chat_gate_response(200, json_body={"message": "ok", "conversation_id": "c"}),
        ]
    )
    sleeps: list[float] = []
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    check = poll_chat_gate(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
    )

    assert check.passed
    assert len(client.calls) == 3
    assert sleeps == [5.0, 10.0]  # the schedule for attempts 1 and 2


def test_gate_retries_a_connection_error() -> None:
    client = _SequenceClient(
        [
            httpx.ConnectError(f"connection refused for {TENANT_ID}"),
            _chat_gate_response(200, json_body={"message": "ok"}),
        ]
    )
    sleeps: list[float] = []
    clock = iter([0.0, 1.0, 2.0])

    check = poll_chat_gate(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
    )

    assert check.passed
    assert sleeps == [5.0]
    # the exception message (which embeds the tenant) never reaches the check
    assert TENANT_ID not in check.detail


def test_gate_treats_401_as_terminal_and_does_not_retry() -> None:
    client = _SequenceClient(
        [
            _chat_gate_response(
                401,
                json_body={"error": {"code": "unauthorized", "message": "bad token"}},
                correlation_id="corr-401",
            )
        ]
    )
    sleeps: list[float] = []

    check = poll_chat_gate(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
    )

    assert not check.passed
    assert len(client.calls) == 1  # never retried
    assert sleeps == []
    assert "401" in check.detail
    assert "unauthorized" in check.detail
    assert "corr-401" in check.detail
    # the raw error message never reaches the check either
    assert "bad token" not in check.detail


def test_gate_treats_403_as_terminal_and_does_not_retry() -> None:
    client = _SequenceClient(
        [
            _chat_gate_response(
                403, json_body={"error": {"code": "insufficient_scope", "message": "no role"}}
            )
        ]
    )
    check = poll_chat_gate(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert not check.passed
    assert len(client.calls) == 1
    assert "403" in check.detail
    assert "insufficient_scope" in check.detail


def test_gate_a_200_with_an_error_envelope_is_not_success() -> None:
    """The brief's explicit example of what "authenticated 200" must exclude."""
    client = _SequenceClient(
        [_chat_gate_response(200, json_body={"error": {"code": "content_filtered"}})]
    )
    check = poll_chat_gate(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert not check.passed
    assert len(client.calls) == 1  # a 200 body defect is terminal, not retried
    assert "error" in check.detail


def test_gate_deadline_bounds_total_elapsed_time_not_attempt_count() -> None:
    """A short deadline that would allow several 5s/10s/20s retries by attempt
    count must still stop as soon as simulated elapsed time exceeds it."""
    client = _SequenceClient(
        [_chat_gate_response(503) for _ in range(10)]
    )
    sleeps: list[float] = []
    # Each poll advances the clock by 1s; the backoff schedule (5s, 10s, ...)
    # is what actually exhausts a 12s deadline, not the number of attempts.
    clock = iter([0.0, 1.0, 2.0, 12.0, 22.0, 42.0, 100.0])

    check = poll_chat_gate(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=12.0,
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
    )

    assert not check.passed
    assert "deadline" in check.detail
    assert "12" in check.detail
    # stopped well short of exhausting the 10-response sequence
    assert len(client.calls) < 10


def test_gate_exit_code_is_1_on_deadline_exceeded_and_0_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit-code contract `deploy-container-app.sh`'s `set -e` depends on,
    driven through `main()` with the real clock replaced so the test does not
    wait 1200 seconds."""
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(
        entra_smoke, "acquire_app_token", lambda *a, **k: unsigned_test_token({"roles": []})
    )

    clock = iter([0.0] + [100.0] * 20)
    monkeypatch.setattr(entra_smoke.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(entra_smoke.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_StubClient, "response", _chat_gate_response(503))
    monkeypatch.setattr(entra_smoke.httpx, "Client", _StubClient)

    exit_code = entra_smoke.main(
        [
            "--gate",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--deadline-seconds", "5",
        ]
    )
    assert exit_code == 1

    monkeypatch.setattr(
        _StubClient, "response", _chat_gate_response(200, json_body={"message": "ok"})
    )
    exit_code = entra_smoke.main(
        [
            "--gate",
            "--tenant-id", TENANT_ID,
            "--api-app-id", CLIENT_APP_ID,
            "--client-id", CLIENT_APP_ID,
            "--deadline-seconds", "5",
        ]
    )
    assert exit_code == 0


# ---------------------------------------------------------------------------
# --check-rag / --check-agent: additive checks the brief specifies.
# ---------------------------------------------------------------------------


class _RoutedClient:
    """Dispatches by URL path, so a single fake stands in for chat + rag."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        self.calls.append(url)
        return self._responses[url]


def test_check_rag_passes_on_200_with_non_empty_sources() -> None:
    client = _RoutedClient(
        {
            "/api/v1/chat": _chat_gate_response(200, json_body={"message": "ok"}),
            "/api/v1/rag": _chat_gate_response(
                200,
                json_body={
                    "status": "answered",
                    "answer": "30 days",
                    "sources": [{"doc_id": "returns-policy"}],
                    "correlation_id": "corr-rag",
                },
            ),
        }
    )
    checks = entra_smoke.run_gate_checks(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        check_rag=True,
        check_agent=False,
        rag_question="refund window?",
        agent_task="",
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert all(check.passed for check in checks)
    assert any("rag" in check.name for check in checks)


def test_check_rag_fails_on_empty_sources() -> None:
    """The `no_answer` shape: 200, but `sources` is an empty array."""
    client = _RoutedClient(
        {
            "/api/v1/chat": _chat_gate_response(200, json_body={"message": "ok"}),
            "/api/v1/rag": _chat_gate_response(
                200,
                json_body={
                    "status": "no_answer",
                    "answer": None,
                    "sources": [],
                    "correlation_id": "c",
                },
            ),
        }
    )
    checks = entra_smoke.run_gate_checks(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        check_rag=True,
        check_agent=False,
        rag_question="refund window?",
        agent_task="",
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    rag_check = next(check for check in checks if "rag" in check.name)
    assert not rag_check.passed
    assert "sources" in rag_check.detail


def test_check_agent_passes_on_200() -> None:
    client = _RoutedClient(
        {
            "/api/v1/chat": _chat_gate_response(200, json_body={"message": "ok"}),
            "/api/v1/agent": _chat_gate_response(
                200, json_body={"answer": "hi", "status": "completed", "correlation_id": "c"}
            ),
        }
    )
    checks = entra_smoke.run_gate_checks(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=1200.0,
        check_rag=False,
        check_agent=True,
        rag_question="",
        agent_task="say hi",
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert all(check.passed for check in checks)
    assert any("agent" in check.name for check in checks)


def test_check_rag_and_check_agent_are_not_evaluated_when_the_gate_fails() -> None:
    client = _RoutedClient({"/api/v1/chat": _chat_gate_response(503)})
    checks = entra_smoke.run_gate_checks(
        client,  # type: ignore[arg-type]
        "app-token",
        deadline_seconds=0.0,
        check_rag=True,
        check_agent=True,
        rag_question="refund window?",
        agent_task="say hi",
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert not checks[0].passed
    rag_check = next(check for check in checks if "rag" in check.name)
    agent_check = next(check for check in checks if "agent" in check.name)
    assert not rag_check.passed
    assert not agent_check.passed
    assert "not evaluated" in rag_check.detail
    assert "not evaluated" in agent_check.detail
    # neither downstream endpoint was ever called
    assert client.calls == ["/api/v1/chat"]


# ---------------------------------------------------------------------------
# CLI validation for the new flags.
# ---------------------------------------------------------------------------


def test_gate_and_phase_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        entra_smoke.main(
            [
                "--gate",
                "--phase", "no-role",
                "--tenant-id", TENANT_ID,
                "--api-app-id", CLIENT_APP_ID,
                "--client-id", CLIENT_APP_ID,
            ]
        )


def test_neither_gate_nor_phase_is_an_argument_error() -> None:
    with pytest.raises(SystemExit):
        entra_smoke.main(
            [
                "--tenant-id", TENANT_ID,
                "--api-app-id", CLIENT_APP_ID,
                "--client-id", CLIENT_APP_ID,
            ]
        )


def test_check_rag_without_gate_is_an_argument_error() -> None:
    with pytest.raises(SystemExit):
        entra_smoke.main(
            [
                "--phase", "no-role",
                "--tenant-id", TENANT_ID,
                "--api-app-id", CLIENT_APP_ID,
                "--client-id", CLIENT_APP_ID,
                "--check-rag",
            ]
        )
