"""``chat.turn`` emission from the ``/chat/stream`` two-phase boundary (Day 22 Task 7).

Pre-stream failures (before ``StreamingResponse`` is built) are owned by the
endpoint finalizer and mirror ``/chat``'s classification exactly (same
``chat_upstream_audit_args`` helper — see test_audit_chat_api.py). Once
``open_stream()`` returns an iterator, ownership moves to ``_audit_observed``,
the post-transfer observer generator: its three-way exception routing (r6-2)
is exercised directly at the generator level so the two disconnect states
(before/after the terminal was observed) land unambiguously on either side of
``StreamDone`` — an endpoint-level approximation (killing a TestClient
connection) cannot pin the exact point of disconnect the way driving
``aclose()`` by hand can.
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from tests.unit.audit_helpers import IDENTITY, audit_events

from azgenai_lab.api.streaming import _audit_observed
from azgenai_lab.core.audit import AuditAttribution, ChatCommitSnapshot, chat_base_fields
from azgenai_lab.core.errors import (
    ChatStorageCommitError,
    UpstreamServiceError,
    UpstreamThrottledError,
)
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.azure_openai import StreamDone, TextDelta

ATTR = AuditAttribution("default_chat", 1, "ab" * 32, "fake")


def _fields():
    return chat_base_fields(
        tenant_id="t1", user_id="u1", correlation_id="cid-1",
        conversation_id="c1", streaming=True,
    )


def _observed(events):
    return _audit_observed(events, base=_fields(), attribution=ATTR, audit_start=0.0)


# --- generator-level: post-transfer observer, all three exception branches ---


async def test_disconnect_before_terminal_is_client_disconnect(caplog):
    async def deltas_forever():
        yield TextDelta("a")
        yield TextDelta("b")
        await asyncio.sleep(3600)

    gen = _observed(deltas_forever())
    with caplog.at_level(logging.INFO, logger="audit"):
        assert isinstance(await anext(gen), TextDelta)
        await gen.aclose()
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "client_disconnect")
    assert event["committed"] is False and event["usage"] is None
    assert event["provider_call_attempted"] is True  # ownership transferred ⇒ boundary called
    assert event["prompt_name"] == "default_chat"


async def test_disconnect_after_terminal_keeps_commit_truth(caplog):
    async def with_terminal():
        yield TextDelta("x")
        yield StreamDone(
            status="completed", model_version="fake",
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
        await asyncio.sleep(3600)

    gen = _observed(with_terminal())
    with caplog.at_level(logging.INFO, logger="audit"):
        await anext(gen)
        done = await anext(gen)
        assert isinstance(done, StreamDone)
        await gen.aclose()
    [event] = audit_events(caplog)
    assert event["outcome"] == "success" and event["committed"] is True
    assert event["usage"]["total_tokens"] == 2 and event["model_version"] == "fake"


async def test_mid_stream_upstream_error(caplog):
    async def failing():
        yield TextDelta("x")
        raise UpstreamServiceError("boom")

    gen = _observed(failing())
    with caplog.at_level(logging.INFO, logger="audit"):
        await anext(gen)
        with pytest.raises(UpstreamServiceError):
            await anext(gen)
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "upstream_error")
    assert event["provider_call_attempted"] is True and event["prompt_name"] is not None


async def test_eof_without_terminal_is_upstream_error(caplog):
    async def eof():
        yield TextDelta("x")

    gen = _observed(eof())
    with caplog.at_level(logging.INFO, logger="audit"):
        async for _ in gen:
            pass
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "upstream_error")


async def test_commit_failure_preserves_terminal_data(caplog):
    snapshot = ChatCommitSnapshot(
        model_version="fake",
        usage=TokenUsage(input_tokens=3, output_tokens=4, total_tokens=7),
        status="completed", incomplete_reason=None,
    )

    async def commit_fails():
        yield TextDelta("x")
        raise ChatStorageCommitError("append blew up", audit_snapshot=snapshot)

    gen = _observed(commit_fails())
    with caplog.at_level(logging.INFO, logger="audit"):
        await anext(gen)
        with pytest.raises(ChatStorageCommitError):
            await anext(gen)
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "storage_error")
    assert event["committed"] is False and event["provider_call_attempted"] is True
    assert event["usage"]["total_tokens"] == 7 and event["model_version"] == "fake"
    assert event["status"] == "completed"


async def test_non_upstream_bug_emits_nothing(caplog):
    async def buggy():
        yield TextDelta("x")
        raise ValueError("programmer error")

    gen = _observed(buggy())
    with caplog.at_level(logging.INFO, logger="audit"):
        await anext(gen)
        with pytest.raises(ValueError):
            await anext(gen)
        await gen.aclose()
    assert audit_events(caplog) == []  # out-of-contract bug: zero events


# --- endpoint-level: pre-stream vs. post-transfer mutual exclusivity ---


def test_pre_stream_404_and_success_are_mutually_exclusive(client, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "hi", "conversation_id": "ghost"},
            headers=IDENTITY,
        )
    assert response.status_code == 404
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "conversation_not_found")
    assert event["streaming"] is True


def test_stream_success_exactly_one_event(client, caplog):
    with caplog.at_level(logging.INFO, logger="audit"), client.stream(
        "POST", "/api/v1/chat/stream", json={"message": "hi"}, headers=IDENTITY
    ) as response:
        body = "".join(response.iter_text())
    assert "message.done" in body
    [event] = audit_events(caplog)  # exactly one — two-phase exclusivity
    assert event["outcome"] == "success" and event["committed"] is True
    assert event["model_version"] == "fake" and event["usage"] is not None


class _RaisingChatService:
    """Substitutes only the LLM boundary — same shape as
    test_audit_chat_api.py's helper of the same name, kept local since each
    task-scoped test file owns its fixtures rather than reaching across
    files for private test helpers."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def complete(self, items: object) -> object:
        raise self._error

    async def open_stream(self, items: object) -> object:
        raise self._error

    async def aclose(self) -> None:
        return None


