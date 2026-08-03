"""Cryptographic verification of Entra access tokens, offline.

Every key in this file is generated locally and every HTTP response is served
by `httpx.MockTransport`: the suite never reaches `login.microsoftonline.com`,
so a broken network, a rotated tenant key or a provider outage cannot turn into
a red run that says nothing about this code.
"""

import ast
import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from azgenai_lab.services import entra_jwt
from azgenai_lab.services.entra_jwt import (
    EntraTokenVerifier,
    TokenInvalidError,
    TokenVerifierStartupError,
)

TENANT_ID = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"

DISCOVERY_BODY: dict[str, object] = {"issuer": ISSUER, "jwks_uri": JWKS_URL}


def signing_material(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig"})
    return private_key, jwk


def access_token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str,
    overrides: dict[str, object] | None = None,
    drop: set[str] | None = None,
    algorithm: str = "RS256",
) -> str:
    """Mint a token. `overrides` replaces claims; `drop` removes them entirely.

    The two are not interchangeable. `{"exp": None}` is a *present-but-null*
    claim, which PyJWT rejects on its own; `drop={"exp"}` produces a token with
    no `exp` at all, which PyJWT accepts as never-expiring unless the `require`
    option says otherwise. Only the second shape can tell us whether that
    option is doing anything.
    """
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "tid": TENANT_ID,
        "oid": "33333333-3333-3333-3333-333333333333",
        "scp": "access_as_user",
    }
    claims.update(overrides or {})
    for name in drop or set():
        del claims[name]
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


def mock_transport(jwks: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DISCOVERY_URL:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=jwks)
        raise AssertionError(f"unexpected URL: {request.url}")

    return httpx.MockTransport(handler)


# --- extra fixtures and helpers -------------------------------------------------

# A response is described as (status, body); a `bytes` body is sent raw so a
# non-JSON payload can be exercised, anything else is serialized as JSON.
ResponseSpec = tuple[int, object]

VALID_DISCOVERY: ResponseSpec = (200, DISCOVERY_BODY)


def _response(spec: ResponseSpec) -> httpx.Response:
    status, body = spec
    if isinstance(body, bytes):
        return httpx.Response(status, content=body)
    return httpx.Response(status, json=body)


