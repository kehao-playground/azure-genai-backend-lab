"""The two `PrincipalResolver` adapters and the HTTP contract around them.

Nothing here touches a network or a real key: the Entra resolver is driven by
a `FakeVerifier` standing in for the cryptographic boundary, which is exactly
the seam this layer owns. `tests/unit/test_entra_jwt.py` proves a token was
signed by the tenant; this file proves what the claims inside a *verified*
token are allowed to mean — tenant binding, identity mapping, groups overage,
and the delegated-versus-app-only permission gate — plus the parsing of the
`Authorization` header that happens before any of it.

The split matters for one case in particular: `verify()` can raise three
different things and only one of them is a client's fault. `TokenInvalidError`
is a 401; a bare `RuntimeError` (uninitialized verifier) and
`TokenVerifierStartupError` (which subclasses `RuntimeError`) are wiring and
deployment faults that must stay loud, because catching them here would turn a
deliberately fatal condition back into a 401 storm that reads as a client
problem.
"""

import ast
import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request

from azgenai_lab.api import principal as principal_module
from azgenai_lab.api.principal import (
    MAX_AUTHORIZATION_HEADER_BYTES,
    EntraJwtPrincipalResolver,
    HeaderPrincipalResolver,
    UninitializedResolver,
    build_entra_resolver,
    build_initial_resolver,
    insufficient_scope,
)
from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.entra_jwt import (
    EntraTokenVerifier,
    TokenInvalidError,
    TokenVerifierStartupError,
)

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "99999999-9999-9999-9999-999999999999"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
USER_ID = "33333333-3333-3333-3333-333333333333"
REQUIRED_SCOPE = "access_as_user"
REQUIRED_ROLE = "Api.Access"

BEARER_HEADER = [(b"authorization", b"Bearer token")]

DELEGATED_CLAIMS: dict[str, Any] = {
    "tid": TENANT_ID,
    "oid": USER_ID,
    "groups": [],
    "scp": REQUIRED_SCOPE,
}
APP_ONLY_CLAIMS: dict[str, Any] = {
    "tid": TENANT_ID,
    "oid": USER_ID,
    "roles": [REQUIRED_ROLE],
}


class FakeVerifier:
    def __init__(self, claims: Mapping[str, Any] | Exception) -> None:
        self.claims = claims
        self.close_count = 0

    async def verify(self, token: str) -> Mapping[str, Any]:
        if isinstance(self.claims, Exception):
            raise self.claims
        return self.claims

    async def aclose(self) -> None:
        self.close_count += 1


def request_with(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    })


