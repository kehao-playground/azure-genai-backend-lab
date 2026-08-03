"""Identity-header parsing at the API boundary (Day 15; X-User-Id required
from Day 19).

``require_principal`` turns ``X-Tenant-Id`` / ``X-User-Id`` / ``X-Group-Ids``
into a ``Principal`` for the four protected endpoints. Every parsing
violation maps to 401 ``unauthorized`` (never 422): the bad-header matrix
below runs parameterized over all four endpoints with each endpoint's own
valid body, so a 422 from FastAPI's own validation can never masquerade as
the 401 the dependency is supposed to raise.
"""

import logging
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from azgenai_lab.api.principal import require_principal
from azgenai_lab.core.tenant_context import tenant_id_var, user_id_var
from azgenai_lab.main import app

_PROTECTED_CASES = [
    ("/api/v1/chat", {"message": "hello"}),
    ("/api/v1/chat/stream", {"message": "hello"}),
    ("/api/v1/rag", {"question": "hello"}),
    ("/api/v1/agent", {"task": "hello"}),
]


@pytest.fixture
def bare_client() -> Generator[TestClient]:
    """A client with no default headers, unlike the shared ``client`` fixture."""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_request(headers: list[tuple[bytes, bytes]]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Step 1: bad-header matrix, parameterized over all four protected endpoints.
# ---------------------------------------------------------------------------

_VALID_IDENTITY = [(b"x-tenant-id", b"t1"), (b"x-user-id", b"u1")]

_BAD_HEADER_CASES: dict[str, list[tuple[bytes, bytes]]] = {
    "missing_tenant": [(b"x-user-id", b"u1")],
    "missing_user": [(b"x-tenant-id", b"t1")],
    "duplicate_tenant": [
        *_VALID_IDENTITY,
        (b"x-tenant-id", b"t2"),
    ],
    "duplicate_user": [
        (b"x-tenant-id", b"t1"),
        (b"x-user-id", b"u1"),
        (b"x-user-id", b"u2"),
    ],
    "duplicate_group_ids_header": [
        *_VALID_IDENTITY,
        (b"x-group-ids", b"g1"),
        (b"x-group-ids", b"g2"),
    ],
    "group_ids_too_large": [
        *_VALID_IDENTITY,
        (b"x-group-ids", ("g," * 3000).encode("ascii")),
    ],
    "group_ids_empty_token_leading_comma": [
        *_VALID_IDENTITY,
        (b"x-group-ids", b",g1"),
    ],
    "group_ids_empty_token_trailing_comma": [
        *_VALID_IDENTITY,
        (b"x-group-ids", b"g1,"),
    ],
    "group_ids_empty_token_middle": [
        *_VALID_IDENTITY,
        (b"x-group-ids", b"g1,,g2"),
    ],
    "group_ids_too_many_tokens": [
        *_VALID_IDENTITY,
        (b"x-group-ids", ",".join(f"g{i}" for i in range(101)).encode("ascii")),
    ],
    "tenant_id_invalid_characters": [
        (b"x-tenant-id", b"bad tenant!"),
        (b"x-user-id", b"u1"),
    ],
    "user_id_invalid_characters": [
        (b"x-tenant-id", b"t1"),
        (b"x-user-id", b"bad user!"),
    ],
    "group_id_invalid_characters": [
        *_VALID_IDENTITY,
        (b"x-group-ids", b"bad group!"),
    ],
}


@pytest.mark.parametrize(("path", "payload"), _PROTECTED_CASES)
@pytest.mark.parametrize("case_name", list(_BAD_HEADER_CASES))
def test_bad_headers_yield_401_never_422(
    bare_client: TestClient, case_name: str, path: str, payload: dict[str, str]
) -> None:
    # A list of tuples (not a dict) preserves duplicate header names — a
    # dict comprehension would silently collapse the very duplication the
    # "duplicate_tenant" / "duplicate_group_ids_header" cases exist to test.
    headers = [(k.decode(), v.decode()) for k, v in _BAD_HEADER_CASES[case_name]]
    response = bare_client.post(path, json=payload, headers=headers)

    assert response.status_code == 401, (
        f"{case_name} on {path}: expected 401, got {response.status_code} "
        f"body={response.text!r}"
    )


@pytest.mark.parametrize(("path", "payload"), _PROTECTED_CASES)
def test_valid_headers_reach_the_handler(
    bare_client: TestClient, path: str, payload: dict[str, str]
) -> None:
    response = bare_client.post(
        path,
        json=payload,
        headers={"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-Group-Ids": "g1, g2"},
    )

    assert response.status_code != 401


@pytest.mark.parametrize(("path", "payload"), _PROTECTED_CASES)
def test_valid_headers_without_group_ids_reach_the_handler(
    bare_client: TestClient, path: str, payload: dict[str, str]
) -> None:
    response = bare_client.post(
        path, json=payload, headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"}
    )

    assert response.status_code != 401


@pytest.mark.parametrize(("path", "payload"), _PROTECTED_CASES)
def test_ows_only_group_ids_header_reaches_the_handler(
    bare_client: TestClient, path: str, payload: dict[str, str]
) -> None:
    # Present but optional-whitespace-only -> () per the parsing rules, same
    # as an absent header entirely; must not be mistaken for a malformed value.
    response = bare_client.post(
        path,
        json=payload,
        headers={"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-Group-Ids": " \t"},
    )

    assert response.status_code != 401


# ---------------------------------------------------------------------------
# Step 5: endpoint matrix assertions — envelope shape, challenge header,
# health bypass, stream content-type.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "payload"), _PROTECTED_CASES)
def test_401_body_is_the_envelope_with_www_authenticate_challenge(
    bare_client: TestClient, path: str, payload: dict[str, str]
) -> None:
    response = bare_client.post(path, json=payload)

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["correlation_id"]
    assert response.headers["www-authenticate"] == "Bearer"


def test_health_with_no_headers_is_200(bare_client: TestClient) -> None:
    response = bare_client.get("/health")

    assert response.status_code == 200


def test_chat_stream_401_is_plain_json_not_sse(bare_client: TestClient) -> None:
    response = bare_client.post("/api/v1/chat/stream", json={"message": "hello"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# ContextVar lifecycle: driven directly via anext()/aclose().
# ---------------------------------------------------------------------------


async def test_tenant_id_var_lifecycle_around_the_dependency() -> None:
    assert tenant_id_var.get() == "-"
    assert user_id_var.get() == "-"

    request = _make_request([(b"x-tenant-id", b"t1"), (b"x-user-id", b"u1")])
    # The second argument is the OpenAPI-only bearer credential (Day 19). It
    # is ignored by the dependency — the resolver re-reads the raw header —
    # so a direct call passes None where FastAPI would inject it.
    gen: AsyncGenerator = require_principal(request, None)  # type: ignore[type-arg]
    try:
        principal = await anext(gen)
        assert principal.tenant_id == "t1"
        assert principal.user_id == "u1"
        assert tenant_id_var.get() == "t1"
        assert user_id_var.get() == "u1"
    finally:
        await gen.aclose()

    assert tenant_id_var.get() == "-"
    assert user_id_var.get() == "-"


async def test_require_principal_logs_identity_resolved_once_after_both_vars_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Attached-but-unverified is the exact shape the Day 15 tenant_id defect
    # took (record.tenant_id was set, but nothing rendered it) — this test
    # exercises the real dependency (not a hand-built record) to prove the
    # "identity resolved" event actually fires, exactly once, and only after
    # both ContextVars carry the resolved values rather than their "-"
    # defaults.
    request = _make_request([(b"x-tenant-id", b"t1"), (b"x-user-id", b"u1")])
    gen: AsyncGenerator = require_principal(request, None)  # type: ignore[type-arg]
    with caplog.at_level(logging.INFO, logger="azgenai_lab.api.principal"):
        try:
            await anext(gen)
        finally:
            await gen.aclose()

    identity_records = [r for r in caplog.records if r.getMessage() == "identity resolved"]
    assert len(identity_records) == 1
    record = identity_records[0]
    assert record.levelno == logging.INFO
    assert record.tenant_id == "t1"  # type: ignore[attr-defined]
    assert record.user_id == "u1"  # type: ignore[attr-defined]


def _emit_record() -> logging.LogRecord:
    """Build a real LogRecord through the configured global factory (the same
    one core/logging.py installs at startup), not by constructing one
    directly — the point is to exercise the factory, matching
    test_logging_wiring.py's pattern of testing real wiring over stubs."""
    factory = logging.getLogRecordFactory()
    return factory(
        "azgenai_lab.test", logging.INFO, __file__, 0, "probe", (), None
    )


async def test_log_record_carries_tenant_id_inside_the_dependency_scope_only() -> None:
    outside_before = _emit_record()
    assert outside_before.tenant_id == "-"  # type: ignore[attr-defined]

    request = _make_request([(b"x-tenant-id", b"t1"), (b"x-user-id", b"u1")])
    gen: AsyncGenerator = require_principal(request, None)  # type: ignore[type-arg]
    try:
        await anext(gen)
        inside = _emit_record()
        assert inside.tenant_id == "t1"  # type: ignore[attr-defined]
    finally:
        await gen.aclose()

    outside_after = _emit_record()
    assert outside_after.tenant_id == "-"  # type: ignore[attr-defined]
