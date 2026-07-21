import pytest
from fastapi.testclient import TestClient

from azgenai_lab.api.chat import get_chat_service
from azgenai_lab.core.errors import (
    ConfigurationError,
    ContentFilteredError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.main import app


def test_chat_returns_reply_and_correlation_id(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "[fake-llm] ping"
    assert body["correlation_id"]
    assert response.headers["x-correlation-id"] == body["correlation_id"]


def test_chat_echoes_provided_correlation_id(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat",
        json={"message": "ping"},
        headers={"X-Correlation-Id": "test-id-123"},
    )

    assert response.status_code == 200
    assert response.json()["correlation_id"] == "test-id-123"


def test_chat_accepts_reserved_conversation_id(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat", json={"message": "ping", "conversation_id": "future-day-07"}
    )

    assert response.status_code == 200


def test_chat_rejects_empty_message(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": ""})

    assert response.status_code == 422


class RaisingChatService:
    def __init__(self, error: UpstreamError) -> None:
        self._error = error

    async def complete(self, message: str) -> object:
        raise self._error


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ConfigurationError("secret detail"), 500, "configuration_error"),
        (ContentFilteredError("secret detail"), 400, "content_filtered"),
        (UpstreamThrottledError("secret detail"), 503, "upstream_throttled"),
        (UpstreamTimeoutError("secret detail"), 504, "upstream_timeout"),
        (UpstreamServiceError("secret detail"), 502, "upstream_error"),
    ],
)
def test_upstream_errors_map_to_error_envelope(
    client: TestClient, error: UpstreamError, status_code: int, code: str
) -> None:
    app.dependency_overrides[get_chat_service] = lambda: RaisingChatService(error)

    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == status_code
    body = response.json()
    assert body["error"]["code"] == code
    assert body["correlation_id"]
    assert "secret detail" not in response.text  # upstream detail never reaches the client