def scripted_transport(
    *,
    discovery: ResponseSpec = VALID_DISCOVERY,
    jwks: ResponseSpec = (200, {"keys": []}),
    calls: list[str] | None = None,
) -> httpx.MockTransport:
    """`mock_transport` with per-endpoint control, plus an optional call log.

    The call log is how "this was rejected without a network request" is
    asserted: it is the only externally visible difference between a check that
    ran before key lookup and one that ran after.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if str(request.url) == DISCOVERY_URL:
            return _response(discovery)
        if str(request.url) == JWKS_URL:
            return _response(jwks)
        raise AssertionError(f"unexpected URL: {request.url}")

    return httpx.MockTransport(handler)


async def startup_error(transport: httpx.MockTransport) -> TokenVerifierStartupError:
    """Initialize against `transport` and return the startup failure it raises."""
    client = httpx.AsyncClient(transport=transport)
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        with pytest.raises(TokenVerifierStartupError) as caught:
            await verifier.initialize()
    finally:
        await client.aclose()
    return caught.value


@pytest.fixture
async def ready() -> AsyncIterator[tuple[rsa.RSAPrivateKey, EntraTokenVerifier]]:
    """An initialized verifier trusting exactly one key, `kid-1`."""
    private_key, jwk = signing_material("kid-1")
    client = httpx.AsyncClient(transport=mock_transport({"keys": [jwk]}))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    await verifier.initialize()
    try:
        yield private_key, verifier
    finally:
        await client.aclose()


# --- happy path -----------------------------------------------------------------


async def test_initializes_and_verifies_valid_rs256_token() -> None:
    private_key, jwk = signing_material("kid-1")
    client = httpx.AsyncClient(transport=mock_transport({"keys": [jwk]}))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        claims = await verifier.verify(access_token(private_key, kid="kid-1"))
    finally:
        await client.aclose()
    assert claims["tid"] == TENANT_ID
    assert claims["aud"] == AUDIENCE


async def test_verify_returns_the_unregistered_claims_untouched(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
) -> None:
    """The verifier hands the claims on; it does not filter or rename them."""
    private_key, verifier = ready
    claims = await verifier.verify(access_token(private_key, kid="kid-1"))
    assert claims["oid"] == "33333333-3333-3333-3333-333333333333"
    assert claims["scp"] == "access_as_user"


async def test_verify_accepts_a_foreign_tid(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
) -> None:
    """`tid` is deliberately not checked here.

    The tenant comparison has exactly one owner — the resolver, which is also
    the only layer that needs `tid` at all (it builds `Principal.tenant_id`
    from it). A duplicated check would merely be redundant; two layers each
    assuming the other did it is a tenant bypass. The tenant is still pinned
    at this layer by the exact `iss` match, because the tenant-specific issuer
    URL embeds the tenant GUID — which is why a token signed by *this*
    tenant's key with a mismatched `tid` gets through here and is stopped one
    layer up.
    """
    private_key, verifier = ready
    foreign = "99999999-9999-9999-9999-999999999999"
    claims = await verifier.verify(
        access_token(private_key, kid="kid-1", overrides={"tid": foreign})
    )
    assert claims["tid"] == foreign


# --- per-request rejection matrix -----------------------------------------------


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        # Well past the 60-second skew allowance, so this fails on being
        # expired rather than on the leeway boundary.
        ("expired", {"exp": datetime.now(UTC) - timedelta(minutes=5)}),
        ("wrong_audience", {"aud": "44444444-4444-4444-4444-444444444444"}),
        ("wrong_issuer", {"iss": "https://login.microsoftonline.com/other/v2.0"}),
        # A present-but-null `exp`. A real input, but not a *missing* claim:
        # PyJWT rejects it either way (with `require`, as a missing claim;
        # without, as a TypeError from int(None)), so this case says nothing
        # about the `require` option. The absent-claim tests below are the
        # ones that do.
        ("null_exp", {"exp": None}),
    ],
)
async def test_verify_rejects_claim_mutations(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
    case: str,
    overrides: dict[str, object],
) -> None:
    private_key, verifier = ready
    token = access_token(private_key, kid="kid-1", overrides=overrides)
    with pytest.raises(TokenInvalidError):
        await verifier.verify(token)


@pytest.mark.parametrize("claim", ["exp", "iss", "aud"])
async def test_verify_rejects_a_token_missing_a_required_claim(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier], claim: str
) -> None:
    """A claim that is absent, not null — PyJWT only validates what exists.

    `exp` is the case that carries the `require` option on its own: drop the
    option and a token with no `exp` at all is **accepted** as never-expiring
    (verified against PyJWT 2.13.0), which is the whole reason the option is
    passed. `iss` and `aud` would still be rejected without it, because the
    `issuer=`/`audience=` arguments make PyJWT demand them — so those two pin
    the contract rather than the option. Both are worth holding: the contract
    is what callers depend on, whichever mechanism enforces it.
    """
    private_key, verifier = ready
    token = access_token(private_key, kid="kid-1", drop={claim})
    with pytest.raises(TokenInvalidError):
        await verifier.verify(token)


async def test_verify_rejects_a_token_signed_by_another_key(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
) -> None:
    """Right `kid`, wrong signer: the claims are perfect and the token is not."""
    _, verifier = ready
    impostor, _ = signing_material("kid-1")
    with pytest.raises(TokenInvalidError):
        await verifier.verify(access_token(impostor, kid="kid-1"))


async def test_verify_rejects_an_unknown_kid() -> None:
    """An unknown `kid` is rejected from the cache alone — no refetch here.

    Not because the cache is static (it is not, since the refresh path
    landed), but because the cooldown starts at the startup fetch and this
    verify happens immediately after it. That is the same property
    `test_unknown_kid_inside_cooldown_does_not_refresh` states directly with a
    manual clock; kept here because the call count is what pins that a
    rejection on this path is a rejection, not a rejection plus an outbound
    request.
    """
    private_key, jwk = signing_material("kid-1")
    calls: list[str] = []
    client = httpx.AsyncClient(
        transport=scripted_transport(jwks=(200, {"keys": [jwk]}), calls=calls)
    )
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        assert calls == [DISCOVERY_URL, JWKS_URL]
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(private_key, kid="kid-unknown"))
    finally:
        await client.aclose()
    assert calls == [DISCOVERY_URL, JWKS_URL]


async def test_verify_rejects_alg_none(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
) -> None:
    """Rejected at the header screen — though not *only* there.

    `algorithms=["RS256"]` would refuse this token anyway, so deleting the
    header screen would not turn this test red, and the arrival of the refresh
    path did not change that: the `kid` here is one we publish, so the lookup
    hits and the request ends before any refresh could be triggered no matter
    what order the checks run in.

    The ordering *is* load-bearing now — an unsigned token must not be able to
    drive JWKS traffic — but the test that proves it needs an unknown `kid`
    and an expired cooldown, which is
    `test_a_forged_algorithm_with_an_unknown_kid_drives_no_jwks_traffic`. This
    stays a contract test: `alg: none` is rejected, full stop.
    """
    _, verifier = ready
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        None,  # type: ignore[arg-type]
        algorithm="none",
        headers={"kid": "kid-1"},
    )
    with pytest.raises(TokenInvalidError):
        await verifier.verify(unsigned)


async def test_verify_rejects_hs256_before_touching_the_key_cache() -> None:
    """Algorithm confusion is stopped at the header, ahead of any key work.

    An HS256 token naming a `kid` we publish is the classic attack: if the
    algorithm were taken from the token, the RSA public key would be used as
    an HMAC secret and a token anyone can mint would verify. That part is
    real, and `algorithms=["RS256"]` backs it up independently.

    The `calls == []` assertion is still weaker than it looks. `verify()` can
    now reach the network, but not on this token's path: the forged header
    names `kid-1`, which is in the cache, so the lookup hits and no refresh is
    reachable whatever the ordering. The clause pins that a *hit* costs no
    request — worth holding, and not the anti-amplification proof it was
    expected to grow into.

    That proof needs a `kid` the cache does not hold, which is
    `test_a_forged_algorithm_with_an_unknown_kid_drives_no_jwks_traffic`.
    """
    _, jwk = signing_material("kid-1")
    calls: list[str] = []
    client = httpx.AsyncClient(
        transport=scripted_transport(jwks=(200, {"keys": [jwk]}), calls=calls)
    )
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        calls.clear()
        forged = access_token(
            secrets.token_bytes(32),  # type: ignore[arg-type]
            kid="kid-1",
            algorithm="HS256",
        )
        with pytest.raises(TokenInvalidError):
            await verifier.verify(forged)
    finally:
        await client.aclose()
    assert calls == []


@pytest.mark.parametrize(
    "token",
    ["", "not-a-jwt", "a.b", "a.b.c", "eyJhbGciOiJSUzI1NiJ9.payload"],
)
async def test_verify_rejects_malformed_tokens(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier], token: str
) -> None:
    _, verifier = ready
    with pytest.raises(TokenInvalidError):
        await verifier.verify(token)


async def test_verify_rejects_a_header_without_a_kid(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
) -> None:
    """A contract test, not a proof that the explicit check is doing the work.

    `self._keys.get(None)` misses regardless, so removing the `kid` screen
    leaves this green — including now that a miss can cost a refresh, because
    a missing `kid` reaches `_refresh_keys(None)`, and `None` there means the
    cache-age trigger, which declines on a fresh cache. So the screen is not
    an anti-amplification control; what it prevents is subtler and worth
    keeping: a token with *no* key id being silently spelled as a refresh for
    *no particular* key id, conflating two unrelated meanings of the same
    argument. Type checking catches that too. This holds the contract.
    """
    private_key, verifier = ready
    claims = {"iss": ISSUER, "aud": AUDIENCE, "exp": datetime.now(UTC) + timedelta(minutes=5)}
    token = jwt.encode(claims, private_key, algorithm="RS256")
    with pytest.raises(TokenInvalidError):
        await verifier.verify(token)


async def test_rejection_messages_carry_no_token_or_claim_text(
    ready: tuple[rsa.RSAPrivateKey, EntraTokenVerifier],
) -> None:
    """These messages reach a log, and sometimes a response body.

    Echoing the token would put a live credential in the log; echoing claim
    values would let an unauthenticated caller use the error text to map the
    tenant. Both are checked here rather than left to review.
    """
    private_key, verifier = ready
    token = access_token(
        private_key, kid="kid-1", overrides={"aud": "44444444-4444-4444-4444-444444444444"}
    )
    with pytest.raises(TokenInvalidError) as caught:
        await verifier.verify(token)
    message = str(caught.value)
    assert token not in message
    for secret_ish in (AUDIENCE, "44444444-4444-4444-4444-444444444444", "kid-1"):
        assert secret_ish not in message


# --- startup failure matrix -----------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_startup_fails_on_discovery_http_error(status: int) -> None:
    await startup_error(scripted_transport(discovery=(status, {"error": "nope"})))


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_startup_fails_on_jwks_http_error(status: int) -> None:
    await startup_error(scripted_transport(jwks=(status, {"error": "nope"})))


async def test_startup_does_not_follow_a_redirect_away_from_the_pinned_host() -> None:
    """Host pinning is only worth as much as the redirect policy behind it.

    A 3xx is the one way a URL we validated could still deliver a response
    from somewhere we did not. httpx does not follow redirects unless asked,
    and this pins that default rather than trusting it to stay put.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/keys"})

    error = await startup_error(httpx.MockTransport(handler))
    # httpx's raise_for_status treats a 3xx as a failure, so the startup
    # message names the status rather than misreporting an empty body.
    assert "HTTPStatusError" in str(error)
    assert requested == [DISCOVERY_URL]


