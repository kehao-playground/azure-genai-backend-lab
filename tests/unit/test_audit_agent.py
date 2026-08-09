"""``agent.run`` emission from the ``/agent`` finalizer (Day 22 Task 9).

The terminal snapshot (spec §4b) is built by the adapter — real and fake
alike — at the point it still holds the app-side tool executions, and success
and error carry the same shape (``AgentAuditTerminalSnapshot``). Fixtures
reach into the already-built ``app.state.agent_turn_service`` and swap only
the piece under test, mirroring test_audit_chat_api.py's discipline: a whole
new service would drop the composed ``audit_attribution``/prompt instance.
"""

import logging
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import IDENTITY, audit_events

from azgenai_lab.core.audit import AgentAuditTerminalSnapshot, AuditToolExecution
from azgenai_lab.core.config import Settings
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.models.chat import Message
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.models.principal import Principal
from azgenai_lab.prompts import loader
from azgenai_lab.services.agent_framework import (
    AgentRunError,
    AgentRunResult,
    AgentToolCall,
    ToolExecution,
    _join_snapshot_executions,
)
from azgenai_lab.services.agent_turn import build_agent_turn_service
from azgenai_lab.services.conversation_store import InMemoryConversationStore


class _RaisingAgentService:
    """Substitutes only the loop boundary — mirrors
    test_audit_chat_api.py's ``_RaisingChatService``. Always raises the
    supplied error; used here to drive a degraded (framework-fields-None)
    terminal snapshot through ``AgentRunError``.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run(
        self, task: str, history: object, *, principal: Principal
    ) -> AgentRunResult:
        raise self._error

    async def aclose(self) -> None:
        return None


class _BrokenGetStore(InMemoryConversationStore):
    async def get(self, tenant_id: str, conversation_id: str) -> object:
        raise RuntimeError("store unavailable")


class _BrokenAppendStore(InMemoryConversationStore):
    async def append(
        self,
        tenant_id: str,
        conversation_id: str,
        turns: Sequence[Message],
        replay_items: Sequence[ReplayItem],
        expected_revision: int,
        usage_tokens: int,
        *,
        first_turn_authorization_group_ids: tuple[str, ...] | None,
    ) -> None:
        raise RuntimeError("disk on fire")


@pytest.fixture
def tiny_budget_agent_client(client: TestClient) -> TestClient:
    client.app.state.agent_turn_service._token_budget = 1
    return client


@pytest.fixture
def degraded_agent_client(client: TestClient) -> TestClient:
    # Framework/provider terminal failure: executions present (one tool ran
    # before the failure), every framework-derived field honestly None — a
    # degraded trace, not a fabricated zero.
    snapshot = AgentAuditTerminalSnapshot(
        provider_call_attempted=True,
        executions=(AuditToolExecution(name="search_docs", executed=True, round_index=None),),
        model_calls=None, tool_call_count=None, refused_call_count=None,
        stop_reason=None, usage=None,
    )
    error = AgentRunError("framework blew up", usage=None, audit_snapshot=snapshot)
    client.app.state.agent_turn_service._agent_service = _RaisingAgentService(error)
    return client


@pytest.fixture
def broken_store_get_agent_client(client: TestClient) -> TestClient:
    client.app.state.agent_turn_service._store = _BrokenGetStore()
    return client


@pytest.fixture
def broken_store_append_agent_client(client: TestClient) -> TestClient:
    client.app.state.agent_turn_service._store = _BrokenAppendStore()
    return client


def test_agent_success_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/agent", json={"task": "check ops"}, headers=IDENTITY)
    assert response.status_code == 200
    [event] = audit_events(caplog)
    assert event["event"] == "agent.run" and event["outcome"] == "success"
    assert event["prompt_name"] == "ops_agent" and event["deployment"] == "fake"
    assert event["provider_call_attempted"] is True and event["committed"] is True
    assert len(event["tools"]) >= 1
    assert all(t["round_index"] == 1 for t in event["tools"])  # fake single round


def test_agent_oversized_task_400_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/agent", json={"task": "x" * 5000}, headers=IDENTITY)
    assert response.status_code == 400
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "invalid_input")
    assert event["provider_call_attempted"] is False and event["prompt_name"] is None


def test_agent_unknown_conversation_404_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/agent",
            json={"task": "hi", "conversation_id": "ghost"},
            headers=IDENTITY,
        )
    assert response.status_code == 404
    [event] = audit_events(caplog)
    assert event["error_code"] == "conversation_not_found"


def test_agent_budget_429_event(
    tiny_budget_agent_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    first = tiny_budget_agent_client.post(
        "/api/v1/agent", json={"task": "hi"}, headers=IDENTITY
    )
    cid = first.json()["conversation_id"]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="audit"):
        response = tiny_budget_agent_client.post(
            "/api/v1/agent", json={"task": "again", "conversation_id": cid}, headers=IDENTITY
        )
    assert response.status_code == 429
    [event] = audit_events(caplog)
    assert event["error_code"] == "token_budget_exceeded"
    assert event["spent"] >= 1 and event["budget"] == 1


def test_agent_run_error_degraded_snapshot(
    degraded_agent_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = degraded_agent_client.post(
            "/api/v1/agent", json={"task": "hi"}, headers=IDENTITY
        )
    assert response.status_code == 502
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "upstream_error")
    assert len(event["tools"]) == 1 and event["tools"][0]["round_index"] is None
    assert event["model_calls"] is None  # null, not 0
    assert event["provider_call_attempted"] is True


def test_agent_storage_load_failure(
    broken_store_get_agent_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = broken_store_get_agent_client.post(
            "/api/v1/agent", json={"task": "hi", "conversation_id": "c1"}, headers=IDENTITY
        )
    assert response.status_code == 500
    [event] = audit_events(caplog)
    assert event["error_code"] == "storage_error"
    assert event["provider_call_attempted"] is False and event["tools"] is None


def test_agent_storage_commit_failure_preserves_snapshot(
    broken_store_append_agent_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = broken_store_append_agent_client.post(
            "/api/v1/agent", json={"task": "hi"}, headers=IDENTITY
        )
    assert response.status_code == 500
    [event] = audit_events(caplog)
    assert event["error_code"] == "storage_error"
    assert event["provider_call_attempted"] is True
    assert len(event["tools"]) >= 1 and event["committed"] is False


def test_join_degrades_on_name_mismatch() -> None:
    # r6-3a: equal length, swapped names -> all round_index None
    executions = [
        ToolExecution(tool_name="a", executed=True, latency_ms=1.0),
        ToolExecution(tool_name="b", executed=True, latency_ms=1.0),
    ]
    calls = (
        AgentToolCall(
            tool_name="b", arguments=None, arguments_canonical_json="",
            round_index=1, executed=True,
        ),
        AgentToolCall(
            tool_name="a", arguments=None, arguments_canonical_json="",
            round_index=1, executed=True,
        ),
    )
    joined = _join_snapshot_executions(executions, calls)
    assert all(e.round_index is None for e in joined)


def test_fake_agent_holds_the_same_prompt_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_load = loader.load_prompt
    monkeypatch.setattr(
        "azgenai_lab.services.agent_turn.load_prompt",
        lambda name: (calls.append(name), real_load(name))[1],
    )
    service = build_agent_turn_service(
        Settings(_env_file=None), store=InMemoryConversationStore(), locks=KeyedLock()
    )
    assert calls.count("ops_agent") == 1
    assert service.audit_attribution is not None
    assert service._agent_service._prompt.sha256 == service.audit_attribution.prompt_sha256


def test_non_upstream_exception_propagates_uncaught_with_no_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A bug (not a contract case): the handler's except clause is UpstreamError
    # specifically, so this must not be swallowed into an agent.run event.
    error = RuntimeError("not an UpstreamError")
    client.app.state.agent_turn_service._agent_service = _RaisingAgentService(error)
    with (
        caplog.at_level(logging.INFO, logger="audit"),
        pytest.raises(RuntimeError, match="not an UpstreamError"),
    ):
        client.post("/api/v1/agent", json={"task": "hi"}, headers=IDENTITY)
    assert audit_events(caplog) == []


def test_tool_arguments_never_reach_the_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        client.post("/api/v1/agent", json={"task": "check ops"}, headers=IDENTITY)
    [event] = audit_events(caplog)
    for tool in event["tools"]:
        assert set(tool) == {"name", "executed", "round_index"}
