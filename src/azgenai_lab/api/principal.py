"""``require_principal``: the identity boundary for protected endpoints (Day 15;
``X-User-Id`` required from Day 19).

Reads trusted gateway headers (``X-Tenant-Id`` / ``X-User-Id`` /
``X-Group-Ids``) — Day 19 also adds an Entra JWT resolver behind the same
dependency shape, producing the same ``Principal``. Every parsing violation
maps to 401 ``unauthorized`` with a ``WWW-Authenticate: Bearer`` challenge,
never 422: header syntax is not request-body validation, and a 422 here would
leak the distinction between "who are you" and "what did you ask for".

An async-generator dependency (not a plain function) so the tenant/user
context is set for the *entire* request lifetime, including a streaming
response body — ``scope="request"`` on the callers' ``Depends(...)`` keeps the
ContextVars alive until the response finishes, not just until the handler
returns.
"""

import logging
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from pydantic import ValidationError

from azgenai_lab.core.tenant_context import tenant_id_var, user_id_var
from azgenai_lab.models.principal import Principal

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


def unauthorized() -> HTTPException:
    """401 through the shared envelope, with the RFC 7235 challenge header
    that makes this an authentication (not authorization) failure. The
    generic message deliberately doesn't name a mechanism (headers vs. JWT):
    Day 19 adds a second resolver behind this same dependency, and a message
    naming one would be wrong the moment the other one fails instead."""
    return HTTPException(
        status_code=401,
        detail={
            "code": "unauthorized",
            "message": "Missing or invalid credentials.",
        },
        headers={"WWW-Authenticate": "Bearer"},
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


async def require_principal(request: Request) -> AsyncIterator[Principal]:
    tenant_id = _parse_tenant_id(request)
    user_id = _parse_user_id(request)
    group_ids = _parse_group_ids(request)

    try:
        principal = Principal(tenant_id=tenant_id, user_id=user_id, group_ids=group_ids)
    except (ValidationError, ValueError):
        raise unauthorized() from None

    tenant_token = tenant_id_var.set(principal.tenant_id)
    user_token = user_id_var.set(principal.user_id)
    logger.info("identity resolved")
    try:
        yield principal
    finally:
        user_id_var.reset(user_token)
        tenant_id_var.reset(tenant_token)