async def test_startup_fails_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    await startup_error(httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "body",
    [
        b"<html>sign in</html>",  # a captive portal or proxy error page
        [{"issuer": ISSUER}],
        "a string",
        None,
    ],
)
async def test_startup_fails_on_non_object_discovery(body: object) -> None:
    await startup_error(scripted_transport(discovery=(200, body)))


@pytest.mark.parametrize("body", [b"not json", ["keys"], "a string", None])
async def test_startup_fails_on_non_object_jwks(body: object) -> None:
    await startup_error(scripted_transport(jwks=(200, body)))


@pytest.mark.parametrize(
    "issuer",
    [
        # Different tenant, right shape.
        "https://login.microsoftonline.com/99999999-9999-9999-9999-999999999999/v2.0",
        # v1.0 issuer for the same tenant: a real Entra value, and still not
        # the one we configured.
        f"https://sts.windows.net/{TENANT_ID}/",
        # Trailing slash — no normalization, so "close enough" is not enough.
        f"{ISSUER}/",
        None,
    ],
)
async def test_startup_fails_on_issuer_mismatch(issuer: object) -> None:
    await startup_error(
        scripted_transport(discovery=(200, {"issuer": issuer, "jwks_uri": JWKS_URL}))
    )


@pytest.mark.parametrize(
    "jwks_uri",
    [
        # Plaintext: keys fetched over http can be swapped in flight.
        f"http://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys",
        # Foreign host: the whole point of pinning. An attacker who can shape
        # the metadata document would otherwise choose the signing keys.
        f"https://login.microsoftonline.com.evil.test/{TENANT_ID}/keys",
        f"https://evil.test/{TENANT_ID}/keys",
        # Userinfo trick: the real host is `evil.test`.
        "https://login.microsoftonline.com@evil.test/keys",
        # Explicit port.
        f"https://login.microsoftonline.com:8443/{TENANT_ID}/keys",
        # Malformed port.
        f"https://login.microsoftonline.com:notaport/{TENANT_ID}/keys",
        # Malformed host: an unbalanced bracket makes `urlsplit` itself raise
        # ValueError("Invalid IPv6 URL") before any field can be read. That
        # must still surface as a startup error — a bare ValueError would skip
        # `initialize()`'s handler and leak the owned client.
        "https://[::1/keys",
        f"https://login.microsoftonline.com]/{TENANT_ID}/keys",
        # Not a URL at all, and absent.
        "keys",
        "",
        None,
        42,
    ],
)
async def test_startup_fails_on_untrusted_jwks_uri(jwks_uri: object) -> None:
    await startup_error(
        scripted_transport(discovery=(200, {"issuer": ISSUER, "jwks_uri": jwks_uri}))
    )


@pytest.mark.parametrize("keys", [[], None, {}, "keys"])
async def test_startup_fails_on_empty_key_set(keys: object) -> None:
    await startup_error(scripted_transport(jwks=(200, {"keys": keys})))


@pytest.mark.parametrize("kid", [None, "", 7, ["kid-1"]])
async def test_startup_fails_when_a_key_has_no_usable_kid(kid: object) -> None:
    _, jwk = signing_material("kid-1")
    jwk["kid"] = kid
    if kid is None:
        del jwk["kid"]
    error = await startup_error(scripted_transport(jwks=(200, {"keys": [jwk]})))
    assert "kid" in str(error)


async def test_startup_fails_on_a_non_object_key_entry() -> None:
    _, jwk = signing_material("kid-1")
    await startup_error(scripted_transport(jwks=(200, {"keys": [jwk, "kid-2"]})))


