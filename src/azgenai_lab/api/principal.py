"""``require_principal``: the identity boundary for protected endpoints (Day 15;
``X-User-Id`` required and Entra ID resolution added Day 19).

Two adapters produce the same ``Principal`` behind one dependency:
``HeaderPrincipalResolver`` reads trusted gateway headers (``X-Tenant-Id`` /
``X-User-Id`` / ``X-Group-Ids``), ``EntraJwtPrincipalResolver`` reads the
claims of a cryptographically verified Entra access token. Which one is
installed is decided once, at startup, from ``AUTH_MODE`` — never per request,
and never by what a request happens to carry.

Every parsing violation maps to 401 ``unauthorized`` with a
``WWW-Authenticate: Bearer`` challenge, never 422: header syntax is not
request-body validation, and a 422 here would leak the distinction between
"who are you" and "what did you ask for". A *verified* token that simply lacks
the required API permission is the one case that is not 401 — that caller is
authenticated, so it gets 403 ``insufficient_scope``.

An async-generator dependency (not a plain function) so the tenant/user
context is set for the *entire* request lifetime, including a streaming
response body — ``scope="request"`` on the callers' ``Depends(...)`` keeps the
ContextVars alive until the response finishes, not just until the handler
returns.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, Protocol

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.core.tenant_context import tenant_id_var, user_id_var
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.entra_jwt import EntraTokenVerifier, TokenInvalidError

logger = logging.getLogger(__name__)

# The raw header value, before splitting into tokens; bounds the amount of
# work done parsing an attacker-controlled string before Principal even sees
# it. ASCII bytes: HTTP header field values are ISO-8859-1 by spec, so byte
# count and character count coincide for the identifiers this header carries.
MAX_GROUP_IDS_HEADER_BYTES = 4096

# Checked before Principal's own field_validator dedups: the field_validator
# guards the model invariant (deduplicated set), this guards the wire
# contract (a client sending >100 raw tokens is malformed input, regardless
# of how many survive deduplication).
MAX_GROUP_ID_TOKENS = 100

# Same idea one layer up, for the Entra path: the bound is on the *raw*
# ``Authorization`` value including the ``Bearer `` prefix, checked before any
# splitting — bounding the token body instead would mean splitting unbounded
# attacker input first. An Entra token carrying group claims is commonly
# 2-4 KB, so 16 KiB is a conservative ceiling rather than a tuning knob.
MAX_AUTHORIZATION_HEADER_BYTES = 16 * 1024


def unauthorized() -> HTTPException:
    """401 through the shared envelope, with the RFC 7235 challenge header
    that makes this an authentication (not authorization) failure. The
    generic message deliberately doesn't name a mechanism (headers vs. JWT)
    or a reason: two resolvers share this dependency, and telling an
    unauthenticated caller *which* check failed is free reconnaissance."""
    return HTTPException(
        status_code=401,
        detail={
            "code": "unauthorized",
            "message": "Missing or invalid credentials.",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def insufficient_scope() -> HTTPException:
    """403 for a verified credential without the required API permission.

    Distinct from 401 on purpose: the caller *is* authenticated, so retrying
    with the same token is pointless — the fix is a different token, which is
    what the RFC 6750 ``insufficient_scope`` challenge says.
    """
    return HTTPException(
        status_code=403,
        detail={
            "code": "insufficient_scope",
            "message": "The credential lacks the required API permission.",
        },
        headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
    )


def _parse_tenant_id(request: Request) -> str:
    values = request.headers.getlist("x-tenant-id")
    if len(values) != 1:
        raise unauthorized()
    return values[0]


def _parse_user_id(request: Request) -> str:
    values = request.headers.getlist("x-user-id")
    if len(values) != 1:
        raise unauthorized()
    return values[0]


def _parse_group_ids(request: Request) -> tuple[str, ...]:
    values = request.headers.getlist("x-group-ids")
    if len(values) > 1:
        raise unauthorized()
    if len(values) == 0:
        return ()

    raw = values[0]
    if not raw.strip(" \t"):
        return ()
    if len(raw.encode("ascii", errors="replace")) > MAX_GROUP_IDS_HEADER_BYTES:
        raise unauthorized()

    tokens = [token.strip(" \t") for token in raw.split(",")]
    if any(token == "" for token in tokens):
        raise unauthorized()
    if len(tokens) > MAX_GROUP_ID_TOKENS:
        raise unauthorized()
    return tuple(tokens)


def _parse_bearer_token(request: Request) -> str:
    """Return the token from exactly one well-formed ``Authorization`` header.

    Order is deliberate: the size cap is applied to the raw value first, so
    every step after it works on a bounded string. Splitting on a single ASCII
    space and requiring exactly two parts enforces the RFC 7235 credentials
    syntax in one comparison — no leading, trailing, doubled or tab
    separators, which are the shapes a permissive parser accepts and a
    proxy downstream might read differently.
    """
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise unauthorized()

    raw = values[0]
    if len(raw.encode("ascii", errors="replace")) > MAX_AUTHORIZATION_HEADER_BYTES:
        raise unauthorized()

    parts = raw.split(" ")
    if len(parts) != 2:
        raise unauthorized()
    scheme, token = parts
    # Case-insensitive per RFC 7235: the scheme is a token, not a literal.
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    return token


class PrincipalResolver(Protocol):
    async def resolve(self, request: Request) -> Principal: ...
    async def aclose(self) -> None: ...


class HeaderPrincipalResolver:
    """Trusted-gateway headers (Day 15). The gateway must strip client-supplied
    identity headers; nothing here can tell a spoofed one from a real one,
    which is exactly why the Entra adapter exists."""

    async def resolve(self, request: Request) -> Principal:
        tenant_id = _parse_tenant_id(request)
        user_id = _parse_user_id(request)
        group_ids = _parse_group_ids(request)
        try:
            return Principal(tenant_id=tenant_id, user_id=user_id, group_ids=group_ids)
        except (ValidationError, ValueError):
            raise unauthorized() from None

    async def aclose(self) -> None:
        return None


class EntraJwtPrincipalResolver:
    """Verified Entra ID token claims -> ``Principal``.

    The cryptography lives in ``services/entra_jwt``; what is left here is
    everything the signature does *not* tell you — which tenant the token is
    for, who the caller is, and whether the credential carries the permission
    this API requires.
    """

    def __init__(self, settings: Settings, verifier: EntraTokenVerifier) -> None:
        self._settings = settings
        self._verifier = verifier

    async def resolve(self, request: Request) -> Principal:
        token = _parse_bearer_token(request)

        try:
            claims = await self._verifier.verify(token)
        except TokenInvalidError as exc:
            # Only this one. `verify()` also raises a bare `RuntimeError` for
            # an uninitialized verifier and `TokenVerifierStartupError` (a
            # `RuntimeError` subclass) for a failed startup; both are our
            # faults, not the caller's, and both must reach the 500 handler
            # loudly. An `except Exception` or `except RuntimeError` here
            # would turn a misconfigured deployment into a 401 storm that
            # reads as a fleet of bad clients.
            logger.info("bearer token rejected exception=%s", type(exc).__name__)
            raise unauthorized() from None

        # Identity first, permissions second: claims we do not trust are not
        # claims we evaluate permissions on. An overage or a malformed `tid`
        # is 401 even when the scope would also have been refused.
        principal = self._principal_from(claims)
        self._require_permission(claims)
        return principal

    async def aclose(self) -> None:
        await self._verifier.aclose()

    def _principal_from(self, claims: Mapping[str, Any]) -> Principal:
        tenant_id = claims.get("tid")
        user_id = claims.get("oid")
        if not isinstance(tenant_id, str) or not isinstance(user_id, str):
            raise unauthorized()
        # The one and only `tid` comparison in the codebase (see the
        # `services/entra_jwt` docstring): the verifier's contract stops at
        # the signature and the registered claims, so if this line goes away
        # nothing else checks the tenant. `iss` pins the tenant too, which
        # makes this the second layer — not a duplicate of a check that lives
        # somewhere else.
        if tenant_id != self._settings.entra_tenant_id:
            raise unauthorized()

        group_ids = self._group_ids(claims)
        try:
            return Principal(tenant_id=tenant_id, user_id=user_id, group_ids=group_ids)
        except (ValidationError, ValueError):
            raise unauthorized() from None

    def _group_ids(self, claims: Mapping[str, Any]) -> tuple[str, ...]:
        """Group object ids, or 401 — never a silent empty tuple.

        Past 200 groups Entra stops emitting `groups` and points at Graph
        instead, through either `_claim_names.groups` or `hasgroups`. Reading
        that as "this user has no groups" would silently demote them: Day 15's
        ACL filter would hide documents they are entitled to, with no error
        anywhere to explain it. Resolving the overage properly needs a Graph
        lookup, which is out of scope, so the honest answer is to refuse.
        """
        hasgroups = claims.get("hasgroups")
        if hasgroups is not None and hasgroups is not False:
            # `is not False` rather than truthiness: a `hasgroups` of `"true"`
            # or `1` is a token shape we do not recognise, and guessing at an
            # overage signal's meaning is the one thing this must not do.
            raise unauthorized()

        claim_names = claims.get("_claim_names")
        if claim_names is not None:
            if not isinstance(claim_names, Mapping):
                raise unauthorized()
            if "groups" in claim_names:
                raise unauthorized()

        groups = claims.get("groups")
        if groups is None:
            return ()
        # The claim contract: a JSON array of strings, no more than the same
        # limit the header path uses. Principal rejects all three violations
        # too (its own validator counts before deduplicating and its field
        # type is `tuple[str, ...]`), so these are a second layer rather than
        # the only one — stated here because "what a `groups` claim may look
        # like" is this adapter's question, not the model's, and because the
        # model's answer currently rests on Pydantic's coercion table.
        if not isinstance(groups, list):
            raise unauthorized()
        if len(groups) > MAX_GROUP_ID_TOKENS:
            raise unauthorized()
        if any(not isinstance(group, str) for group in groups):
            raise unauthorized()
        return tuple(groups)

    def _require_permission(self, claims: Mapping[str, Any]) -> None:
        """Delegated and app-only, selected by the presence of `scp` and never
        allowed to satisfy each other.

        Entra can assign app roles to *users*, so a delegated token may carry
        `roles`. If a missing or wrong `scp` could fall through to the app-role
        gate, that user token would be admitted on a permission it was never
        granted as a delegated scope. The configured value that is `None` never
        matches anything — "not configured" means "this credential type is not
        accepted", not "not checked".
        """
        if "scp" in claims:
            scp = claims["scp"]
            if not isinstance(scp, str):
                raise unauthorized()
            required_scope = self._settings.entra_required_scope
            # `split()` with no argument splits on runs of whitespace and
            # drops empties, so set membership is exact: `access_as_user`
            # must be one of the space-delimited entries, not a substring of
            # one (`access_as_user_extended` is a different scope).
            if required_scope is None or required_scope not in set(scp.split()):
                raise insufficient_scope()
            return

        roles = claims.get("roles")
        if roles is not None and (
            not isinstance(roles, list) or any(not isinstance(role, str) for role in roles)
        ):
            raise unauthorized()
        required_role = self._settings.entra_required_app_role
        if required_role is None or roles is None or required_role not in roles:
            raise insufficient_scope()


class UninitializedResolver:
    """Installed in Entra mode before lifespan startup replaces it.

    Raises rather than returning a principal: reaching this means the app was
    served without running its lifespan, which is a deployment fault, not a
    caller fault. Never fall back to header trust here — that would silently
    turn a spoofable header into an accepted identity.
    """

    async def resolve(self, request: Request) -> Principal:
        raise RuntimeError(
            "principal resolver not initialized: application lifespan did not run"
        )

    async def aclose(self) -> None:
        return None


def build_initial_resolver(settings: Settings) -> PrincipalResolver:
    """The resolver installed at app construction. No I/O, no async, safe at
    import time — headers mode gets a working adapter (so a bare `TestClient`
    that never runs the lifespan still works), Entra mode gets the sentinel."""
    if settings.auth_mode == "headers":
        return HeaderPrincipalResolver()
    return UninitializedResolver()


async def build_entra_resolver(settings: Settings) -> EntraJwtPrincipalResolver:
    """The only async construction path; called from the lifespan alone."""
    tenant_id = settings.entra_tenant_id
    audience = settings.entra_audience
    if tenant_id is None or audience is None:
        # Unreachable in entra mode — Settings' own model validator rejects a
        # configuration missing either one at startup. This is the type
        # narrowing that fact needs, not a second copy of the policy.
        raise ValueError("entra mode requires entra_tenant_id and entra_audience")

    verifier = EntraTokenVerifier(tenant_id, audience)
    try:
        await verifier.initialize()
    except BaseException:
        # BaseException, not Exception: a cancellation during startup is
        # exactly when nobody is left to close the client we just opened.
        await verifier.aclose()
        raise
    return EntraJwtPrincipalResolver(settings, verifier)


# Documents the bearer credential in OpenAPI without letting FastAPI decide
# anything: `auto_error=False` means a missing or malformed header produces
# `None` here instead of FastAPI's own error response, so every rejection
# still comes from the resolver, through the shared envelope. The parsed
# credential is deliberately unused — the resolver re-reads the raw header,
# because the size cap and the exactly-one-header rule are ours to enforce.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="bearerAuth",
    description=(
        "Microsoft Entra ID Bearer token when AUTH_MODE=entra. "
        "Development AUTH_MODE=headers uses X-Tenant-Id, X-User-Id, and optional X-Group-Ids."
    ),
)

# Task 6 replaces this with `app.state.principal_resolver`. Until then one
# module-level instance, built through the *mode-aware* factory: a hard-coded
# `HeaderPrincipalResolver()` would make an `AUTH_MODE=entra` deployment
# silently accept unverified `X-Tenant-Id` headers as identity — a trust
# downgrade invisible from the outside, because every request still returns
# 200.
_interim_resolver: PrincipalResolver = build_initial_resolver(get_settings())


async def require_principal(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)],
) -> AsyncIterator[Principal]:
    principal = await _interim_resolver.resolve(request)

    tenant_token = tenant_id_var.set(principal.tenant_id)
    user_token = user_id_var.set(principal.user_id)
    logger.info("identity resolved")
    try:
        yield principal
    finally:
        user_id_var.reset(user_token)
        tenant_id_var.reset(tenant_token)
