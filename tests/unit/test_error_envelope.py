from fastapi.testclient import TestClient


def test_correlation_id_is_propagated_from_request(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Correlation-Id": "test-id-123"})

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "test-id-123"