async def test_startup_fails_on_duplicate_kid() -> None:
    """Two keys, one name: nothing can decide which one a token meant.

    Unusable keys are skipped below, but this is not a usability problem — it
    is a key set that contradicts itself, and picking one would be a coin toss
    repeated on every request.
    """
    _, first = signing_material("kid-1")
    _, second = signing_material("kid-1")
    error = await startup_error(scripted_transport(jwks=(200, {"keys": [first, second]})))
    assert "duplicate" in str(error)


async def test_duplicate_kid_is_fatal_even_when_one_copy_is_unusable() -> None:
    """The duplicate check runs before the usable/unusable filter.

    Otherwise a self-contradictory key set could be laundered into a valid one
    by whichever copy happened to fail parsing.
    """
    _, jwk = signing_material("kid-1")
    unusable = {"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB", "kid": "kid-1", "use": "sig"}
    error = await startup_error(scripted_transport(jwks=(200, {"keys": [jwk, unusable]})))
    assert "duplicate" in str(error)


# --- key skipping ---------------------------------------------------------------


async def test_unusable_key_is_skipped_and_others_still_verify() -> None:
    """One key Entra publishes that PyJWT cannot build must not be an outage.

    An all-or-nothing parse would hand the provider a switch that takes this
    service's authentication down: the day a key of an unsupported type
    appears in the set, an additive change on their side becomes a total
    failure on ours.

    Both skip branches are in the set, because they are not the same code
    path. The EC entry short-circuits at the `kty` check and never reaches
    `PyJWK.from_dict`; the malformed RSA entry is the one that gets there and
    raises, which is the branch a genuinely novel provider-side key would take.
    """
    private_key, jwk = signing_material("kid-1")
    wrong_kty = {"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB", "kid": "kid-ec", "use": "sig"}
    unparseable = {"kty": "RSA", "n": "!!!", "e": "AQAB", "kid": "kid-bad", "use": "sig"}
    client = httpx.AsyncClient(transport=mock_transport({"keys": [wrong_kty, unparseable, jwk]}))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        claims = await verifier.verify(access_token(private_key, kid="kid-1"))
        # Both skipped keys are absent from the cache, not merely unused.
        for skipped in ("kid-ec", "kid-bad"):
            with pytest.raises(TokenInvalidError):
                await verifier.verify(access_token(private_key, kid=skipped))
    finally:
        await client.aclose()
    assert claims["tid"] == TENANT_ID


@pytest.mark.parametrize(
    ("case", "entry"),
    [
        ("non_rsa_kty", {"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB", "kid": "k"}),
        ("encryption_use", {"kty": "RSA", "n": "AA", "e": "AQAB", "kid": "k", "use": "enc"}),
        ("malformed_rsa", {"kty": "RSA", "kid": "k"}),
        ("garbage_modulus", {"kty": "RSA", "n": "!!!", "e": "AQAB", "kid": "k", "use": "sig"}),
    ],
)
async def test_startup_fails_only_when_no_usable_key_remains(
    case: str, entry: dict[str, object]
) -> None:
    error = await startup_error(scripted_transport(jwks=(200, {"keys": [entry]})))
    # Pinned to the zero-usable-keys message so each case is proven to have
    # gone through the skip path and been rejected for emptiness, rather than
    # tripping one of the structural checks above and passing for the wrong
    # reason.
    assert "no usable" in str(error)


async def test_a_signing_key_alongside_an_encryption_key_still_works() -> None:
    """`use: enc` keys appear in real key sets and are simply not ours to use."""
    private_key, jwk = signing_material("kid-1")
    encryption_key = dict(jwk)
    encryption_key.update({"kid": "kid-enc", "use": "enc"})
    client = httpx.AsyncClient(transport=mock_transport({"keys": [encryption_key, jwk]}))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        await verifier.verify(access_token(private_key, kid="kid-1"))
        # The encryption key was skipped, so a token naming it is unknown.
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(private_key, kid="kid-enc"))
    finally:
        await client.aclose()


