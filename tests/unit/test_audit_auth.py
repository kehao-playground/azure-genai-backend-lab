"""``auth.rejected`` emission from `require_principal` (Day 22 Task 5).

Every 401/403 the dependency raises now also emits exactly one `auth.rejected`
audit event before the HTTPException reaches the client — covered call site by
call site, in both `AUTH_MODE`s, so a regression in the reason mapping (not
just "some 401 emits something") fails a test. A successful request emits no
`auth.rejected` at all.

Entra mode is exercised through `EntraJwtPrincipalResolver` wired to a stub
verifier (mirrors `test_auth_endpoints.py`'s `_StubResolver` pattern one layer
down): the cryptography is `services/entra_jwt`'s own test surface
(`test_entra_jwt.py`), not this dependency's, so nothing here touches a real
JWKS endpoint or a real signature.
"""

import logging
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import IDENTITY, audit_events

from azgenai_lab.api.principal import EntraJwtPrincipalResolver
from azgenai_lab.core.config import Settings
from azgenai_lab.main import app
from azgenai_lab.services.entra_jwt import TokenInvalidError

STUB_TID = "11111111-1111-1111-1111-111111111111"
STUB_OID = "33333333-3333-3333-3333-333333333333"
STUB_AUDIENCE = "22222222-2222-2222-2222-222222222222"
# Not a real credential — just the one value this stub verifier recognizes.
VALID_STUB_TOKEN = "stub-token"  # noqa: S105


class _StubVerifier:
    """Claims for the one token this suite mints; `TokenInvalidError` for
    anything else. The real verifier's rejection matrix (expiry, signature,
    issuer, ...) is `test_entra_jwt.py`'s job — this dependency only needs
    "verified" and "not verified" to exercise its own reason mapping."""

    async def verify(self, token: str) -> dict[str, object]:
        if token != VALID_STUB_TOKEN:
            raise TokenInvalidError("stub: unrecognized token")
        return {"tid": STUB_TID, "oid": STUB_OID, "scp": "access_as_user"}

    async def aclose(self) -> None:
        return None


def _entra_app(monkeypatch: pytest.MonkeyPatch, *, required_scope: str | None) -> FastAPI:
    """An Entra-mode app with a working (stubbed) resolver installed directly
    — the lifespan is never run, so `build_entra_resolver`'s real JWKS
    discovery never fires (see `_entra_mode_app` in test_auth_endpoints.py for
    the same pattern without a working resolver)."""
    from azgenai_lab import main as main_module

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            auth_mode="entra",
            entra_tenant_id=STUB_TID,
            entra_audience=STUB_AUDIENCE,
            entra_required_scope=required_scope,
        ),
    )
    app_under_test = main_module.create_app()
    app_under_test.state.principal_resolver = EntraJwtPrincipalResolver(
        app_under_test.state.settings, _StubVerifier()  # type: ignore[arg-type]
    )
    return app_under_test


@pytest.fixture
def client() -> Generator[TestClient]:
    """Shadows the shared `client` fixture (tests/conftest.py) for this
    module: that one carries default `X-Tenant-Id`/`X-User-Id` headers so
    every other suite's happy path doesn't have to repeat them, which is
    exactly wrong here — the header-boundary tests below need a request that
    carries precisely what each call specifies, nothing implied. Same
    no-defaults construction as `bare_client` in test_auth_endpoints.py."""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def entra_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Bare TestClient, deliberately not a context manager: entering one would
    # run the entra-mode lifespan, which would try a real JWKS discovery
    # request and overwrite the stub resolver installed above.
    return TestClient(_entra_app(monkeypatch, required_scope="access_as_user"))


@pytest.fixture
def entra_client_no_scope(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # A scope the stub token's `scp` claim does not carry, so a verified
    # caller still fails the permission gate — the 403 path.
    return TestClient(_entra_app(monkeypatch, required_scope="admin_only"))


def test_headers_missing_emits_401_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": "hi"})
    assert response.status_code == 401
    [event] = audit_events(caplog)
    assert (event["event"], event["http_status"], event["reason"]) == \
        ("auth.rejected", 401, "headers_missing")
    assert event["tenant_id"] is None and event["user_id"] is None
    assert event["path"] == "/api/v1/chat" and event["auth_mode"] == "headers"


def test_headers_duplicate_is_headers_invalid(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/chat", json={"message": "hi"},
            headers=[("X-Tenant-Id", "a"), ("X-Tenant-Id", "b"), ("X-User-Id", "u")])
    assert response.status_code == 401
    [event] = audit_events(caplog)
    assert event["reason"] == "headers_invalid"


def test_401_envelope_and_challenge_unchanged(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "hi"})
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


def test_entra_no_authorization_is_bearer_missing(
    entra_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = entra_client.post("/api/v1/chat", json={"message": "hi"})
    assert response.status_code == 401
    [event] = audit_events(caplog)
    assert event["reason"] == "bearer_missing" and event["auth_mode"] == "entra"


def test_entra_bad_token_is_token_invalid(
    entra_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = entra_client.post("/api/v1/chat", json={"message": "hi"},
                                     headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    [event] = audit_events(caplog)
    assert event["reason"] == "token_invalid"
    assert event["tenant_id"] is None and event["user_id"] is None


def test_entra_verified_without_permission_is_403_with_identity(
    entra_client_no_scope: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = entra_client_no_scope.post(
            "/api/v1/chat", json={"message": "hi"},
            headers={"Authorization": f"Bearer {VALID_STUB_TOKEN}"})
    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope"'
    [event] = audit_events(caplog)
    assert (event["http_status"], event["reason"]) == (403, "permission_missing")
    assert event["tenant_id"] == STUB_TID and event["user_id"] == STUB_OID


def test_successful_request_emits_no_auth_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    assert response.status_code == 200
    assert not [e for e in audit_events(caplog) if e["event"] == "auth.rejected"]
