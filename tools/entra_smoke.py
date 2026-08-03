"""Day 19 live smoke: does the API accept real Microsoft Entra ID tokens, and
refuse the right ones?

Two phases, because the interesting proof needs two different tenant states and
a single run cannot hold both:

    # phase 1 — run right after create-entra-app.sh --defer-app-role-assignment
    ENTRA_CLIENT_SECRET=... uv run python tools/entra_smoke.py --phase no-role \
        --tenant-id <tenant> --api-app-id <api-app> --client-id <client-app>

    # phase 2 — run after assign-entra-app-role.sh
    ENTRA_CLIENT_SECRET=... uv run python tools/entra_smoke.py --phase full \
        --tenant-id <tenant> --api-app-id <api-app> --client-id <client-app> \
        --server-log server.log --evidence-out evidence.txt

`--phase no-role` proves the 403 path end to end. Its whole value rests on the
API service principal keeping `appRoleAssignmentRequired=false`: with that
setting the token endpoint happily issues a valid-audience token carrying no
`roles` claim, so the 403 comes from *this API* refusing a credential it
authenticated — not from Entra refusing to mint the credential in the first
place. Set it to `true` and this phase would fail at the token endpoint and
prove nothing about the server.

`--phase full` proves the 200 path (delegated and app-only), the two 401 paths
(an ID token, which has the wrong audience, and a string that is not a JWT at
all), and the identity log line each accepted request produced.

Both phases call `/api/v1/chat` against a server running the fake adapters
(`USE_FAKE_LLM=true` and friends), so Entra verification is the only live
dependency and the run costs nothing in model tokens.

Evidence discipline, in one rule with two halves:

  * claim KEY NAMES may be read at any time — a list of names carries no
    values, and `--evidence-out` records nothing else about a token;
  * claim VALUES (`tid`, `oid`, `roles`) are read only *after* the API has
    accepted that token with a 200. Decoding here is unverified and exists
    solely to compare against the server's own log line; it never decides
    access. `decode_claims_unverified` says so in its own docstring.

`--evidence-out` is written for a public repository: PASS/FAIL, check names,
redacted details, and sorted claim key names. No tenant id, no app ids, no
`oid`, no client secret, no access token, no ID token, no device `user_code`.
The short-lived `user_code` is the single value printed to the interactive
terminal, and it is printed before evidence collection begins.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

AUTHORITY_HOST = "https://login.microsoftonline.com"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# The two challenges Day 19 fixed at the API boundary (see
# `azgenai_lab.api.principal`). Compared verbatim, not parsed: the exact bytes
# are the contract a client library reads.
UNAUTHORIZED_CHALLENGE = "Bearer"
INSUFFICIENT_SCOPE_CHALLENGE = 'Bearer error="insufficient_scope"'

# The one INFO line `require_principal` emits per accepted request, and the
# fields the log-record factory stamps on every record.
IDENTITY_MESSAGE = "identity resolved"

# The server writes its log through a pipe (`... 2>&1 | tee server.log`), so a
# line can lag the HTTP response it belongs to by a moment. Bounded and
# retried rather than slept blindly — and a join that never appears is a
# failure, never a warning.
LOG_JOIN_ATTEMPTS = 10
LOG_JOIN_DELAY_SECONDS = 0.5

# Only used if the device authorization response omits the field, which the
# v2.0 endpoint does not do; RFC 8628 §3.2 makes `interval` optional with a
# recommended default of 5.
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_DEVICE_CODE_LIFETIME_SECONDS = 900


class SmokeError(RuntimeError):
    """A step that had to succeed for the run to mean anything did not.

    Carries an OAuth error code or an HTTP status — never a provider
    `error_description`, which routinely names the tenant and a trace id, and
    never a response body, which could be anything.
    """


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


# ---------------------------------------------------------------------------
# Redaction. Everything written to `--evidence-out` passes through here.
# ---------------------------------------------------------------------------

# Three patterns for the three value shapes that can reach a check detail. The
# details themselves are built from a bounded vocabulary (status codes,
# challenge headers, OAuth error codes), so this is the second layer rather
# than the only one — but a detail is the one channel fed by live material, so
# it gets a real masker rather than a promise.
_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# A JWT: three base64url segments, the first of which starts `ey` because its
# JSON header begins with `{"`. Access tokens, ID tokens and refresh tokens
# alike.
_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")
# An Entra client secret *value*: a ~40 character run containing a tilde. The
# tilde is what makes this precise enough to be safe — no word in this tool's
# own vocabulary ("authorization_pending", "insufficient_scope") contains one,
# so nothing legitimate is eaten.
_SECRET_RE = re.compile(r"[A-Za-z0-9._~-]*~[A-Za-z0-9._~-]{15,}")


def redact_sensitive(text: str) -> str:
    """Mask GUIDs, JWTs and secret-shaped runs, keeping the surrounding prose.

    Masking rather than dropping: evidence that says `<redacted-guid>` still
    tells a reader an identifier was there, which is the point of writing the
    detail down at all.
    """
    text = _JWT_RE.sub("<redacted-jwt>", text)
    text = _SECRET_RE.sub("<redacted-secret>", text)
    return _GUID_RE.sub("<redacted-guid>", text)


# ---------------------------------------------------------------------------
# Unverified claim reading — diagnostics only.
# ---------------------------------------------------------------------------


def decode_claims_unverified(token: str) -> dict[str, Any]:
    """Read a JWT's payload **without verifying anything**.

    Diagnostics only. Nothing in this tool — and nothing anywhere else in this
    repository — may decide access on the result: the server's own
    `EntraTokenVerifier` is the only thing that verifies a token, and the
    values read here are used solely to build the needle for a server-log line
    the API has *already* accepted.
    """
    segments = token.split(".")
    if len(segments) != 3:
        raise SmokeError("token is not a three-segment JWT")
    payload = segments[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise SmokeError("token payload is not base64url-encoded JSON") from exc
    if not isinstance(claims, dict):
        raise SmokeError("token payload is not a JSON object")
    return claims


def claim_keys(token: str) -> list[str]:
    """The sorted claim *names* in a token. Never the values."""
    return sorted(decode_claims_unverified(token))


def render_claim_inventory(flow: str, keys: Sequence[str]) -> str:
    inventory = ", ".join(sorted(keys)) if keys else "(none)"
    return f"{flow} token claim keys (names only, values never recorded): {inventory}"


def render_evidence(
    *,
    phase: str,
    checks: Sequence[Check],
    delegated_claim_keys: Sequence[str] = (),
    app_claim_keys: Sequence[str] = (),
) -> str:
    """The committed artifact: outcomes and claim key names, nothing else.

    Check names and details both go through `redact_sensitive`; the claim key
    sequences carry names only by construction.
    """
    passed = sum(1 for check in checks if check.passed)
    result = "PASS" if checks and passed == len(checks) else "FAIL"
    lines = [
        "# Entra ID live smoke evidence",
        f"phase: {phase}",
        f"result: {result}",
        f"checks: {passed}/{len(checks)} passed",
        "",
    ]
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        detail = redact_sensitive(check.detail)
        name = redact_sensitive(check.name)
        lines.append(f"{status}  {name}" + (f"  ({detail})" if detail else ""))
    if delegated_claim_keys:
        lines += ["", redact_sensitive(render_claim_inventory("delegated", delegated_claim_keys))]
    if app_claim_keys:
        if not delegated_claim_keys:
            lines.append("")
        lines.append(redact_sensitive(render_claim_inventory("app-only", app_claim_keys)))
    lines += [
        "",
        "Recorded above: check outcomes and claim key names only. Tenant ids, "
        "application ids, object ids, access tokens, ID tokens, the client "
        "secret and the device user code are never written to this file.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Response-contract checks.
# ---------------------------------------------------------------------------


def status_check(name: str, response: httpx.Response, expected: int) -> Check:
    ok = response.status_code == expected
    return Check(name, ok, "" if ok else f"expected {expected}, got {response.status_code}")


# The bounded vocabularies. A check detail may echo a provider- or
# server-supplied string ONLY when that string is a member of one of these
# sets; anything else is described, never quoted. This is what makes "details
# are built from a bounded vocabulary" a property of the code rather than a
# claim about it — `redact_sensitive` is then genuinely the second layer, not
# the only thing standing between an opaque refresh token or a `user_code`
# (neither GUID-, JWT- nor tilde-shaped) and a committed file.

# Every `error.code` this API is documented to emit (`core/errors.py` plus the
# handlers' literals). A code outside the set means something that is not this
# API answered, which is worth reporting but not worth quoting.
API_ERROR_CODES = frozenset(
    {
        "configuration_error",
        "content_filtered",
        "conversation_not_found",
        "document_too_large",
        "duplicate_chunk_id",
        "embedding_rejected",
        "enumeration_failed",
        "http_error",
        "insufficient_scope",
        "invalid_input",
        "rag_context_overflow",
        "search_request_rejected",
        "search_unavailable",
        "storage_error",
        "token_budget_exceeded",
        "unauthorized",
        "unsendable_document",
        "upstream_error",
        "upstream_throttled",
        "upstream_timeout",
        "validation_error",
    }
)

# The media types this tool expects to meet in front of, or instead of, the
# API. Naming the type is the whole diagnostic value here ("a proxy answered
# with HTML"), and the set keeps that from becoming a free-text channel.
KNOWN_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/problem+json",
        "application/xml",
        "text/html",
        "text/plain",
    }
)

# RFC 6749 §5.2 plus RFC 8628 §3.5, and the two Entra adds this flow meets.
OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "authorization_declined",
        "authorization_pending",
        "bad_verification_code",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "invalid_target",
        "server_error",
        "slow_down",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)


def describe(value: str | None, known: frozenset[str], noun: str) -> str:
    """Quote `value` only if it is a member of `known`; otherwise describe it.

    The one gate every provider- and server-supplied string passes through
    before it can reach a check detail.
    """
    if value is None:
        return f"no {noun}"
    if value in known:
        return repr(value)
    return f"an unrecognized {noun}"


def _error_code(response: httpx.Response) -> tuple[str | None, str]:
    """The envelope's `error.code`, plus why it could not be read.

    The response body is never quoted back: an HTML error page from something
    in front of the API is a realistic failure mode, and it could contain the
    bearer token that produced it. The media type is named only when it is one
    this tool knows.
    """
    media_type = response.headers.get("content-type", "").split(";")[0].strip()
    if media_type != "application/json":
        described = describe(media_type or None, KNOWN_MEDIA_TYPES, "media type")
        return None, f"non-JSON response (content-type {described})"
    try:
        body = response.json()
    except ValueError:
        return None, "response body was not valid JSON"
    if not isinstance(body, dict):
        return None, "response body was not a JSON object"
    error = body.get("error")
    if not isinstance(error, dict):
        return None, "envelope has no `error` object"
    code = error.get("code")
    if not isinstance(code, str):
        return None, "`error.code` is missing or not a string"
    return code, ""


def error_code_check(name: str, response: httpx.Response, expected: str) -> Check:
    code, problem = _error_code(response)
    if code is None:
        return Check(name, False, problem)
    if code == expected:
        return Check(name, True, "")
    described = describe(code, API_ERROR_CODES, "error code")
    return Check(name, False, f"expected {expected!r}, got {described}")


def challenge_check(name: str, response: httpx.Response, expected: str) -> Check:
    """Compared, never echoed.

    RFC 6750 §3 lets a challenge carry an `error_description`, so the header
    an unknown intermediary sets is arbitrary text. Since the check is an
    equality test, the received value adds nothing the comparison has not
    already reported.
    """
    actual = response.headers.get("www-authenticate")
    if actual == expected:
        return Check(name, True, "")
    problem = "no WWW-Authenticate header" if actual is None else "the challenge differed"
    return Check(name, False, f"expected {expected!r}; {problem}")


def rejection_checks(
    label: str, response: httpx.Response, *, status: int, code: str, challenge: str
) -> list[Check]:
    """The three independent halves of a rejection contract.

    Separate checks rather than one combined verdict: a 401 where 403 is
    required, a right status with the wrong code, and a right code with the
    wrong challenge are three different defects, and collapsing them would
    make a run report one.
    """
    return [
        status_check(f"{label}: status {status}", response, status),
        error_code_check(f"{label}: error code", response, code),
        challenge_check(f"{label}: WWW-Authenticate challenge", response, challenge),
    ]


# ---------------------------------------------------------------------------
# The server-log join.
# ---------------------------------------------------------------------------


def identity_log_check(
    label: str,
    *,
    lines: Iterable[str],
    correlation_id: str,
    tenant_id: str,
    user_id: str,
) -> Check:
    """One log line carrying all three ids *and* the identity message.

    All four on the same line, because the claim is about a single request.
    Fields are matched as whole whitespace-delimited `key=value` tokens, not as
    substrings: `tenant_id=<guid>` is a prefix of `tenant_id=<guid>-shadow`,
    and a substring test would accept a line about a different tenant. The
    record factory stamps the three fields on *every* record, so the message
    is what says the resolver ran.
    """
    wanted = {
        f"correlation_id={correlation_id}",
        f"tenant_id={tenant_id}",
        f"user_id={user_id}",
    }
    for line in lines:
        if wanted <= set(line.split()) and IDENTITY_MESSAGE in line:
            return Check(f"{label}: identity log line", True, "")
    return Check(
        f"{label}: identity log line",
        False,
        "no log line carried this request's correlation id, tenant id, user id "
        f"and {IDENTITY_MESSAGE!r}",
    )


def identity_checks(
    label: str, *, token: str, lines: Iterable[str], correlation_id: str
) -> list[Check]:
    """Always two checks: the claims are readable, and the log line joins.

    Never one. A token with no `tid`/`oid` leaves nothing to join on, and
    silently dropping the join would turn "we could not verify this" into a
    shorter, all-green check list.
    """
    claim_name = f"{label}: token carries tid and oid"
    try:
        claims = decode_claims_unverified(token)
    except SmokeError as exc:
        return [
            Check(claim_name, False, str(exc)),
            Check(f"{label}: identity log line", False, "no ids to join on"),
        ]
    tenant_id = claims.get("tid")
    user_id = claims.get("oid")
    if not isinstance(tenant_id, str) or not isinstance(user_id, str):
        return [
            Check(claim_name, False, "`tid` or `oid` is missing or not a string"),
            Check(f"{label}: identity log line", False, "no ids to join on"),
        ]
    return [
        Check(claim_name, True, ""),
        identity_log_check(
            label,
            lines=lines,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            user_id=user_id,
        ),
    ]


# ---------------------------------------------------------------------------
# App-role claim checks.
# ---------------------------------------------------------------------------


def role_present_check(label: str, *, token: str, role: str) -> Check:
    """The configured app role is one entry of the `roles` array.

    Membership of the parsed array, not a substring of the joined text:
    `Api.Access.Extended` is a different role.
    """
    name = f"{label}: roles claim contains the configured role"
    try:
        claims = decode_claims_unverified(token)
    except SmokeError as exc:
        return Check(name, False, str(exc))
    roles = claims.get("roles")
    if not isinstance(roles, list) or any(not isinstance(entry, str) for entry in roles):
        return Check(name, False, "`roles` is missing or is not an array of strings")
    return Check(name, role in roles, "" if role in roles else "configured role not in `roles`")


def role_absent_check(label: str, *, token: str) -> Check:
    """No `roles` claim at all — the key, not its contents.

    An empty array is the claim still being emitted, which is a different
    tenant state from the one this phase exists to observe.
    """
    name = f"{label}: no roles claim"
    try:
        keys = claim_keys(token)
    except SmokeError as exc:
        return Check(name, False, str(exc))
    return Check(name, "roles" not in keys, "" if "roles" not in keys else "`roles` claim present")


# ---------------------------------------------------------------------------
# Raw OAuth. Sync `httpx.Client`: this is a standalone CLI, not the server.
# ---------------------------------------------------------------------------


def _authority(tenant_id: str) -> str:
    return f"{AUTHORITY_HOST}/{tenant_id}"


def token_url(tenant_id: str) -> str:
    return f"{_authority(tenant_id)}/oauth2/v2.0/token"


def _oauth_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) else None


def _oauth_failure(context: str, response: httpx.Response) -> SmokeError:
    """A SmokeError naming the OAuth error code and status, and nothing else.

    Microsoft's `error_description` is genuinely useful when a run fails, and
    it also names the tenant and a trace id — so it goes to the terminal with
    the same warning the ephemeral-resource scripts print for a secret, and
    never into the exception message that a check detail could inherit.
    """
    try:
        payload = response.json()
    except ValueError:
        return SmokeError(f"{context}: HTTP {response.status_code} with a non-JSON body")
    if not isinstance(payload, dict):
        return SmokeError(f"{context}: HTTP {response.status_code} with an unexpected JSON body")
    description = payload.get("error_description")
    if isinstance(description, str) and description:
        print(
            "Provider diagnostics (terminal only — names the tenant and a "
            f"trace id; never paste into evidence files or committed text):\n  {description}",
            file=sys.stderr,
        )
    described = describe(_oauth_error_code(response), OAUTH_ERROR_CODES, "OAuth error")
    return SmokeError(f"{context}: {described} (HTTP {response.status_code})")


def _require_str(payload: dict[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SmokeError(f"{context}: response has no `{key}`")
    return value


def _require_int(payload: dict[str, Any], key: str, context: str, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SmokeError(f"{context}: `{key}` is not an integer")
    return value


def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SmokeError(f"{context}: response body was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SmokeError(f"{context}: response body was not a JSON object")
    return payload


def request_device_code(
    client: httpx.Client, *, tenant_id: str, client_id: str, api_app_id: str, scope: str
) -> dict[str, Any]:
    context = "device authorization request"
    response = client.post(
        f"{_authority(tenant_id)}/oauth2/v2.0/devicecode",
        data={
            "client_id": client_id,
            "scope": f"openid profile api://{api_app_id}/{scope}",
        },
    )
    if response.status_code != 200:
        raise _oauth_failure(context, response)
    payload = _json_object(response, context)
    _require_str(payload, "device_code", context)
    _require_str(payload, "user_code", context)
    _require_str(payload, "verification_uri", context)
    # Validated here, with the rest of the response shape, rather than at the
    # call site: the caller prints the sign-in prompt before it starts polling,
    # and a malformed field discovered after that costs the operator a
    # completed interactive sign-in for nothing. The v2.0 endpoint returns both
    # as JSON numbers (it was the v1 endpoint that returned strings, and this
    # tool never calls it), so a non-integer here is a real surprise.
    _require_int(payload, "interval", context, DEFAULT_POLL_INTERVAL_SECONDS)
    _require_int(payload, "expires_in", context, DEFAULT_DEVICE_CODE_LIFETIME_SECONDS)
    return payload


def poll_device_token(
    client: httpx.Client,
    *,
    token_url: str,
    client_id: str,
    device_code: str,
    interval: float,
    expires_in: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    """Poll until the user finishes signing in, or the device code expires.

    `slow_down` raises the interval **permanently**. RFC 8628 §3.5 defines it
    as "a variant of `authorization_pending` ... the interval MUST be increased
    by 5 seconds for this and all subsequent requests" — so the raised value is
    carried forward for the rest of the session, not applied to one poll and
    then forgotten.

    Prints nothing: the token this returns must not reach the terminal.
    """
    deadline = monotonic() + expires_in
    current_interval = interval
    form = {
        "grant_type": DEVICE_CODE_GRANT,
        "client_id": client_id,
        "device_code": device_code,
    }
    while True:
        if monotonic() >= deadline:
            raise SmokeError("device code expired before authorization completed")
        response = client.post(token_url, data=form)
        if response.status_code == 200:
            return _json_object(response, "device code token request")
        error = _oauth_error_code(response)
        if error == "slow_down":
            current_interval += 5
        elif error != "authorization_pending":
            raise _oauth_failure("device code token request", response)
        sleep(current_interval)


def acquire_app_token(
    client: httpx.Client,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    api_app_id: str,
) -> str:
    """One client-credentials token for the API's `.default` scope.

    Before the app-role assignment exists this still succeeds — the API
    service principal keeps `appRoleAssignmentRequired=false` — and the token
    it returns carries no `roles` claim. That is precisely the credential
    `--phase no-role` needs the API itself to refuse.
    """
    context = "client credentials request"
    response = client.post(
        token_url(tenant_id),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"api://{api_app_id}/.default",
        },
    )
    if response.status_code != 200:
        raise _oauth_failure(context, response)
    return _require_str(_json_object(response, context), "access_token", context)


# ---------------------------------------------------------------------------
# The API under test.
# ---------------------------------------------------------------------------


def post_chat(client: httpx.Client, token: str) -> httpx.Response:
    return client.post(
        "/api/v1/chat",
        json={"message": "entra live smoke"},
        headers={"Authorization": f"Bearer {token}"},
    )


def response_correlation_id(response: httpx.Response) -> str:
    correlation_id = response.headers.get("x-correlation-id", "")
    return correlation_id if isinstance(correlation_id, str) else ""


def read_log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def accepted_token_checks(
    label: str,
    *,
    token: str,
    response: httpx.Response,
    log_path: Path,
    role: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Check]:
    """Claim VALUES and the log join, for a token the API accepted.

    When it did not, this contributes one explicitly FAILED check rather than
    quietly nothing: an evidence file whose check list silently got shorter is
    the shape a reader mistakes for a smaller run, and "not evaluated" is a
    result, not an absence.
    """
    if response.status_code != 200:
        return [
            Check(
                f"{label}: claim values and identity log line",
                False,
                "not evaluated: the API did not accept this token, and claim "
                "values are read only from a token it accepted",
            )
        ]
    checks = []
    if role is not None:
        checks.append(role_present_check(label, token=token, role=role))
    checks.extend(
        identity_checks_with_retry(
            label,
            token=token,
            correlation_id=response_correlation_id(response),
            log_path=log_path,
            sleep=sleep,
        )
    )
    return checks


def identity_checks_with_retry(
    label: str,
    *,
    token: str,
    correlation_id: str,
    log_path: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Check]:
    """`identity_checks`, retried while the log **line** is still in flight.

    The server's stdout reaches the file through a pipe, so a line can arrive
    a moment after the response did. Only that half is retried: a token with no
    `tid`/`oid` is a fact about the token, and no amount of waiting changes it,
    so retrying there would spend the whole budget sleeping over a verdict
    already reached. Bounded either way — when the attempts run out the last
    (failing) result is what gets reported, never a warning.
    """
    checks = identity_checks(
        label, token=token, lines=read_log_lines(log_path), correlation_id=correlation_id
    )
    claim_check = checks[0]
    if not claim_check.passed:
        return checks
    for _attempt in range(LOG_JOIN_ATTEMPTS - 1):
        if all(check.passed for check in checks):
            return checks
        sleep(LOG_JOIN_DELAY_SECONDS)
        checks = identity_checks(
            label, token=token, lines=read_log_lines(log_path), correlation_id=correlation_id
        )
    return checks


# ---------------------------------------------------------------------------
# Phases.
# ---------------------------------------------------------------------------


@dataclass
class Config:
    phase: str
    base_url: str
    tenant_id: str
    api_app_id: str
    client_id: str
    scope: str
    app_role: str
    server_log: Path | None
    evidence_out: Path | None
    timeout: float


@dataclass
class PhaseState:
    """Accumulated as the run proceeds, so a mid-run failure still reports
    every check that had already been decided."""

    checks: list[Check] = field(default_factory=list)
    delegated_claim_keys: list[str] = field(default_factory=list)
    app_claim_keys: list[str] = field(default_factory=list)


def run_no_role_phase(config: Config, client_secret: str, state: PhaseState) -> None:
    with httpx.Client(timeout=config.timeout) as oauth:
        token = acquire_app_token(
            oauth,
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=client_secret,
            api_app_id=config.api_app_id,
        )
    # Key names only, so this is allowed before the API has seen the token —
    # and it has to be, because the API is about to reject it.
    state.app_claim_keys = claim_keys(token)
    state.checks.append(role_absent_check("app-only before assignment", token=token))

    with httpx.Client(base_url=config.base_url, timeout=config.timeout) as api:
        response = post_chat(api, token)
    state.checks.extend(
        rejection_checks(
            "app-only without role",
            response,
            status=403,
            code="insufficient_scope",
            challenge=INSUFFICIENT_SCOPE_CHALLENGE,
        )
    )


def run_full_phase(config: Config, client_secret: str, state: PhaseState) -> None:
    log_path = config.server_log
    if log_path is None:  # pragma: no cover - argument parsing rejects this first
        raise SmokeError("--phase full requires --server-log")

    with httpx.Client(timeout=config.timeout) as oauth:
        device = request_device_code(
            oauth,
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            api_app_id=config.api_app_id,
            scope=config.scope,
        )
        # Terminal only, and before any evidence is collected: the user code is
        # short-lived and worthless once the flow completes.
        print(f"\nOpen {device['verification_uri']} and enter code: {device['user_code']}")
        print("Waiting for authorization (nothing is recorded until it completes)...\n")
        tokens = poll_device_token(
            oauth,
            token_url=token_url(config.tenant_id),
            client_id=config.client_id,
            device_code=str(device["device_code"]),
            interval=_require_int(device, "interval", "device authorization request", 5),
            expires_in=_require_int(device, "expires_in", "device authorization request", 900),
            sleep=time.sleep,
            monotonic=time.monotonic,
        )
        delegated = _require_str(tokens, "access_token", "device code token request")
        id_token = _require_str(tokens, "id_token", "device code token request")
        app_token = acquire_app_token(
            oauth,
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=client_secret,
            api_app_id=config.api_app_id,
        )

    # Key names, so this is allowed for every token regardless of outcome —
    # and it means the evidence carries both inventories even on a failed run.
    state.delegated_claim_keys = claim_keys(delegated)
    state.app_claim_keys = claim_keys(app_token)

    with httpx.Client(base_url=config.base_url, timeout=config.timeout) as api:
        delegated_response = post_chat(api, delegated)
        app_response = post_chat(api, app_token)
        id_token_response = post_chat(api, id_token)
        not_a_jwt_response = post_chat(api, "not-a-jwt")

    state.checks.append(status_check("delegated: status 200", delegated_response, 200))
    state.checks.append(status_check("app-only: status 200", app_response, 200))

    state.checks.extend(
        accepted_token_checks(
            "delegated", token=delegated, response=delegated_response, log_path=log_path
        )
    )
    state.checks.extend(
        accepted_token_checks(
            "app-only",
            token=app_token,
            response=app_response,
            log_path=log_path,
            role=config.app_role,
        )
    )

    state.checks.extend(
        rejection_checks(
            "id token (wrong audience)",
            id_token_response,
            status=401,
            code="unauthorized",
            challenge=UNAUTHORIZED_CHALLENGE,
        )
    )
    state.checks.extend(
        rejection_checks(
            "not a jwt",
            not_a_jwt_response,
            status=401,
            code="unauthorized",
            challenge=UNAUTHORIZED_CHALLENGE,
        )
    )


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIGURATION = 2


def _parse_args(argv: Sequence[str] | None) -> Config:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--phase", choices=("no-role", "full"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--api-app-id", required=True, help="the API application's client id")
    parser.add_argument("--client-id", required=True, help="the client application's client id")
    parser.add_argument("--scope", default="access_as_user")
    parser.add_argument("--app-role", default="Api.Access")
    parser.add_argument(
        "--server-log",
        type=Path,
        help="the file the server's log is tee'd to; required for --phase full",
    )
    parser.add_argument("--evidence-out", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.phase == "full" and args.server_log is None:
        parser.error("--phase full requires --server-log (the identity log line is asserted)")
    return Config(
        phase=args.phase,
        base_url=args.base_url,
        tenant_id=args.tenant_id,
        api_app_id=args.api_app_id,
        client_id=args.client_id,
        scope=args.scope,
        app_role=args.app_role,
        server_log=args.server_log,
        evidence_out=args.evidence_out,
        timeout=args.timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = _parse_args(argv)

    # Never a command-line argument: `ps` shows another user the whole argv of
    # a running process, and shell history keeps it afterwards.
    client_secret = os.environ.get("ENTRA_CLIENT_SECRET", "")
    if not client_secret:
        print("Set ENTRA_CLIENT_SECRET (the client application's secret).", file=sys.stderr)
        return EXIT_CONFIGURATION
    if config.server_log is not None and not config.server_log.is_file():
        # Checked before the interactive device flow, not after it: failing
        # here costs nothing, failing later costs the operator the whole sign-in.
        print(f"Server log {config.server_log} does not exist.", file=sys.stderr)
        return EXIT_CONFIGURATION

    state = PhaseState()
    try:
        if config.phase == "no-role":
            run_no_role_phase(config, client_secret, state)
        else:
            run_full_phase(config, client_secret, state)
    except SmokeError as exc:
        state.checks.append(Check(f"{config.phase}: prerequisite step", False, str(exc)))
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        # The transport and the filesystem, not just this tool's own errors. A
        # server that is not running, a mistyped `--base-url` and a log file
        # rotated mid-run are the three likeliest operator mistakes, and in
        # `--phase full` they land AFTER the interactive sign-in — the exact
        # cost the log-file precheck above exists to avoid paying twice. An
        # escaping traceback would also print the token URL, and with it the
        # tenant GUID, to stderr unredacted.
        #
        # Only the exception CLASS is recorded: httpx messages embed the
        # request URL, so quoting one would put the tenant back in the
        # evidence through a different door.
        state.checks.append(
            Check(
                f"{config.phase}: prerequisite step",
                False,
                f"{type(exc).__name__} while contacting the token endpoint, the API, "
                "or the server log",
            )
        )

    evidence = render_evidence(
        phase=config.phase,
        checks=state.checks,
        delegated_claim_keys=state.delegated_claim_keys,
        app_claim_keys=state.app_claim_keys,
    )
    print(evidence)
    if config.evidence_out is not None:
        config.evidence_out.write_text(evidence, encoding="utf-8")
        print(f"evidence written to {config.evidence_out}")

    return EXIT_PASS if state.checks and all(check.passed for check in state.checks) else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