async def test_skipped_keys_log_only_the_kid_and_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A skip is logged, and the log line carries no key material.

    A forward regression sentinel, not a proof. Logging `type(exc).__name__`
    rather than `exc` is what keeps provider payload out of this line — and
    that is held by construction today, not by the assertion below: PyJWT
    2.13.0's message here is the constant "Not a public or private key", so
    the marker would survive even if the code logged `exc` verbatim. No entry
    shape was found whose exception echoes JWK material. The marker exists to
    catch a future PyJWT that starts embedding key material in these messages.

    Pointed at a malformed **RSA** entry because that is the only branch which
    formats an exception at all; the `kty`-mismatch branch logs a fixed string
    and could not leak under any version.

    The entry omits `e` rather than corrupting `n`, because PyJWT's base64
    decoder is lenient enough to accept a marker like this one as a modulus:
    `{"n": "SECRETX", "e": "AQAB"}` parses successfully. Dropping the exponent
    is what reliably reaches `InvalidKeyError` with the marker still in the
    key. (Checked against PyJWT 2.13.0, that message is the constant "Not a
    public or private key" — it does not embed the JWK today. This test holds
    the property rather than the current message.)
    """
    private_key, jwk = signing_material("kid-1")
    unparseable = {"kty": "RSA", "n": "SECRETX", "kid": "kid-bad", "use": "sig"}
    client = httpx.AsyncClient(transport=mock_transport({"keys": [unparseable, jwk]}))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        with caplog.at_level("WARNING", logger="azgenai_lab.services.entra_jwt"):
            await verifier.initialize()
    finally:
        await client.aclose()
    messages = [record.getMessage() for record in caplog.records]
    assert any("kid-bad" in message for message in messages)
    assert not any("SECRETX" in message for message in messages)


# --- cache publication ----------------------------------------------------------


async def test_a_failed_reinitialize_neither_publishes_nor_destroys_the_cache() -> None:
    """The cache is replaced only after the whole response validates.

    Written as a *second* `initialize()` over a working one, because that is
    the only arrangement in which the property is observable. On a first
    startup the uninitialized-verifier guard refuses every token regardless of
    what landed in `_keys`, so a leak would be invisible; here the old key must
    still verify and the new one must not, and both halves fail if the cache
    were assigned before the key set finished validating.

    This is the path Task 4 rewrites when it adds refresh, which is why it is
    pinned behaviorally rather than by reading private state.
    """
    old_key, old_jwk = signing_material("kid-old")
    new_key, new_jwk = signing_material("kid-new")
    responses = [{"keys": [old_jwk]}, {"keys": [new_jwk, new_jwk]}]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DISCOVERY_URL:
            return httpx.Response(200, json=DISCOVERY_BODY)
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=responses.pop(0))
        raise AssertionError(f"unexpected URL: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        await verifier.verify(access_token(old_key, kid="kid-old"))

        # Second key set is self-contradictory (duplicate kid) and must be
        # rejected wholesale.
        with pytest.raises(TokenVerifierStartupError):
            await verifier.initialize()

        # Still the old key set: not half-replaced, and not wiped either.
        claims = await verifier.verify(access_token(old_key, kid="kid-old"))
        assert claims["tid"] == TENANT_ID
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(new_key, kid="kid-new"))
    finally:
        await client.aclose()


def owned_verifier(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Callable[[], float] | None = None,
) -> EntraTokenVerifier:
    """A verifier that *owns* its client, but whose client speaks to a mock.

    The constructor only owns a client it built itself, so the transport has
    to be supplied to the constructor rather than to the verifier. Patching
    the factory keeps the real ownership branch in play.
    """
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    # Scoped rather than `monkeypatch.undo()`, which reverts *every* patch
    # registered on the fixture instance, not just this one. Today's only
    # caller registers nothing else; a future one that does would have its
    # patch silently torn down here.
    kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
    with monkeypatch.context() as patched:
        patched.setattr(httpx, "AsyncClient", factory)
        return EntraTokenVerifier(TENANT_ID, AUDIENCE, **kwargs)


async def test_a_failed_reinitialize_keeps_an_owned_client_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown is for a verifier that never came up, not for a live one.

    Closing the owned client here would leave the worst kind of object: one
    that still answers `verify()` from its live cache, so nothing looks wrong,
    but can never refresh again — the next `initialize()` dies inside httpx
    with `RuntimeError("Cannot send a request, as the client has been
    closed.")`, which is neither `HTTPError` nor `InvalidURL` and so escapes
    `_get_json` uncaught.

    Uses an owned client on purpose: the injected-client tests cannot reach
    this path, because `aclose()` is a no-op for a client it does not own.
    The transport is supplied by patching the constructor's factory rather
    than by assigning `_client` afterwards, so `_owns_client` is set by the
    real ownership branch instead of being staged.
    """
    key, jwk = signing_material("kid-1")
    responses: list[dict[str, object]] = [
        {"keys": [jwk]},  # first startup: fine
        {"keys": [jwk, jwk]},  # refresh: duplicate kid, rejected wholesale
        {"keys": [jwk]},  # third call must still be possible
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DISCOVERY_URL:
            return httpx.Response(200, json=DISCOVERY_BODY)
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=responses.pop(0))
        raise AssertionError(f"unexpected URL: {request.url}")

    verifier = owned_verifier(monkeypatch, handler)
    try:
        await verifier.initialize()
        with pytest.raises(TokenVerifierStartupError):
            await verifier.initialize()
        assert verifier.closed is False
        # The client is still usable: a later refresh can actually happen.
        await verifier.initialize()
        await verifier.verify(access_token(key, kid="kid-1"))
    finally:
        await verifier.aclose()
    assert verifier.closed is True


async def test_verify_before_initialize_is_a_programming_error() -> None:
    """Not `TokenInvalidError`: nothing is wrong with the token.

    An unguarded empty cache rejects every token as an unknown `kid`, which
    reaches the caller as a 401 storm indistinguishable from a client at fault
    and leaves nothing in the log pointing at the wiring.
    """
    private_key, _ = signing_material("kid-1")
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE)
    try:
        with pytest.raises(RuntimeError) as caught:
            await verifier.verify(access_token(private_key, kid="kid-1"))
        assert not isinstance(caught.value, TokenInvalidError)
    finally:
        await verifier.aclose()


async def test_verify_after_a_failed_startup_is_a_programming_error() -> None:
    """A verifier that never came up must not be usable, by the same rule."""
    private_key, jwk = signing_material("kid-1")
    client = httpx.AsyncClient(transport=scripted_transport(jwks=(200, {"keys": [jwk, jwk]})))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        with pytest.raises(TokenVerifierStartupError):
            await verifier.initialize()
        with pytest.raises(RuntimeError):
            await verifier.verify(access_token(private_key, kid="kid-1"))
    finally:
        await client.aclose()


# --- rotation, cooldown and max age ---------------------------------------------


@dataclass
class ManualClock:
    """A monotonic clock the test moves by hand.

    Cache age and cooldown are the entire subject of this section, and they are
    measured in hours and minutes; sleeping through them is not an option, and
    patching `time.monotonic` globally would reach every other clock in the
    process. The verifier takes its clock as a constructor argument precisely
    so this can be a local substitution.
    """

    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RotatingJwks:
    """A JWKS endpoint whose published key set can change mid-test.

    `jwks_calls` counts requests to the keys endpoint, so "one refresh" reads
    as one increment. `discovery_calls` counts the metadata leg separately,
    because a refresh is *two* outbound requests, not one: the anti-
    amplification bound this module offers is two per cooldown window per
    verifier, and a regression that multiplied only the discovery leg would be
    invisible to a keys-only count.
    """

    def __init__(self, keys: list[dict[str, object]]) -> None:
        self.current_keys = list(keys)
        self.jwks_calls = 0
        self.discovery_calls = 0
        self.fail = False
        # Runs while the keys request is in flight, which is the only moment a
        # test can act on a half-finished refresh.
        self.before_response: Callable[[], None] | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        # Async, and yielding, on purpose. `MockTransport` awaits an async
        # handler; a sync one never reaches the event loop, so a burst of
        # "concurrent" verifies would run strictly one after another — the
        # first finishing its entire refresh before the second began. Every
        # serialization property would then hold whether or not the code
        # serializes anything, and the lock would be untested. This is the
        # suspension point that makes the interleaving real.
        await asyncio.sleep(0)
        if str(request.url) == DISCOVERY_URL:
            self.discovery_calls += 1
            return httpx.Response(200, json=DISCOVERY_BODY)
        if str(request.url) == JWKS_URL:
            self.jwks_calls += 1
            if self.before_response is not None:
                self.before_response()
            if self.fail:
                return httpx.Response(503, json={"error": "nope"})
            return httpx.Response(200, json={"keys": self.current_keys})
        raise AssertionError(f"unexpected URL: {request.url}")


