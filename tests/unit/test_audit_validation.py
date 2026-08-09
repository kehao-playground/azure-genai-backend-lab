"""``validation_error_handler`` 422 audit emission (Day 22 Task 10).

A request that fails FastAPI's request-body validation returns 422 without
ever reaching a route handler, so none of the Task 6-9 finalizers run —
this handler is the only place left that can close the gap. The tricky part
is malformed JSON: it can fail validation *before* `require_principal` ever
runs, leaving no resolved identity behind. In that case the design rule is
zero events, not a fabricated one — this suite is the guard's proof.

Uses a local `client` fixture (not the shared one from tests/conftest.py)
for the same reason test_audit_auth.py does: the shared fixture carries
default identity headers, which is exactly wrong for
`test_unauthenticated_bad_body_is_auth_event_only` — that test needs a
request that carries precisely what it specifies, nothing implied.
"""

import logging
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import IDENTITY, audit_events

from azgenai_lab.main import app


@pytest.fixture
def client() -> Generator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_chat_422_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": ""}, headers=IDENTITY)
    assert response.status_code == 422
    [event] = audit_events(caplog)
    assert event["event"] == "chat.turn" and event["error_code"] == "validation_error"
    assert event["conversation_id"] is None and event["streaming"] is False
    assert event["provider_call_attempted"] is False and event["committed"] is False


def test_stream_422_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/chat/stream", json={"message": ""}, headers=IDENTITY
        )
    assert response.status_code == 422
    [event] = audit_events(caplog)
    assert event["streaming"] is True


def test_rag_422_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/rag", json={"question": "   "}, headers=IDENTITY)
    assert response.status_code == 422
    [event] = audit_events(caplog)
    assert event["event"] == "rag.query" and event["status"] is None


def test_agent_422_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/agent", json={"task": " "}, headers=IDENTITY)
    assert response.status_code == 422
    [event] = audit_events(caplog)
    assert event["event"] == "agent.run" and event["error_code"] == "validation_error"


def test_malformed_json_emits_nothing(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/chat", content=b"{not json",
            headers={**IDENTITY, "content-type": "application/json"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"  # envelope unchanged
    assert audit_events(caplog) == []  # no principal -> no event


def test_unauthenticated_bad_body_is_auth_event_only(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": ""})
    assert response.status_code == 401  # dependency runs first
    events = audit_events(caplog)
    assert [e["event"] for e in events] == ["auth.rejected"]
