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
identity_checks = entra_smoke.identity_checks
identity_log_check = entra_smoke.identity_log_check
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
        ],
    )
    for forbidden in (TENANT_ID, USER_ID, CLIENT_APP_ID[:8], CLIENT_SECRET, ACCESS_TOKEN):
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