@asynccontextmanager
async def rotating_verifier(
    source: RotatingJwks, clock: ManualClock
) -> AsyncIterator[EntraTokenVerifier]:
    """A verifier wired to `source` and `clock`, with the client always closed."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(source.handler))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client, clock=clock)
    try:
        yield verifier
    finally:
        await client.aclose()


async def test_concurrent_unknown_kid_causes_one_refresh() -> None:
    """Twenty simultaneous unknown-`kid` requests share a single fetch.

    The lock is not an optimization. `kid` is attacker-controlled, so an
    unknown one that always fetched would make this service a request
    amplifier pointed at Microsoft: N concurrent forged tokens, N outbound
    requests, no credentials needed.

    All twenty must also *succeed*, and that half is what the lock is for.
    The cooldown alone would hold the call count down — the first arrival
    stamps the attempt before it awaits, so the other nineteen are inside the
    window — but they would then fall straight through to a lookup against the
    cache the refresh has not published yet, and a rotation would answer 401
    to nineteen legitimate requests. The lock is what makes them wait for the
    answer instead of racing past it.
    """
    _, old_jwk = signing_material("kid-1")
    new_key, new_jwk = signing_material("kid-2")
    source = RotatingJwks([old_jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
        source.current_keys = [new_jwk]
        token = access_token(new_key, kid="kid-2")
        results = await asyncio.gather(*(verifier.verify(token) for _ in range(20)))
    assert all(result["oid"] == "33333333-3333-3333-3333-333333333333" for result in results)
    assert source.jwks_calls == 2  # startup + one shared refresh
    # Twenty forged tokens buy one discovery request too, not twenty.
    assert source.discovery_calls == 2


async def test_unknown_kid_inside_cooldown_does_not_refresh() -> None:
    """A forged `kid` moments after startup buys the attacker nothing.

    The cooldown clock starts at the startup fetch rather than at the first
    refresh, so the window right after a process comes up — the one an
    attacker can aim at by watching for a deploy — is covered like any other.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(private_key, kid="kid-unknown"))
    assert source.jwks_calls == 1


async def test_stale_known_key_refreshes_after_24_hours() -> None:
    """Age alone refreshes the cache; no unknown `kid` has to ask for it.

    Without this trigger a verifier whose traffic all names keys it already
    holds would never notice a withdrawal, and would keep honouring a revoked
    key until the process restarted.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_MAX_AGE_SECONDS + 1)
        claims = await verifier.verify(access_token(private_key, kid="kid-1"))
    assert claims["tid"] == TENANT_ID
    assert source.jwks_calls == 2


async def test_failed_stale_refresh_keeps_known_key_and_starts_cooldown() -> None:
    """An unreachable JWKS endpoint must not take authentication down with it.

    Two properties in one arrangement: the cache that failed to refresh still
    verifies (a provider outage is not an outage here), and the *failed*
    attempt starts the cooldown, so a persistently broken endpoint is retried
    on a timer rather than once per request. The second is why the attempt
    timestamp is written before the network await and not after it.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_MAX_AGE_SECONDS + 1)
        source.fail = True
        first = await verifier.verify(access_token(private_key, kid="kid-1"))
        second = await verifier.verify(access_token(private_key, kid="kid-1"))
    assert first["tid"] == TENANT_ID
    assert second["tid"] == TENANT_ID
    assert source.jwks_calls == 2  # startup + one failed attempt


async def test_successful_refresh_replaces_instead_of_merging_keys() -> None:
    """The refreshed key set replaces the cache; it is not merged into it.

    A merge is invisible on the happy path and wrong in exactly the case that
    matters: the tenant withdrew `kid-1`, and a verifier that kept it would go
    on accepting tokens signed by a retired key. The call count pins the other
    half — the miss triggers one refresh, not a second one after the first
    already ran in this same `verify()`.
    """
    old_key, old_jwk = signing_material("kid-1")
    _, new_jwk = signing_material("kid-2")
    source = RotatingJwks([old_jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_MAX_AGE_SECONDS + 1)
        source.current_keys = [new_jwk]
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(old_key, kid="kid-1"))
    assert source.jwks_calls == 2


# --- rotation under an adversary ------------------------------------------------


async def test_concurrent_failed_unknown_kid_requests_make_one_attempt() -> None:
    """The amplifier is closed on the failure path too.

    The success path is the easy half: the first waiter publishes the key and
    the rest find it, so they never reach the network. When the refresh
    *fails* there is nothing to find, every waiter re-enters the refresh method
    with the same unknown `kid`, and the cooldown — set before the network
    await — is the only thing standing between twenty forged tokens and twenty
    outbound requests.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
        source.fail = True
        token = access_token(private_key, kid="kid-unknown")
        results = await asyncio.gather(
            *(verifier.verify(token) for _ in range(20)), return_exceptions=True
        )
    assert all(isinstance(result, TokenInvalidError) for result in results)
    assert source.jwks_calls == 2  # startup + one failed attempt shared by all twenty


async def test_a_second_unknown_kid_during_cooldown_makes_no_attempt() -> None:
    """Rotating the forged `kid` does not rotate past the cooldown.

    The cooldown is a property of the cache, not of the `kid` that prompted
    it. A per-`kid` cooldown would be an amplifier with one extra step, since
    the attacker is the one choosing the `kid` — nine more forgeries here, and
    the call count does not move.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(private_key, kid="forged-a"))
        assert source.jwks_calls == 2
        for suffix in "bcdefghij":
            with pytest.raises(TokenInvalidError):
                await verifier.verify(access_token(private_key, kid=f"forged-{suffix}"))
    assert source.jwks_calls == 2


