"""POST /api/v1/agent contract (Day 18). Fake adapters end-to-end."""

from fastapi.testclient import TestClient

from azgenai_lab.main import create_app

HEADERS = {"X-Tenant-Id": "t1"}


def _client() -> TestClient:
    return TestClient(create_app(), headers=HEADERS)


def test_agent_turn_opens_conversation_with_trace() -> None:
    response = _client().post("/api/v1/agent", json={"task": "check config"})
    assert response.status_code == 200
    body = response.json()
    assert body["conversation_id"]
    assert body["correlation_id"]
    assert body["status"] == "completed"
    assert body["incomplete_reason"] is None
    assert isinstance(body["tool_calls"], list) and body["tool_calls"]
    first = body["tool_calls"][0]
    assert set(first) == {"tool_name", "arguments", "round_index", "executed"}


def test_agent_sees_prior_chat_turn_in_same_conversation() -> None:
    client = _client()
    chat = client.post("/api/v1/chat", json={"message": "Hello"})
    cid = chat.json()["conversation_id"]
    agent = client.post("/api/v1/agent", json={"task": "recap", "conversation_id": cid})
    assert agent.status_code == 200
    assert "history=2" in agent.json()["answer"]
    follow_up = client.post(
        "/api/v1/chat", json={"message": "and now?", "conversation_id": cid}
    )
    assert follow_up.status_code == 200  # agent turn committed into /chat's history


def test_unknown_conversation_is_404() -> None:
    response = _client().post(
        "/api/v1/agent", json={"task": "x", "conversation_id": "never-issued"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


def test_scope_mismatch_is_404_same_shape() -> None:
    client = _client()
    cid = client.post("/api/v1/agent", json={"task": "seed"}).json()["conversation_id"]
    response = client.post(
        "/api/v1/agent",
        json={"task": "steal", "conversation_id": cid},
        headers={"X-Tenant-Id": "t1", "X-Group-Ids": "other"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


def test_whitespace_task_is_422() -> None:
    response = _client().post("/api/v1/agent", json={"task": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_oversized_task_is_400_invalid_input() -> None:
    response = _client().post("/api/v1/agent", json={"task": "字" * 2000})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"
