from fastapi.testclient import TestClient


def test_chat_placeholder_returns_error_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "hi"})

    assert response.status_code == 501
    body = response.json()
    assert body["error"]["code"] == "not_implemented"
    assert body["correlation_id"]
    assert response.headers["x-correlation-id"] == body["correlation_id"]


def test_correlation_id_is_propagated_from_request(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Correlation-Id": "test-id-123"})

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "test-id-123"
