"""``chat.turn`` emission from the non-streaming ``/chat`` finalizer (Day 22 Task 6).

Every exit path of the handler — success, each rejection, each upstream
failure class, each typed storage-commit outcome — emits exactly one
``chat.turn`` event before the response leaves. Fixtures reach into the
already-built ``app.state.conversation_service`` and swap only the piece
under test (``._chat_service`` or ``._store`` or ``._token_budget``), so the
service keeps the ``audit_attribution`` the composition point actually built
— substituting a whole new ``ConversationChatService`` would either drop that
attribution or require re-deriving it, which is exactly the "same file, same
sha256 is not the same instance" trap Task 6 exists to close.
"""

import logging
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import (
    IDENTITY,
    audit_events,
    with_broken_chat_append_store,
    with_broken_chat_get_store,
    with_raising_chat_service,
    with_tiny_chat_budget,
)

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import ContentFilteredError, UpstreamServiceError
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts import loader
from azgenai_lab.services.conversation import build_conversation_service
from azgenai_lab.services.conversation_store import InMemoryConversationStore


@pytest.fixture
def raising_client_factory(client: TestClient):
    def _factory(error: Exception) -> TestClient:
        return with_raising_chat_service(client, error)

    return _factory


@pytest.fixture
def tiny_budget_client(client: TestClient) -> TestClient:
    return with_tiny_chat_budget(client)


@pytest.fixture
def broken_store_get_client(client: TestClient) -> TestClient:
    return with_broken_chat_get_store(client)


@pytest.fixture
def broken_store_append_client(client: TestClient) -> TestClient:
    return with_broken_chat_append_store(client)


def test_chat_success_exactly_one_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    assert response.status_code == 200
    [event] = audit_events(caplog)
    assert event["event"] == "chat.turn" and event["outcome"] == "success"
    assert event["streaming"] is False and event["committed"] is True
    assert event["provider_call_attempted"] is True
    assert event["deployment"] == "fake" and event["model_version"] == "fake"
    assert event["prompt_name"] == "default_chat"
    assert event["correlation_id"] == response.json()["correlation_id"]
    assert event["conversation_id"] == response.json()["conversation_id"]


def test_chat_unknown_conversation_404_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/chat", json={"message": "hi", "conversation_id": "ghost"}, headers=IDENTITY
        )
    assert response.status_code == 404
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "conversation_not_found")
    assert event["provider_call_attempted"] is False and event["prompt_name"] is None
    assert event["conversation_id"] == "ghost"


def test_chat_budget_429_event(
    tiny_budget_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    first = tiny_budget_client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    cid = first.json()["conversation_id"]
    # caplog accumulates for the whole test, not just inside the with-block
    # below: clear the first turn's own chat.turn event so the assertion
    # sees only the rejection this test is about.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="audit"):
        response = tiny_budget_client.post(
            "/api/v1/chat",
            json={"message": "again", "conversation_id": cid},
            headers=IDENTITY,
        )
    assert response.status_code == 429
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "token_budget_exceeded")
    assert event["spent"] >= 1 and event["budget"] == 1


def test_chat_provider_400_event(raising_client_factory, caplog: pytest.LogCaptureFixture) -> None:
    client = raising_client_factory(ContentFilteredError("blocked"))
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    assert response.status_code == 400
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "content_filtered")
    assert event["provider_call_attempted"] is True and event["deployment"] == "fake"


def test_chat_provider_5xx_event(raising_client_factory, caplog: pytest.LogCaptureFixture) -> None:
    client = raising_client_factory(UpstreamServiceError("boom"))
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    assert response.status_code == 502
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "upstream_error")
    assert event["usage"] is None


def test_chat_storage_load_failure_attempted_false(
    broken_store_get_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = broken_store_get_client.post(
            "/api/v1/chat", json={"message": "hi", "conversation_id": "c1"}, headers=IDENTITY
        )
    assert response.status_code == 500
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "storage_error")
    assert event["provider_call_attempted"] is False and event["model_version"] is None


def test_chat_storage_commit_failure_attempted_true(
    broken_store_append_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="audit"):
        response = broken_store_append_client.post(
            "/api/v1/chat", json={"message": "hi"}, headers=IDENTITY
        )
    assert response.status_code == 500
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "storage_error")
    assert event["provider_call_attempted"] is True
    assert event["model_version"] == "fake" and event["committed"] is False


def test_non_upstream_exception_propagates_uncaught_with_no_event(
    raising_client_factory, caplog: pytest.LogCaptureFixture
) -> None:
    # A bug (not a contract case): the handler's except clause is UpstreamError
    # specifically, so this must not be swallowed into a chat.turn event —
    # it propagates unchanged, and nothing is emitted.
    client = raising_client_factory(RuntimeError("not an UpstreamError"))
    with (
        caplog.at_level(logging.INFO, logger="audit"),
        pytest.raises(RuntimeError, match="not an UpstreamError"),
    ):
        client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    assert audit_events(caplog) == []


def test_default_chat_prompt_loaded_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_load = loader.load_prompt
    monkeypatch.setattr(
        "azgenai_lab.services.conversation.load_prompt",
        lambda name: (calls.append(name), real_load(name))[1],
    )
    service = build_conversation_service(
        Settings(_env_file=None), store=InMemoryConversationStore(), locks=KeyedLock()
    )
    assert calls == ["default_chat"]
    assert service.audit_attribution is not None
    assert service.audit_attribution.prompt_name == "default_chat"
    assert service.audit_attribution.deployment == "fake"


def test_duration_covers_service_time(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Controllable clock (r6-6): perf_counter is process-global, so drive a
    # shared value instead of counting calls. All reads before the stub's
    # completion see 0.0; the stub advances the clock; emission reads 0.25.
    clock = {"now": 0.0}
    monkeypatch.setattr("time.perf_counter", lambda: clock["now"])

    inner = client.app.state.conversation_service._chat_service

    class DelayedStub:
        async def complete(self, items: Sequence[ReplayItem]) -> object:
            result = await inner.complete(items)
            clock["now"] = 0.25
            return result

        async def open_stream(self, items: Sequence[ReplayItem]) -> object:
            return await inner.open_stream(items)

        async def aclose(self) -> None:
            return None

    client.app.state.conversation_service._chat_service = DelayedStub()
    with caplog.at_level(logging.INFO, logger="audit"):
        client.post("/api/v1/chat", json={"message": "hi"}, headers=IDENTITY)
    [event] = audit_events(caplog)
    assert event["duration_ms"] == pytest.approx(250.0)