async def test_each_cooldown_window_permits_exactly_one_attempt() -> None:
    """Sustained forged traffic costs one outbound request per minute, no more.

    Three windows rather than one, because a cooldown that is armed once and
    never re-armed passes a single-window test. Each window also probes just
    short of the boundary, so the timestamp being refreshed on every attempt —
    not only on the first — is what the count is measuring.

    Both legs are counted. A refresh re-reads the discovery document before
    the keys, so the honest bound is *two* requests per window, and holding
    the two counts equal is what stops that ratio from drifting unnoticed —
    a keys-only assertion would not move if discovery were retried in a loop.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        expected = 1  # the startup fetch
        for _ in range(3):
            clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
            expected += 1
            for _ in range(5):
                with pytest.raises(TokenInvalidError):
                    await verifier.verify(access_token(private_key, kid="forged"))
                assert source.jwks_calls == expected
                assert source.discovery_calls == expected
            # Just short of the next window opening: still nothing.
            clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS - 1)
            with pytest.raises(TokenInvalidError):
                await verifier.verify(access_token(private_key, kid="forged"))
            assert source.jwks_calls == expected
            assert source.discovery_calls == expected


async def test_a_malformed_refresh_response_retains_the_previous_cache() -> None:
    """A 200 that parses but contradicts itself must not empty the cache.

    A different failure shape from the dead endpoint above: the request
    succeeds, the JSON is well formed, and the *key set* is the thing that is
    wrong. The refresh path validates the whole document before publishing
    anything, so the old key keeps working and no half-built key set ever
    becomes the live one — the same property `initialize()` holds, now on the
    path that runs while requests are being served.

    Both halves are asserted, and the document is built so that the second one
    can fail. `_parse_keys` walks the entries in order, so `kid-2` is already
    in the dict under construction when the duplicate `kid-1` further down
    raises: a refresh that built into the live cache instead of a local would
    leave `kid-2` verifiable off a document that was rejected wholesale. With
    only the duplicate pair in the response there is no new key to leak, and
    the leak half of the property would be untestable.
    """
    private_key, jwk = signing_material("kid-1")
    leaked_key, leaked_jwk = signing_material("kid-2")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_MAX_AGE_SECONDS + 1)
        # Valid new key first, then a duplicate `kid` that condemns the set.
        source.current_keys = [leaked_jwk, jwk, jwk]
        # Not destroyed: the old key set is still serving.
        claims = await verifier.verify(access_token(private_key, kid="kid-1"))
        # Not leaked: nothing from the rejected document became live.
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(leaked_key, kid="kid-2"))
    assert claims["tid"] == TENANT_ID
    assert source.jwks_calls == 2


async def test_a_successful_refresh_withdraws_a_retired_key() -> None:
    """The retired key stops verifying while its replacement carries on.

    `test_successful_refresh_replaces_instead_of_merging_keys` shows the
    withdrawn key being rejected, but with an empty overlap it cannot tell
    "the retired key was dropped" apart from "the refresh broke everything".
    Here one key survives the rotation and one does not, in the same key set.
    """
    retired_key, retired_jwk = signing_material("kid-retired")
    kept_key, kept_jwk = signing_material("kid-kept")
    source = RotatingJwks([retired_jwk, kept_jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        await verifier.verify(access_token(retired_key, kid="kid-retired"))
        clock.advance(entra_jwt.JWKS_MAX_AGE_SECONDS + 1)
        source.current_keys = [kept_jwk]
        claims = await verifier.verify(access_token(kept_key, kid="kid-kept"))
        with pytest.raises(TokenInvalidError):
            await verifier.verify(access_token(retired_key, kid="kid-retired"))
    assert claims["tid"] == TENANT_ID
    assert source.jwks_calls == 2


async def test_a_waiter_does_not_refetch_after_a_slow_refresh_publishes_its_key() -> None:
    """A refresh slower than its own cooldown is not repeated by its waiters.

    Every waiter behind the lock is holding a `kid` that has just been
    published, and "is the key here now?" is the check that lets them out.
    Usually the cooldown would cover for its absence — the waiters are inside
    the window the fetch opened. Not here: the fetch outlasts its own cooldown
    (a slow provider, which is exactly when a queue forms), so by the time the
    queue drains the rate limit has expired and nothing else would stop each
    waiter from starting a fresh fetch for a key it already holds.

    The clock jumps *during* the keys request, so the window closes on the
    refresh that is still in flight. Note this pins the check's presence, not
    its position: all three guards return, so reordering them is not
    observable from the outside.
    """
    _, old_jwk = signing_material("kid-1")
    new_key, new_jwk = signing_material("kid-2")
    source = RotatingJwks([old_jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
        source.current_keys = [new_jwk]
        source.before_response = lambda: clock.advance(
            entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1
        )
        token = access_token(new_key, kid="kid-2")
        results = await asyncio.gather(*(verifier.verify(token) for _ in range(5)))
    assert all(result["oid"] == "33333333-3333-3333-3333-333333333333" for result in results)
    assert source.jwks_calls == 2  # startup + one refresh, not one per waiter


async def test_a_forged_algorithm_with_an_unknown_kid_drives_no_jwks_traffic() -> None:
    """The load-bearing form of the header-screen ordering.

    `test_verify_rejects_alg_none` and
    `test_verify_rejects_hs256_before_touching_the_key_cache` both present a
    `kid` we publish, so a cache hit ends the request whatever order the checks
    run in, and `algorithms=["RS256"]` rejects the token either way. Neither
    can fail if the screens are deleted.

    This one can. The `kid` is unknown and the cooldown has expired, so a
    lookup miss costs a real outbound request — and the backstop that makes
    those two green, PyJWT's own algorithm list, does not run until after that
    request would already have been made. An attacker mints these for free.
    """
    _, jwk = signing_material("kid-1")
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    header = {"kid": "kid-unknown"}
    forged = [
        jwt.encode(claims, None, algorithm="none", headers=header),  # type: ignore[arg-type]
        jwt.encode(claims, secrets.token_bytes(32), algorithm="HS256", headers=header),
    ]
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        # Every gate that could stop a refresh for an unrelated reason is open:
        # the cooldown has lapsed, so only the header screens are left.
        clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
        for token in forged:
            with pytest.raises(TokenInvalidError):
                await verifier.verify(token)
    assert source.jwks_calls == 1


async def test_an_unknown_kid_after_aclose_is_a_rejection_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not convert a bad token into a server error.

    Once `verify()` can reach the network, a closed client is on its path:
    httpx answers one with a bare `RuntimeError`, which is neither `HTTPError`
    nor `InvalidURL` and so escapes `_get_json` — a request that is merely
    unauthenticated would land as a 500 during drain, and in the logs it would
    look like a defect rather than a forged token.

    Owned client on purpose: `aclose()` is a no-op for an injected one, so
    `_closed` — the only signal the verifier has — is never set there.
    """
    private_key, jwk = signing_material("kid-1")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DISCOVERY_URL:
            return httpx.Response(200, json=DISCOVERY_BODY)
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json={"keys": [jwk]})
        raise AssertionError(f"unexpected URL: {request.url}")

    clock = ManualClock()
    verifier = owned_verifier(monkeypatch, handler, clock=clock)
    await verifier.initialize()
    # Past the cooldown, so nothing but the closed-client guard is left to
    # stop the refresh this unknown `kid` would otherwise trigger.
    clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
    await verifier.aclose()
    assert verifier.closed is True

    with pytest.raises(TokenInvalidError):
        await verifier.verify(access_token(private_key, kid="kid-unknown"))
    # The cached key set outlives the client, so a good token still verifies.
    claims = await verifier.verify(access_token(private_key, kid="kid-1"))
    assert claims["tid"] == TENANT_ID


