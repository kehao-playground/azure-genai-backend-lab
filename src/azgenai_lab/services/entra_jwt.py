"""Cryptographic verification of Microsoft Entra ID access tokens.

This module is the whole trust boundary for a bearer token and nothing else:
it discovers the tenant's OIDC metadata at startup, caches the published
signing keys (re-reading them when they age out or when a token names a key it
has not seen), and answers one question per request — *was this token signed by
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

import asyncio
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
# The cache's timing policy, in one place. The cooldown is the shortest
# interval between two refresh *attempts* — successful or not — and it is the
# rate limit on an attacker-driven refresh, not a tuning knob. The max age is
# the point past which a cached key set is refreshed with nothing prompting it,
# which is what lets a withdrawn key stop working on a verifier whose traffic
# only ever names keys it already holds.
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

    The key set is fetched by `initialize()` and refreshed lazily thereafter,
    on two triggers: a cached set older than `JWKS_MAX_AGE_SECONDS`, and a
    token naming a `kid` the cache does not hold. Both go through one
    serialized, rate-limited path — see `_refresh_keys` for why the second
    trigger cannot be turned into an outbound request amplifier.

    They differ in how much a request will *wait* for that path, which
    `verify()` decides: a cache miss blocks on the fetch, an aged-out cache
    that can still answer the request does not.
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
        # Two separate facts, deliberately not one. `_initialized` answers "has
        # this verifier ever come up", which is a latch: once true it never goes
        # back. `_fetched_at` answers "how old is the cache", which the refresh
        # path moves freely. Folding them together would mean a refresh that
        # invalidates by clearing the timestamp silently turns every in-flight
        # `verify()` into a RuntimeError.
        self._initialized = False
        self._fetched_at: float | None = None
        # And a third: when a fetch was last *attempted*, which is not when one
        # last succeeded. The cooldown is measured from the attempt, so a
        # failing endpoint is retried on a timer instead of once per request.
        # A startup fetch counts as an attempt, which is what covers the window
        # right after the process comes up.
        self._last_refresh_attempt: float | None = None
        # Constructed eagerly: since 3.10 `asyncio.Lock` binds to the running
        # loop on first use rather than at construction, so a verifier built
        # outside a loop (the composition point does exactly that) is fine.
        self._refresh_lock = asyncio.Lock()
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
        Safe to call again over a live cache: a failure leaves the previous key
        set serving requests untouched.
        """
        try:
            metadata = await self._get_json(self._discovery_url, "OIDC discovery")
            jwks_uri = self._jwks_uri(metadata)
            document = await self._get_json(jwks_uri, "JWKS")
            keys = self._parse_keys(document)
        except TokenVerifierStartupError:
            # Only when the verifier never came up. A first startup that fails
            # is terminal — nobody will use this object again, so an owned
            # client would leak for the life of the process. A *re*-initialize
            # that fails is not terminal: the previous key set is still live and
            # still serving, so closing the client would leave a verifier that
            # answers `verify()` correctly but can never refresh again, and
            # whose next `initialize()` dies on httpx's RuntimeError for a
            # closed client — which is neither HTTPError nor InvalidURL, and so
            # escapes `_get_json` uncaught.
            if not self._initialized:
                await self.aclose()
            raise
        # Order matters: the cache is published first and the readiness latch
        # is set last, so no `verify()` can be admitted against a key set that
        # is not yet live. The attempt timestamp starts the cooldown here
        # rather than at the first refresh — a startup fetch is a fetch, and
        # without this the minute after startup would be the one minute an
        # attacker could drive a refresh with a single forged `kid`.
        self._keys = keys
        self._fetched_at = self._clock()
        self._last_refresh_attempt = self._fetched_at
        self._initialized = True

    async def verify(self, token: str) -> Mapping[str, Any]:
        """Return the token's claims, or raise `TokenInvalidError`.

        Checks the untrusted header before touching the key cache: a token
        naming an algorithm we do not accept is rejected without ever being
        looked up, which is what stops an attacker from using algorithm
        confusion (`none`, or HS256 keyed on the public key) to reach the
        verification path at all. That ordering does double duty now that a
        cache miss can cost a network request — the header screens are also
        what keep a junk token from reaching `_refresh_keys`.
        """
        # A programming error, not a token problem, so it is not
        # `TokenInvalidError`: an uninitialized verifier has an empty cache and
        # would otherwise reject every token as an unknown `kid` — a 401 storm
        # that looks exactly like a client at fault, with nothing in the log
        # pointing at the wiring. Checked ahead of the header screen because it
        # is a fact about this object, not about the token, and should not
        # depend on what the caller happened to present. Reads the latch, not
        # the cache timestamp, so refresh timing can never revoke readiness.
        if not self._initialized:
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

        # Both refresh triggers sit *below* the header screens, so a token we
        # were never going to accept — unsigned, HS256, no `kid` — cannot
        # reach the network at all.
        #
        # The lookup comes first, and the two triggers are deliberately not
        # symmetric. A miss has nothing to serve, so waiting for the fetch is
        # the only way that caller ever gets an answer: full blocking
        # single-flight. A hit can be answered right now, so the age-triggered
        # refresh is opportunistic — it is taken only when nobody else is
        # already fetching. Refreshing on age *before* the lookup would put
        # every request behind one 20-second round trip on a dead provider,
        # including the ones holding a key the cache can serve, which would
        # turn a provider outage into the authentication outage this module
        # promises it is not.
        key = self._keys.get(kid)
        if key is None:
            await self._refresh_keys(kid)
            key = self._keys.get(kid)
        elif self._is_stale() and not self._refresh_lock.locked():
            # `locked()` is a cheaper question than "will acquiring block",
            # and deliberately so. It reads `_locked` alone, while
            # `Lock.acquire`'s fast path also demands an empty (or fully
            # cancelled) waiter queue — and `release()` clears `_locked`
            # *before* the woken waiter sets it again. So there is a real
            # window where this reads False and the acquire below still
            # queues behind a draining waiter list.
            #
            # That window costs event-loop ticks rather than a round trip.
            # Waiters only ever queue here through the unconditional miss path
            # above, and by the time any of them is woken the holder has
            # already stamped `_last_refresh_attempt` — so each returns at a
            # guard without touching the network, provided the refresh that
            # stamped it is still inside the cooldown. In practice it is: both
            # legs carry `HTTP_TIMEOUT_SECONDS`, putting a refresh near 20s
            # against a 60s window. That is a practical bound and not a
            # guaranteed one — httpx's timeout is per-operation, not a total
            # deadline, so a provider trickling bytes could hold a read open
            # past the window. The cost if it ever happens is bounded anyway:
            # this one request blocks on a real fetch, which is exactly the
            # pre-reorder behaviour, for itself instead of for everyone.
            #
            # What this check does buy is the thing that matters: while a
            # fetch is genuinely in flight, a request holding a key we can
            # serve is never parked behind it.
            await self._refresh_keys(None)
            # Re-read. This request paid for the fetch, so it is holding a
            # fresher key set than the lookup above saw, and answering it from
            # the older one would mean honouring a key we have *just learned*
            # the tenant withdrew. This keeps the accept/reject outcome **for
            # this request** identical to refreshing ahead of the lookup;
            # requests served from cache while the fetch was in flight are the
            # case where the two orderings genuinely differ.
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
                # `require` pins all three as contract requirements. The one
                # it protects on its own is `exp`: without the option, a token
                # carrying no `exp` at all is accepted as never-expiring.
                # `iss` and `aud` would still be rejected, because the
                # issuer=/audience= arguments above make PyJWT demand them —
                # for those two the option restates the contract rather than
                # being the only thing enforcing it.
                options={"require": ["exp", "iss", "aud"]},
            )
        except (PyJWTError, TypeError, ValueError) as exc:
            raise TokenInvalidError("token failed verification") from exc
        return claims

    def _is_stale(self) -> bool:
        fetched_at = self._fetched_at
        if fetched_at is None:
            return True
        return self._clock() - fetched_at >= JWKS_MAX_AGE_SECONDS

    async def _refresh_keys(self, kid: str | None) -> None:
        """Re-fetch the key set, at most once per cooldown, never concurrently.

        One path for both triggers, because they need the same two mitigations
        and a second path would be a second chance to get them wrong.

        The threat this is shaped around: `kid` is attacker-controlled, so a
        refresh on every unknown one turns any unauthenticated client into an
        outbound request amplifier aimed at Microsoft — no credentials needed,
        one forged header per request. The cooldown is what bounds that, and it
        bounds it on its own: the attempt is stamped before the await, so a
        simultaneous burst finds the window already closed. The bound is two
        requests per window per verifier, not one — this re-reads discovery
        before the keys, because a rotation can move `jwks_uri` and a cached
        one would be the stale half of a key rotation.

        The lock is not a second copy of that guarantee. It is what makes the
        waiters *wait*. Without it they would leave this method the moment the
        cooldown turned them away and read a cache the in-flight refresh has
        not published yet — so a real rotation would answer 401 to every
        legitimate request that arrived alongside the first one, while the
        call count stayed reassuringly at one.

        A failure is deliberately swallowed. The caller is `verify()`, the
        cache it already has is still good, and a provider outage must not
        become an authentication outage here — a known key keeps verifying,
        while an unknown one stays `TokenInvalidError` because the lookup that
        follows this call simply misses again.
        """
        # Nothing to refresh through a client we already closed. httpx answers
        # a closed client with a bare `RuntimeError`, which is neither
        # `HTTPError` nor `InvalidURL` and so escapes `_get_json` and the
        # handler below — turning a shutdown-time cache miss into a 500 for
        # what is really just an unknown key. Declining here lets the lookup
        # miss cleanly and answer 401. Both flags are needed: `_closed` is
        # ours and covers an owned client, `is_closed` is httpx's and covers
        # an injected one the caller closed behind our back (it is False for a
        # client that was never opened, so it costs nothing otherwise).
        if self._closed or self._client.is_closed:
            return

        async with self._refresh_lock:
            # Re-read every condition *inside* the lock: each was last
            # evaluated before waiting, and the waiter ahead may have already
            # done the work. All three are refusals, so their relative order is
            # not observable from outside — but each is independently
            # load-bearing. Drop the first and a waiter queued behind a refresh
            # slower than the cooldown finds the window reopened and starts a
            # second fetch for a key it is already holding.
            if kid is not None and kid in self._keys:
                return
            if kid is None and not self._is_stale():
                return
            now = self._clock()
            if (
                self._last_refresh_attempt is not None
                and now - self._last_refresh_attempt < JWKS_REFRESH_COOLDOWN_SECONDS
            ):
                return

            # Before the await, not after: a refresh that only recorded its
            # successes would leave every failure un-rate-limited, which is
            # precisely the case an attacker (unknown `kid`, nothing to find)
            # and an outage (endpoint down) both produce.
            self._last_refresh_attempt = now
            try:
                metadata = await self._get_json(self._discovery_url, "OIDC discovery")
                jwks_uri = self._jwks_uri(metadata)
                document = await self._get_json(jwks_uri, "JWKS")
                keys = self._parse_keys(document)
            except TokenVerifierStartupError as exc:
                # The class is always the same one; the message is what says
                # which stage failed, and it is ours — every one of them is a
                # fixed string, so no provider payload reaches this line.
                logger.warning(
                    "JWKS refresh failed exception=%s reason=%s", type(exc).__name__, exc
                )
                return
            # Same publication rule as `initialize()`: a fully-built local dict
            # replaces the live one in a single rebind, so no request can ever
            # be served from a partially-parsed key set. The existing cache is
            # untouched until this line, which is what makes the failure return
            # above safe.
            self._keys = keys
            self._fetched_at = self._clock()

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