class _ScriptedStreamService:
    """Drives ``open_stream`` from a fixed event script; ``complete`` is
    unused by any test in this file."""

    def __init__(self, events: list) -> None:
        self._events = events

    async def complete(self, items: object) -> object:
        raise NotImplementedError

    async def open_stream(self, items: object) -> object:
        async def stream():
            for event in self._events:
                yield event

        return stream()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def tiny_budget_client(client: TestClient) -> TestClient:
    client.app.state.conversation_service._token_budget = 1
    return client


def test_pre_stream_429_budget_event(
    tiny_budget_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    first = tiny_budget_client.post(
        "/api/v1/chat/stream", json={"message": "hi"}, headers=IDENTITY
    )
    cid = first.headers["x-conversation-id"]
    # caplog accumulates for the whole test, not just inside the with-block
    # below: clear the first turn's own chat.turn event so the assertion
    # sees only the rejection this test is about.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="audit"):
        response = tiny_budget_client.post(
            "/api/v1/chat/stream",
            json={"message": "again", "conversation_id": cid},
            headers=IDENTITY,
        )
    assert response.status_code == 429
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("rejected", "token_budget_exceeded")
    assert event["spent"] >= 1 and event["budget"] == 1
    assert event["streaming"] is True


def test_pre_stream_eager_upstream_failure_event(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.app.state.conversation_service._chat_service = _RaisingChatService(
        UpstreamThrottledError("429 from upstream")
    )
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat/stream", json={"message": "hi"}, headers=IDENTITY)
    assert response.status_code == 503
    [event] = audit_events(caplog)
    assert (event["outcome"], event["error_code"]) == ("error", "upstream_throttled")
    assert event["provider_call_attempted"] is True


def test_content_filter_incomplete_success_event_not_committed(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.app.state.conversation_service._chat_service = _ScriptedStreamService(
        [
            TextDelta("par"),
            StreamDone(
                status="incomplete", incomplete_reason="content_filter", model_version="fake",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            ),
        ]
    )
    with caplog.at_level(logging.INFO, logger="audit"):
        response = client.post("/api/v1/chat/stream", json={"message": "hi"}, headers=IDENTITY)
    assert response.status_code == 200
    [event] = audit_events(caplog)
    assert event["outcome"] == "success" and event["committed"] is False
    assert event["status"] == "incomplete" and event["incomplete_reason"] == "content_filter"