def _settings(**overrides: Any) -> Settings:
    """Entra-mode settings built from literals, never the ambient `.env`.

    Both permissions are configured by default so the delegated/app-only gate
    tests exercise the interesting case — a token that *could* satisfy the
    other gate if the two were allowed to cross-fall-back.
    """
    values: dict[str, Any] = {
        "auth_mode": "entra",
        "entra_tenant_id": TENANT_ID,
        "entra_audience": AUDIENCE,
        "entra_required_scope": REQUIRED_SCOPE,
        "entra_required_app_role": REQUIRED_ROLE,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def _resolve(
    claims: Mapping[str, Any] | Exception,
    *,
    settings: Settings | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Principal:
    resolver = EntraJwtPrincipalResolver(
        settings if settings is not None else _settings(),
        FakeVerifier(claims),  # type: ignore[arg-type]
    )
    return await resolver.resolve(request_with(headers if headers is not None else BEARER_HEADER))


async def _expect_status(
    claims: Mapping[str, Any] | Exception,
    status: int,
    *,
    settings: Settings | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await _resolve(claims, settings=settings, headers=headers)
    assert caught.value.status_code == status
    if status == 401:
        assert caught.value.detail == {
            "code": "unauthorized",
            "message": "Missing or invalid credentials.",
        }
        assert caught.value.headers == {"WWW-Authenticate": "Bearer"}
    return caught.value


# ---------------------------------------------------------------------------
# Authorization header parsing. Every violation is 401 before the verifier is
# ever consulted; the claims below are valid, so anything reaching `verify()`
# would succeed — which is what makes these assertions discriminating.
# ---------------------------------------------------------------------------

_BAD_AUTHORIZATION_HEADERS: dict[str, list[tuple[bytes, bytes]]] = {
    "absent": [],
    "two_headers": [(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")],
    "wrong_scheme": [(b"authorization", b"Basic dXNlcjpwdw==")],
    "no_scheme": [(b"authorization", b"token")],
    "empty_value": [(b"authorization", b"")],
    "empty_token": [(b"authorization", b"Bearer ")],
    "two_spaces": [(b"authorization", b"Bearer  token")],
    "tab_separator": [(b"authorization", b"Bearer\ttoken")],
    "trailing_space": [(b"authorization", b"Bearer token ")],
    "leading_space": [(b"authorization", b" Bearer token")],
}


@pytest.mark.parametrize("case", list(_BAD_AUTHORIZATION_HEADERS))
async def test_malformed_authorization_header_is_401(case: str) -> None:
    await _expect_status(DELEGATED_CLAIMS, 401, headers=_BAD_AUTHORIZATION_HEADERS[case])


@pytest.mark.parametrize("scheme", [b"Bearer", b"bearer", b"BEARER", b"BeArEr"])
async def test_bearer_scheme_is_case_insensitive(scheme: str) -> None:
    principal = await _resolve(
        DELEGATED_CLAIMS, headers=[(b"authorization", scheme + b" token")]
    )

    assert principal.user_id == USER_ID


async def test_header_value_at_the_cap_is_accepted() -> None:
    # The bound is the raw header value *including* the "Bearer " prefix, so
    # the token body is the cap minus the prefix. At exactly the cap the
    # request is parsed and reaches the (fake) verifier.
    token = b"t" * (MAX_AUTHORIZATION_HEADER_BYTES - len(b"Bearer "))
    header = b"Bearer " + token
    assert len(header) == MAX_AUTHORIZATION_HEADER_BYTES == 16384

    principal = await _resolve(DELEGATED_CLAIMS, headers=[(b"authorization", header)])

    assert principal.user_id == USER_ID


async def test_header_value_one_byte_over_the_cap_is_401() -> None:
    # One byte more, and the claims behind it never matter — the fake verifier
    # would have accepted this token, so a 401 here can only come from the cap.
    token = b"t" * (MAX_AUTHORIZATION_HEADER_BYTES - len(b"Bearer ") + 1)
    header = b"Bearer " + token
    assert len(header) == MAX_AUTHORIZATION_HEADER_BYTES + 1 == 16385

    await _expect_status(DELEGATED_CLAIMS, 401, headers=[(b"authorization", header)])


# ---------------------------------------------------------------------------
# The verifier boundary: one of its three exception types is the client's
# fault. The other two are ours and must not be laundered into a 401.
# ---------------------------------------------------------------------------


async def test_token_invalid_error_is_a_generic_401() -> None:
    exception = await _expect_status(TokenInvalidError("token failed verification"), 401)

    # No mechanism, no reason, nothing from the token: an unauthenticated
    # caller learning *which* check failed is free reconnaissance.
    assert "verification" not in str(exception.detail)


async def test_rejection_logs_only_the_exception_class(caplog: pytest.LogCaptureFixture) -> None:
    secret_token = b"Bearer supersecrettokenvalue"
    with caplog.at_level(logging.INFO, logger="azgenai_lab.api.principal"):
        await _expect_status(
            TokenInvalidError("expired at 2026-08-03"),
            401,
            headers=[(b"authorization", secret_token)],
        )

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "TokenInvalidError" in rendered
    assert "supersecrettokenvalue" not in rendered
    assert "expired at" not in rendered


async def test_uninitialized_verifier_runtime_error_propagates() -> None:
    # A wiring bug. Converting it to 401 would make every request look like a
    # bad client while the real cause — a verifier nobody initialized — leaves
    # no trace at all.
    with pytest.raises(RuntimeError, match="initialize"):
        await _resolve(RuntimeError("EntraTokenVerifier.initialize() has not completed"))


async def test_startup_error_propagates_even_though_it_is_a_runtime_error() -> None:
    # TokenVerifierStartupError subclasses RuntimeError, so an `except
    # RuntimeError` written to "be safe" would swallow this one too.
    with pytest.raises(TokenVerifierStartupError):
        await _resolve(TokenVerifierStartupError("JWKS request failed"))


# ---------------------------------------------------------------------------
# Claims -> Principal. Fail-closed: anything missing, malformed, or from the
# wrong tenant is 401, and all of it is decided before permissions are.
# ---------------------------------------------------------------------------


async def test_entra_resolver_ignores_day_15_identity_headers() -> None:
    # The mirror of the sentinel's rule: with a token present, spoofed
    # `X-Tenant-Id` / `X-User-Id` headers must contribute nothing. Identity
    # comes from the verified claims or from nowhere.
    principal = await _resolve(
        DELEGATED_CLAIMS,
        headers=[
            (b"authorization", b"Bearer token"),
            (b"x-tenant-id", b"attacker"),
            (b"x-user-id", b"attacker"),
            (b"x-group-ids", b"admins"),
        ],
    )

    assert principal == Principal(tenant_id=TENANT_ID, user_id=USER_ID, group_ids=())


async def test_delegated_and_app_only_produce_the_same_principal() -> None:
    delegated = await _resolve(DELEGATED_CLAIMS)
    app_only = await _resolve(APP_ONLY_CLAIMS)

    assert delegated == Principal(tenant_id=TENANT_ID, user_id=USER_ID, group_ids=())
    assert app_only == delegated


_BAD_IDENTITY_CLAIMS: dict[str, dict[str, Any]] = {
    "missing_tid": {"oid": USER_ID, "scp": REQUIRED_SCOPE},
    "null_tid": {"tid": None, "oid": USER_ID, "scp": REQUIRED_SCOPE},
    "non_string_tid": {"tid": 1234, "oid": USER_ID, "scp": REQUIRED_SCOPE},
    "missing_oid": {"tid": TENANT_ID, "scp": REQUIRED_SCOPE},
    "null_oid": {"tid": TENANT_ID, "oid": None, "scp": REQUIRED_SCOPE},
    "non_string_oid": {"tid": TENANT_ID, "oid": ["a"], "scp": REQUIRED_SCOPE},
    "malformed_oid": {"tid": TENANT_ID, "oid": "not a valid id!", "scp": REQUIRED_SCOPE},
    "oid_too_long": {"tid": TENANT_ID, "oid": "u" * 65, "scp": REQUIRED_SCOPE},
    "empty_oid": {"tid": TENANT_ID, "oid": "", "scp": REQUIRED_SCOPE},
}


@pytest.mark.parametrize("case", list(_BAD_IDENTITY_CLAIMS))
async def test_missing_or_malformed_identity_claims_are_401(case: str) -> None:
    await _expect_status(_BAD_IDENTITY_CLAIMS[case], 401)


async def test_token_from_another_tenant_is_401() -> None:
    # The single owner of the tid comparison. The verifier deliberately does
    # not do it, so deleting this check is a tenant bypass with a valid
    # signature.
    claims = {**DELEGATED_CLAIMS, "tid": OTHER_TENANT_ID}

    await _expect_status(claims, 401)


async def test_configured_tenant_is_compared_exactly() -> None:
    # A prefix of the configured GUID is not the configured GUID.
    claims = {**DELEGATED_CLAIMS, "tid": TENANT_ID[:-1]}

    await _expect_status(claims, 401)


_GROUP_CLAIM_CASES: dict[str, tuple[dict[str, Any], tuple[str, ...] | None]] = {
    "absent": ({}, ()),
    "empty": ({"groups": []}, ()),
    "sorted_and_deduplicated": ({"groups": ["g2", "g1", "g2", "g10"]}, ("g1", "g10", "g2")),
    # None means "check the count instead", for a case whose expected value
    # would otherwise be a hundred literals.
    "at_the_limit": ({"groups": [f"g{i}" for i in range(100)]}, None),
    # `_claim_names` is a pointer: an entry for something other than groups
    # is not an overage signal, and a null points at nothing at all. Compare
    # `hasgroups`, a flag, where null *is* refused — see the rejection table.
    "unrelated_claim_names": ({"_claim_names": {"upn": "src1"}}, ()),
    "null_claim_names": ({"_claim_names": None}, ()),
    # The one value of the flag that means "no overage".
    "hasgroups_false": ({"hasgroups": False}, ()),
}


@pytest.mark.parametrize("case", list(_GROUP_CLAIM_CASES))
async def test_accepted_group_claim_shapes(case: str) -> None:
    extra, expected = _GROUP_CLAIM_CASES[case]
    claims = {"tid": TENANT_ID, "oid": USER_ID, "scp": REQUIRED_SCOPE, **extra}

    principal = await _resolve(claims)

    if expected is None:
        assert len(principal.group_ids) == 100
    else:
        assert principal.group_ids == expected


_BAD_GROUP_CLAIMS: dict[str, dict[str, Any]] = {
    "non_list_string": {"groups": "g1,g2"},
    "non_list_mapping": {"groups": {"0": "g1"}},
    "non_string_member": {"groups": ["g1", 2]},
    "malformed_member": {"groups": ["not a valid id!"]},
    "over_the_limit": {"groups": [f"g{i}" for i in range(101)]},
    # 101 entries that deduplicate to one. The limit counts what arrived, not
    # what survives deduplication — verified against both layers that enforce
    # it (the resolver's cap and Principal's own validator, which also counts
    # before deduplicating).
    "over_the_limit_after_dedup": {"groups": ["g1"] * 101},
    # Overage: Entra stops emitting `groups` past 200 and points at Graph
    # instead. Silently reading that as "no groups" would demote the user's
    # ACL with no error anywhere.
    "claim_names_groups": {"_claim_names": {"groups": "src1"}},
    "claim_names_not_a_mapping": {"_claim_names": "groups"},
    "claim_names_is_a_list": {"_claim_names": ["groups"]},
    "hasgroups_true": {"hasgroups": True},
    "hasgroups_malformed_string": {"hasgroups": "true"},
    "hasgroups_malformed_number": {"hasgroups": 1},
    # A present flag whose value is not `False`, by the same rule as the two
    # above: the issuer said something about groups and we cannot read it.
    "hasgroups_null": {"hasgroups": None},
}


@pytest.mark.parametrize("case", list(_BAD_GROUP_CLAIMS))
async def test_bad_or_overage_group_claims_are_401(case: str) -> None:
    claims = {"tid": TENANT_ID, "oid": USER_ID, "scp": REQUIRED_SCOPE, **_BAD_GROUP_CLAIMS[case]}

    await _expect_status(claims, 401)


@pytest.mark.parametrize("case", ["claim_names_groups", "hasgroups_true"])
async def test_overage_is_401_before_the_permission_gate(case: str) -> None:
    # Both faults at once: a malformed identity and a scope that would be
    # refused. 401 must win — a 403 here would mean the resolver decided
    # permissions on claims it had already found untrustworthy.
    claims = {
        "tid": TENANT_ID,
        "oid": USER_ID,
        "scp": "wrong_scope",
        **_BAD_GROUP_CLAIMS[case],
    }

    await _expect_status(claims, 401)


# ---------------------------------------------------------------------------
# The permission gate: `scp` present selects delegated, absent selects
# app-only, and the two never cross.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scp", "expected"),
    [
        ("access_as_user", 200),
        ("openid profile access_as_user", 200),  # required scope among others
        ("access_as_user extra.scope", 200),  # position must not matter
        ("access_as_user_extended", 403),  # substring must NOT match
        ("openid profile", 403),
        ("", 403),
    ],
)
async def test_scp_is_a_whitespace_delimited_set(scp: str, expected: int) -> None:
    claims = {"tid": TENANT_ID, "oid": USER_ID, "scp": scp}

    if expected == 200:
        assert (await _resolve(claims)).user_id == USER_ID
        return
    exception = await _expect_status(claims, 403)
    assert exception.detail["code"] == "insufficient_scope"


async def test_delegated_token_cannot_fall_back_to_app_role() -> None:
    claims = {
        "tid": TENANT_ID,
        "oid": USER_ID,
        "groups": [],
        "scp": "wrong_scope",
        "roles": ["Api.Access"],
    }
    resolver = EntraJwtPrincipalResolver(_settings(), FakeVerifier(claims))  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as caught:
        await resolver.resolve(request_with([(b"authorization", b"Bearer token")]))
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "insufficient_scope"
    assert caught.value.headers == {
        "WWW-Authenticate": 'Bearer error="insufficient_scope"'
    }


async def test_role_only_configuration_rejects_a_delegated_token() -> None:
    # Configuring only an app role means delegated callers are refused, not
    # that the delegated check is skipped.
    settings = _settings(entra_required_scope=None)

    await _expect_status(DELEGATED_CLAIMS, 403, settings=settings)


async def test_scope_only_configuration_rejects_an_app_only_token() -> None:
    settings = _settings(entra_required_app_role=None)

    await _expect_status(APP_ONLY_CLAIMS, 403, settings=settings)


_INSUFFICIENT_PERMISSION_CLAIMS: dict[str, dict[str, Any]] = {
    "no_scp_no_roles": {},
    "empty_roles": {"roles": []},
    "wrong_role": {"roles": ["Other.Role"]},
    "role_substring": {"roles": ["Api.Access.Extended"]},
}


@pytest.mark.parametrize("case", list(_INSUFFICIENT_PERMISSION_CLAIMS))
async def test_missing_permissions_are_403(case: str) -> None:
    claims = {"tid": TENANT_ID, "oid": USER_ID, **_INSUFFICIENT_PERMISSION_CLAIMS[case]}

    exception = await _expect_status(claims, 403)

    assert exception.detail == {
        "code": "insufficient_scope",
        "message": "The credential lacks the required API permission.",
    }
    assert exception.headers == {"WWW-Authenticate": 'Bearer error="insufficient_scope"'}


_MALFORMED_PERMISSION_CLAIMS: dict[str, dict[str, Any]] = {
    "scp_is_a_list": {"scp": ["access_as_user"]},
    "scp_is_null": {"scp": None},
    "scp_is_a_number": {"scp": 1},
    "roles_is_a_string": {"roles": "Api.Access"},
    "roles_has_a_non_string": {"roles": ["Api.Access", 7]},
    "roles_is_a_mapping": {"roles": {"0": "Api.Access"}},
}


@pytest.mark.parametrize("case", list(_MALFORMED_PERMISSION_CLAIMS))
async def test_malformed_permission_claims_are_401(case: str) -> None:
    # A claim of the wrong type is a malformed token, not a permission
    # decision — there is nothing to evaluate.
    claims = {"tid": TENANT_ID, "oid": USER_ID, **_MALFORMED_PERMISSION_CLAIMS[case]}

    await _expect_status(claims, 401)


def test_insufficient_scope_challenge_is_exact() -> None:
    exception = insufficient_scope()

    assert exception.status_code == 403
    assert exception.detail == {
        "code": "insufficient_scope",
        "message": "The credential lacks the required API permission.",
    }
    assert exception.headers == {"WWW-Authenticate": 'Bearer error="insufficient_scope"'}


# ---------------------------------------------------------------------------
# Header resolver and lifecycle.
# ---------------------------------------------------------------------------


async def test_header_resolver_maps_the_day_15_headers() -> None:
    resolver = HeaderPrincipalResolver()

    principal = await resolver.resolve(
        request_with([
            (b"x-tenant-id", b"t1"),
            (b"x-user-id", b"u1"),
            (b"x-group-ids", b"g2, g1"),
        ])
    )

    assert principal == Principal(tenant_id="t1", user_id="u1", group_ids=("g1", "g2"))


async def test_header_resolver_still_rejects_bad_headers() -> None:
    resolver = HeaderPrincipalResolver()

    with pytest.raises(HTTPException) as caught:
        await resolver.resolve(request_with([(b"x-tenant-id", b"t1")]))

    assert caught.value.status_code == 401


async def test_header_resolver_close_is_a_no_op() -> None:
    # The lifespan closes whatever resolver it holds without branching on the
    # concrete type, so this has to be awaitable and harmless.
    resolver = HeaderPrincipalResolver()

    assert await resolver.aclose() is None
    assert await resolver.aclose() is None


async def test_entra_resolver_close_delegates_to_the_verifier() -> None:
    verifier = FakeVerifier(DELEGATED_CLAIMS)
    resolver = EntraJwtPrincipalResolver(_settings(), verifier)  # type: ignore[arg-type]

    await resolver.aclose()

    assert verifier.close_count == 1


# ---------------------------------------------------------------------------
# The two-stage factory pair.
# ---------------------------------------------------------------------------


class _ExplodingVerifier:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("build_initial_resolver must not construct a verifier")


def test_build_initial_resolver_returns_the_header_adapter_in_headers_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(principal_module, "EntraTokenVerifier", _ExplodingVerifier)

    resolver = build_initial_resolver(Settings(_env_file=None, auth_mode="headers"))

    assert isinstance(resolver, HeaderPrincipalResolver)


def test_build_initial_resolver_returns_the_sentinel_in_entra_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No verifier, therefore no httpx client and no discovery request:
    # app construction must stay synchronous and offline.
    monkeypatch.setattr(principal_module, "EntraTokenVerifier", _ExplodingVerifier)

    resolver = build_initial_resolver(_settings())

    assert isinstance(resolver, UninitializedResolver)


async def test_uninitialized_resolver_raises_rather_than_trusting_headers() -> None:
    # Deliberately handing it a request whose Day 15 identity headers are
    # perfectly valid: the failure must come from the missing wiring, not
    # from the request.
    resolver = UninitializedResolver()

    with pytest.raises(RuntimeError) as caught:
        await resolver.resolve(
            request_with([(b"x-tenant-id", b"t1"), (b"x-user-id", b"u1")])
        )

    assert not isinstance(caught.value, HTTPException)
    assert "lifespan" in str(caught.value)


async def test_uninitialized_resolver_close_is_a_no_op() -> None:
    assert await UninitializedResolver().aclose() is None


async def test_build_entra_resolver_returns_an_initialized_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized: list[EntraTokenVerifier] = []

    async def fake_initialize(self: EntraTokenVerifier) -> None:
        initialized.append(self)

    monkeypatch.setattr(EntraTokenVerifier, "initialize", fake_initialize)

    resolver = await build_entra_resolver(_settings())

    assert isinstance(resolver, EntraJwtPrincipalResolver)
    assert len(initialized) == 1
    await resolver.aclose()


async def test_build_entra_resolver_refuses_settings_without_a_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Settings' own model validator makes this unreachable in Entra mode, so
    # the narrowing exists for a caller handing over headers-mode Settings.
    # The exploding stand-in proves the refusal happens *before* a verifier
    # (and its httpx client) is built, which is what stops the branch from
    # leaking one on the way out.
    monkeypatch.setattr(principal_module, "EntraTokenVerifier", _ExplodingVerifier)

    with pytest.raises(ValueError, match="entra_tenant_id"):
        await build_entra_resolver(Settings(_env_file=None, auth_mode="headers"))


@pytest.mark.parametrize(
    "failure", [TokenVerifierStartupError("discovery failed"), asyncio.CancelledError()]
)
async def test_build_entra_resolver_closes_the_verifier_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    # CancelledError is in the matrix on purpose: it is a BaseException, so an
    # `except Exception` here would leak the httpx client on the one path
    # (shutdown during startup) where nobody is left to close it.
    closed: list[str] = []
    real_aclose = EntraTokenVerifier.aclose

    async def fail_initialize(self: EntraTokenVerifier) -> None:
        raise failure

    async def record_aclose(self: EntraTokenVerifier) -> None:
        closed.append("aclose")
        await real_aclose(self)

    monkeypatch.setattr(EntraTokenVerifier, "initialize", fail_initialize)
    monkeypatch.setattr(EntraTokenVerifier, "aclose", record_aclose)

    with pytest.raises(type(failure)):
        await build_entra_resolver(_settings())

    assert closed == ["aclose"]


# ---------------------------------------------------------------------------
# The interim resolver (Task 6 replaces it with app.state wiring).
# ---------------------------------------------------------------------------


def _build_interim_for(mode: str) -> object:
    """Re-derive the module-level interim resolver under a given AUTH_MODE.

    `get_settings()` is cached, so the environment is patched and the cache
    cleared on both sides — the surrounding suite runs in headers mode and
    must not inherit an Entra-mode Settings.

    The patching is scoped here rather than taken from the `monkeypatch`
    fixture so the final `cache_clear()` provably runs *after* the
    environment is restored: `return` inside the `with` triggers its exit
    first, then the `finally`. With fixture teardown the two orders depend on
    instantiation order, leaving a window in which anything calling
    `get_settings()` would cache Entra-mode settings for the rest of the
    session.
    """
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("AUTH_MODE", mode)
            patch.setenv("ENTRA_TENANT_ID", TENANT_ID)
            patch.setenv("ENTRA_AUDIENCE", AUDIENCE)
            patch.setenv("ENTRA_REQUIRED_SCOPE", REQUIRED_SCOPE)
            get_settings.cache_clear()
            return build_initial_resolver(get_settings())
    finally:
        get_settings.cache_clear()


def test_interim_resolver_is_not_header_trust_in_entra_mode() -> None:
    # With AUTH_MODE=entra, the module-level interim resolver must be the
    # sentinel, never a HeaderPrincipalResolver.
    assert isinstance(_build_interim_for("entra"), UninitializedResolver)


def test_interim_resolver_is_usable_in_headers_mode() -> None:
    assert isinstance(_build_interim_for("headers"), HeaderPrincipalResolver)


def test_the_settings_cache_survives_the_interim_probe() -> None:
    # The probe above is the only thing in the suite that clears the cache,
    # so this pins the half its own assertions cannot: whatever it leaves
    # behind must be the ambient headers-mode configuration.
    _build_interim_for("entra")

    assert get_settings().auth_mode == "headers"


def test_interim_resolver_is_assigned_from_the_mode_aware_factory() -> None:
    """The assertion above only reaches the factory; this one reaches the
    module-level assignment that uses it.

    Hard-coding `_interim_resolver = HeaderPrincipalResolver()` would leave
    every behavioural test in this file green while an `AUTH_MODE=entra`
    deployment silently accepted spoofable `X-Tenant-Id` headers as identity.
    The assignment is a single module-level statement executed at import, so
    the source is where it can be pinned without re-importing the module the
    routers already hold a reference into.

    The argument is pinned too, and it is not a formality: passing a
    literal `Settings(auth_mode="headers")` reaches the factory, satisfies
    every behavioural test in this file, and reintroduces exactly the same
    trust downgrade one level out.
    """
    tree = ast.parse(Path(principal_module.__file__ or "").read_text(encoding="utf-8"))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_interim_resolver"
            for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        )
    ]

    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Call)
    assert isinstance(value.func, ast.Name)
    assert value.func.id == "build_initial_resolver"

    # Positional or keyword is not the point and is not pinned; that the one
    # argument is the live `get_settings()` call is.
    arguments = [*value.args, *(keyword.value for keyword in value.keywords)]
    assert len(arguments) == 1
    argument = arguments[0]
    assert isinstance(argument, ast.Call)
    assert isinstance(argument.func, ast.Name)
    assert argument.func.id == "get_settings"


def test_bearer_scheme_never_errors_on_its_own() -> None:
    # auto_error=True would make FastAPI answer a missing or malformed
    # Authorization header itself, with its own status code and body, before
    # the resolver — including in headers mode, where the header is not used
    # at all.
    assert principal_module._bearer_scheme.auto_error is False
    assert principal_module._bearer_scheme.scheme_name == "bearerAuth"
