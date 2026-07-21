from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import (
    ConfigurationError,
    ContentFilteredError,
    InvalidInputError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.main import app
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.services.azure_openai import ChatResult, FakeChatService
from azgenai_lab.services.conversation import ConversationChatService
from azgenai_lab.services.conversation_store import InMemoryConversationStore


def test_chat_returns_reply_conversation_and_correlation_id(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "[fake-llm] ping"
    assert body["conversation_id"]
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


def test_chat_follow_up_turn_carries_the_history(client: TestClient) -> None:
    first = client.post("/api/v1/chat", json={"message": "ping"})
    conversation_id = first.json()["conversation_id"]

    second = client.post(
        "/api/v1/chat", json={"message": "again", "conversation_id": conversation_id}
    )

    assert second.status_code == 200
    body = second.json()
    # user+assistant from turn 1 = 2 prior messages seen by the (fake) model.
    assert body["message"] == "[fake-llm] again (history=2)"
    assert body["conversation_id"] == conversation_id


def test_chat_unknown_conversation_id_maps_to_404_envelope(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat", json={"message": "ping", "conversation_id": "never-issued"}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "conversation_not_found"
    assert body["correlation_id"]


def test_chat_rejects_empty_message_with_error_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "message" in body["error"]["message"]
    assert body["correlation_id"]
    assert "detail" not in body  # no FastAPI default shape leaking through


class RaisingChatService:
    def __init__(self, error: UpstreamError) -> None:
        self._error = error

    async def complete(self, items: Sequence[ReplayItem]) -> object:
        raise self._error


def override_with_raising(error: UpstreamError) -> None:
    # The orchestrator stays real: only the LLM boundary fails.
    service = ConversationChatService(RaisingChatService(error), InMemoryConversationStore())  # type: ignore[arg-type]
    app.dependency_overrides[get_conversation_service] = lambda: service


class FailingStore(InMemoryConversationStore):
    async def append(
        self,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
    ) -> None:
        raise RuntimeError("disk on fire")


def test_store_failure_maps_to_500_storage_error_envelope(client: TestClient) -> None:
    service = ConversationChatService(FakeChatService(), FailingStore())
    app.dependency_overrides[get_conversation_service] = lambda: service

    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "storage_error"
    assert body["correlation_id"]
    assert "disk on fire" not in response.text  # detail goes to the log only


class EmptyReplyChatService:
    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        return ChatResult(message="", model="empty")


def test_empty_upstream_reply_maps_to_502_not_a_ghost_conversation(client: TestClient) -> None:
    service = ConversationChatService(EmptyReplyChatService(), InMemoryConversationStore())  # type: ignore[arg-type]
    app.dependency_overrides[get_conversation_service] = lambda: service

    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ConfigurationError("secret detail"), 500, "configuration_error"),
        (ContentFilteredError("secret detail"), 400, "content_filtered"),
        (InvalidInputError("secret detail"), 400, "invalid_input"),
        (UpstreamThrottledError("secret detail"), 503, "upstream_throttled"),
        (UpstreamTimeoutError("secret detail"), 504, "upstream_timeout"),
        (UpstreamServiceError("secret detail"), 502, "upstream_error"),
    ],
)
def test_upstream_errors_map_to_error_envelope(
    client: TestClient, error: UpstreamError, status_code: int, code: str
) -> None:
    override_with_raising(error)

    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == status_code
    body = response.json()
    assert body["error"]["code"] == code
    assert body["correlation_id"]
    assert "secret detail" not in response.text  # upstream detail never reaches the client


def test_openapi_documents_the_error_contract() -> None:
    responses = app.openapi()["paths"]["/api/v1/chat"]["post"]["responses"]

    assert {"200", "400", "404", "422", "500", "502", "503", "504"} <= set(responses)
