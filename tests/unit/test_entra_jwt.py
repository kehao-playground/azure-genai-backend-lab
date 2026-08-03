"""Cryptographic verification of Entra access tokens, offline.

Every key in this file is generated locally and every HTTP response is served
by `httpx.MockTransport`: the suite never reaches `login.microsoftonline.com`,
so a broken network, a rotated tenant key or a provider outage cannot turn into
a red run that says nothing about this code.
"""

import ast
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
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

    The cache is static after `initialize()` in this module; recovering from a
    key rotation is a separate concern, and asserting the call count pins that
    boundary rather than leaving it implied.
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
    """Rejected at the header screen — though not *only* there today.

    `algorithms=["RS256"]` would refuse this token anyway, so deleting the
    header screen would not turn this test red. The screen is what stops the
    token before key lookup, and from Task 4 onward that ordering is
    load-bearing: an unsigned token must not be able to trigger a JWKS
    refresh. Kept as a contract test, not as proof of the ordering.
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

    The `calls == []` assertion is weaker than it looks *today*: `verify()`
    makes no requests on any path in this task, so it holds whatever the
    ordering is. It is here to be inherited — once Task 4 lets an unknown
    `kid` trigger a refresh, this becomes the assertion that an attacker
    cannot drive JWKS traffic with a token we never had to look at.
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
    leaves this green. It earns its keep from Task 4 onward, when a lookup
    miss stops being a dead end and starts costing a refresh.
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
) -> EntraTokenVerifier:
    """A verifier that *owns* its client, but whose client speaks to a mock.

    The constructor only owns a client it built itself, so the transport has
    to be supplied to the constructor rather than to the verifier. Patching
    the factory keeps the real ownership branch in play.
    """
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    verifier = EntraTokenVerifier(TENANT_ID, AUDIENCE)
    monkeypatch.undo()
    return verifier


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
