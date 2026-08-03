"""Cryptographic verification of Microsoft Entra ID access tokens.

This module is the whole trust boundary for a bearer token and nothing else:
it discovers the tenant's OIDC metadata once at startup, caches the published
signing keys, and answers one question per request — *was this token signed by
a key this tenant publishes, and are its registered claims (`iss`, `aud`,
`exp`) the ones we require?* It returns the claims and forms no opinion about
what they mean.

It is deliberately HTTP-framework-agnostic. No FastAPI, no `Request`, no
`HTTPException`, no `Principal`: the mapping from a failure here to a status
code, and the mapping from claims to an application identity, both live in
`api/principal.py`. That keeps `services/` free of any dependency on `api/`,
and it keeps this module testable with nothing but a key pair and a mock
transport.

**This module does not check `tid`.** That is not an oversight and not a gap:
the tenant comparison happens exactly once, in the resolver, which is the only
layer that needs `tid` anyway (it builds `Principal.tenant_id` from it). A
duplicated check would merely be redundant, but two layers each assuming the
other did it is a tenant bypass — so the check has one named owner and it is
not here. The tenant is still pinned at this layer by the exact `iss` match,
because the tenant-specific issuer URL embeds the tenant GUID.
"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWK
from jwt.exceptions import PyJWTError

logger = logging.getLogger(__name__)

# The only host this verifier will talk to. Discovery hands us a `jwks_uri`,
# and following it blindly would let a compromised or spoofed metadata
# document point key retrieval anywhere; pinning the host means the metadata
# can only ever redirect us within Microsoft's own identity endpoint.
OIDC_HOST = "login.microsoftonline.com"
HTTP_TIMEOUT_SECONDS = 10.0
# Consumed by the rotation work that sits on top of this module: the shortest
# interval between two key refreshes, and the age past which a cached key set
# is refreshed even without an unknown `kid` to prompt it. They live here so
# there is one place that states the cache's timing policy.
JWKS_REFRESH_COOLDOWN_SECONDS = 60.0
JWKS_MAX_AGE_SECONDS = 24 * 60 * 60.0


class TokenVerifierStartupError(RuntimeError):
    """The verifier could not be brought up against the tenant's metadata.

    Startup-only, and fatal by design: a process that cannot fetch the signing
    keys cannot authenticate anybody, and starting anyway would mean serving
    requests that are guaranteed to fail — or, worse, inviting a later
    "temporarily skip verification" workaround.
    """


class TokenInvalidError(Exception):
    """A presented token is not trusted.

    Every reason collapses into this one type on purpose. The caller is an
    unauthenticated client, and telling it *which* check failed — expired
    versus wrong audience versus unknown key — is free reconnaissance. The
    detail belongs in the server's log, which is why the underlying exception
    is always chained (`raise ... from exc`) rather than formatted into the
    message.
    """


class EntraTokenVerifier:
    """Verifies RS256 access tokens against a tenant's published JWKS.

    The key set is fetched once by `initialize()` and is static thereafter.
    """

    def __init__(
        self,
        tenant_id: str,
        audience: str,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = audience
        # Built from the tenant GUID rather than read from the discovery
        # document: the value we compare `iss` against has to be the one we
        # configured, otherwise the comparison is the document validating
        # itself.
        self._issuer = f"https://{OIDC_HOST}/{tenant_id}/v2.0"
        self._discovery_url = f"{self._issuer}/.well-known/openid-configuration"
        # Ownership decides who closes. A client we were handed belongs to the
        # caller and outlives us; one we build is ours to tear down.
        self._owns_client = client is None
        self._client = (
            client if client is not None else httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        )
        # Monotonic by default and injectable so cache-age behaviour can be
        # tested without sleeping. Wall-clock time would let an NTP correction
        # move the cache's age backwards.
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """True once an owned client has been closed. Injected clients are the
        caller's to close, so this stays False for them."""
        return self._closed

    async def initialize(self) -> None:
        """Fetch and validate the tenant's OIDC metadata and signing keys.

        Nothing is published to the cache until the entire response has
        validated, so a partially-parsed key set can never become the live one.
        """
        try:
            metadata = await self._get_json(self._discovery_url, "OIDC discovery")
            jwks_uri = self._jwks_uri(metadata)
            document = await self._get_json(jwks_uri, "JWKS")
            keys = self._parse_keys(document)
        except TokenVerifierStartupError:
            # A verifier that failed to start is never used again, so an owned
            # client would otherwise leak for the life of the process.
            await self.aclose()
            raise
        self._keys = keys
        self._fetched_at = self._clock()

    async def verify(self, token: str) -> Mapping[str, Any]:
        """Return the token's claims, or raise `TokenInvalidError`.

        Checks the untrusted header before touching the key cache: a token
        naming an algorithm we do not accept is rejected without ever being
        looked up, which is what stops an attacker from using algorithm
        confusion (`none`, or HS256 keyed on the public key) to reach the
        verification path at all.
        """
        # A programming error, not a token problem, so it is not
        # `TokenInvalidError`: an uninitialized verifier has an empty cache and
        # would otherwise reject every token as an unknown `kid` — a 401 storm
        # that looks exactly like a client at fault, with nothing in the log
        # pointing at the wiring. Checked ahead of the header screen because it
        # is a fact about this object, not about the token, and should not
        # depend on what the caller happened to present.
        if self._fetched_at is None:
            raise RuntimeError("EntraTokenVerifier.initialize() has not completed")

        try:
            header = jwt.get_unverified_header(token)
        except (PyJWTError, TypeError, ValueError) as exc:
            raise TokenInvalidError("token header could not be read") from exc

        if header.get("alg") != "RS256":
            raise TokenInvalidError("token algorithm is not accepted")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenInvalidError("token header carries no key id")

        key = self._keys.get(kid)
        if key is None:
            raise TokenInvalidError("token key id is not in the published key set")

        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                # Clock skew between Entra and this host, not a grace period
                # for expired tokens.
                leeway=60,
                # A token missing any of these would otherwise pass by having
                # nothing to check: PyJWT only validates claims that exist.
                options={"require": ["exp", "iss", "aud"]},
            )
        except (PyJWTError, TypeError, ValueError) as exc:
            raise TokenInvalidError("token failed verification") from exc
        return claims

    async def aclose(self) -> None:
        """Close an owned client. Idempotent; a no-op for injected clients."""
        if self._closed or not self._owns_client:
            return
        self._closed = True
        await self._client.aclose()

    async def _get_json(self, url: str, what: str) -> Any:
        # The timeout is passed per request, not left to the client, so an
        # injected client configured without one cannot hang startup
        # indefinitely. Redirects are deliberately not followed: httpx does not
        # follow them by default, and a 3xx is the one way a host-pinned URL
        # could still land somewhere else.
        try:
            response = await self._client.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        # `httpx.InvalidURL` is deliberately named alongside `HTTPError`: it is
        # not a subclass of it, so a URL httpx itself rejects (one over its
        # length limit, say) would otherwise escape this boundary uncaught.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TokenVerifierStartupError(f"{what} request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            # json.JSONDecodeError; the body is not our data to log.
            raise TokenVerifierStartupError(f"{what} response was not JSON") from exc

    def _jwks_uri(self, metadata: Any) -> str:
        if not isinstance(metadata, Mapping):
            raise TokenVerifierStartupError("OIDC discovery response was not a JSON object")
        if metadata.get("issuer") != self._issuer:
            # Exact match, no normalization: this is the value every token's
            # `iss` is compared against, so accepting a "close enough" issuer
            # here would widen every later comparison.
            raise TokenVerifierStartupError(
                "OIDC discovery issuer does not match the configured tenant"
            )
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise TokenVerifierStartupError("OIDC discovery response carries no jwks_uri")
        try:
            parts = urlsplit(jwks_uri)
            port = parts.port
        except ValueError as exc:
            # Both calls can raise: `urlsplit` rejects unbalanced brackets
            # ("Invalid IPv6 URL"), and `.port` rejects a non-numeric port.
            # Neither may escape as a bare ValueError — `initialize()` only
            # handles `TokenVerifierStartupError`, so anything else would skip
            # the owned-client cleanup and reach the composition point as an
            # unhandled traceback instead of its fatal-startup path.
            raise TokenVerifierStartupError("jwks_uri is not a well-formed URL") from exc
        if (
            parts.scheme != "https"
            or parts.hostname != OIDC_HOST
            or parts.username
            or parts.password
            or port is not None
        ):
            raise TokenVerifierStartupError("jwks_uri is not an https URL on the expected host")
        return jwks_uri

    def _parse_keys(self, document: Any) -> dict[str, PyJWK]:
        if not isinstance(document, Mapping):
            raise TokenVerifierStartupError("JWKS response was not a JSON object")
        entries = document.get("keys")
        if not isinstance(entries, list) or not entries:
            raise TokenVerifierStartupError("JWKS response carries no keys")

        keys: dict[str, PyJWK] = {}
        seen: set[str] = set()
        for entry in entries:
            # Structural problems with the key *set* are fatal, even though
            # unusable individual keys below are not: a set that cannot say
            # which key is which is self-contradictory, and picking one of two
            # keys sharing a `kid` would be a coin toss on every request.
            if not isinstance(entry, Mapping):
                raise TokenVerifierStartupError("JWKS contains an entry that is not an object")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                raise TokenVerifierStartupError("JWKS contains a key with no usable kid")
            if kid in seen:
                raise TokenVerifierStartupError("JWKS contains duplicate kid values")
            seen.add(kid)

            # Individual keys we cannot use are skipped rather than fatal.
            # All-or-nothing parsing would hand Microsoft a switch that takes
            # this service's authentication offline: the day Entra publishes
            # one key of a type PyJWT will not build as RS256, a provider-side
            # additive change becomes a total outage here.
            if entry.get("kty") != "RSA":
                logger.warning("skipping JWKS key kid=%s reason=non_rsa_kty", kid)
                continue
            use = entry.get("use")
            if use is not None and use != "sig":
                logger.warning("skipping JWKS key kid=%s reason=non_signing_use", kid)
                continue
            try:
                key = PyJWK.from_dict(dict(entry), algorithm="RS256")
            except (PyJWTError, TypeError, ValueError, KeyError) as exc:
                # Only the kid and the exception class: key material and raw
                # provider payloads do not belong in this log line.
                logger.warning("skipping JWKS key kid=%s reason=%s", kid, type(exc).__name__)
                continue
            keys[kid] = key

        if not keys:
            raise TokenVerifierStartupError("JWKS contains no usable RS256 signing keys")
        return keys


__all__ = [
    "HTTP_TIMEOUT_SECONDS",
    "JWKS_MAX_AGE_SECONDS",
    "JWKS_REFRESH_COOLDOWN_SECONDS",
    "OIDC_HOST",
    "EntraTokenVerifier",
    "TokenInvalidError",
    "TokenVerifierStartupError",
]