async def test_a_refresh_failure_does_not_revoke_readiness() -> None:
    """A failed refresh leaves a working verifier, not an uninitialized one.

    `verify()` raises `RuntimeError` for a verifier that never came up. If the
    refresh path touched the readiness latch or cleared the cache timestamp to
    force a retry, that programming-error signal would start firing at real
    clients over a cache that is still perfectly serviceable — and it would
    arrive as a 500, not a 401.
    """
    private_key, jwk = signing_material("kid-1")
    source = RotatingJwks([jwk])
    clock = ManualClock()
    async with rotating_verifier(source, clock) as verifier:
        await verifier.initialize()
        clock.advance(entra_jwt.JWKS_MAX_AGE_SECONDS + 1)
        source.fail = True
        await verifier.verify(access_token(private_key, kid="kid-1"))
        source.fail = False
        # The next window opens and the retry succeeds: a failed refresh does
        # not wedge the path either.
        clock.advance(entra_jwt.JWKS_REFRESH_COOLDOWN_SECONDS + 1)
        claims = await verifier.verify(access_token(private_key, kid="kid-1"))
    assert claims["tid"] == TENANT_ID
    assert source.jwks_calls == 3


# --- lifecycle ------------------------------------------------------------------


def _failing_get(exc: Exception) -> Callable[..., Awaitable[httpx.Response]]:
    async def get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise exc

    return get


async def test_owned_client_closes_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patched at the class, so the verifier's own client is used and no socket
    # is ever opened — the failure is deterministic and offline.
    monkeypatch.setattr(httpx.AsyncClient, "get", _failing_get(httpx.ConnectError("offline")))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE)
    with pytest.raises(TokenVerifierStartupError):
        await verifier.initialize()
    assert verifier.closed


async def test_aclose_is_idempotent() -> None:
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE)
    await verifier.aclose()
    await verifier.aclose()
    assert verifier.closed


async def test_aclose_leaves_an_injected_client_alone() -> None:
    """A client we were handed belongs to the caller and outlives the verifier."""
    _, jwk = signing_material("kid-1")
    client = httpx.AsyncClient(transport=mock_transport({"keys": [jwk]}))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        await verifier.initialize()
        await verifier.aclose()
        assert verifier.closed is False
        assert client.is_closed is False
    finally:
        await client.aclose()


async def test_startup_failure_leaves_an_injected_client_alone() -> None:
    client = httpx.AsyncClient(transport=scripted_transport(jwks=(500, {"error": "nope"})))
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE, client=client)
    try:
        with pytest.raises(TokenVerifierStartupError):
            await verifier.initialize()
        assert verifier.closed is False
        assert client.is_closed is False
    finally:
        await client.aclose()


async def test_owned_client_is_created_when_none_is_injected() -> None:
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE)
    assert verifier.closed is False
    await verifier.aclose()
    assert verifier.closed is True


# --- module boundary ------------------------------------------------------------


def test_module_does_not_depend_on_the_http_layer() -> None:
    """`services/` must not reach into `api/`.

    Claims become a `Principal`, and failures become status codes, one layer
    up. Keeping that out of here is what lets this module be exercised with a
    key pair and a mock transport, and it is the series-wide rule the import
    graph is checked against.
    """
    tree = ast.parse(Path(entra_jwt.__file__ or "").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported if name.split(".")[0] in {"fastapi", "starlette"}]
    assert not [name for name in imported if name.startswith("azgenai_lab.api")]
    assert not [name for name in imported if name.startswith("azgenai_lab.models")]
