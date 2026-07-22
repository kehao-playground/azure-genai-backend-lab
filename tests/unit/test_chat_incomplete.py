"""Non-streaming incomplete contract (Day 9 review r01 finding 2).

max_output_tokens travels on non-streaming calls too, so /chat must mirror
the stream terminal: an incomplete response surfaces status and
incomplete_reason instead of being disguised as normal success, and the
turn-commit rule is the same keep/discard rule as the Day 6 vocabulary.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import UpstreamServiceError, UpstreamThrottledError
from azgenai_lab.main import app
from azgenai_lab.prompts.loader import load_prompt
from azgenai_lab.services.azure_openai import (
    AzureOpenAIChatService,
    ChatResult,
    FakeChatService,
    _log_llm_usage,
)
from azgenai_lab.services.conversation import ConversationChatService
from azgenai_lab.services.conversation_store import InMemoryConversationStore

PROMPT = load_prompt("default_chat")


def make_service(response: Any) -> AzureOpenAIChatService:
    async def create(**kwargs: Any) -> Any:
        return response

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return AzureOpenAIChatService(
        cast(AsyncOpenAI, client), "chat-mini", prompt=PROMPT, max_output_tokens=1000
    )


def response(
    status: str = "completed",
    reason: str | None = None,
    text: str = "par",
    error: Any = None,
) -> Any:
    return SimpleNamespace(
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        error=error,
        output_text=text,
        model="gpt-5-mini-2025-08-07",
        output=[],
        usage=SimpleNamespace(
            input_tokens=30,
            output_tokens=72,
            total_tokens=102,
            output_tokens_details=SimpleNamespace(reasoning_tokens=64),
        ),
    )


# --- adapter: status mapping and reasoning-token extraction ---


@pytest.mark.parametrize(
    ("upstream_reason", "mapped"),
    [
        ("max_output_tokens", "max_output_tokens"),
        ("content_filter", "content_filter"),
        ("something_new", "other"),
        (None, "other"),
    ],
)
async def test_incomplete_response_surfaces_status_and_reason(
    upstream_reason: str | None, mapped: str
) -> None:
    service = make_service(response(status="incomplete", reason=upstream_reason))

    result = await service.complete([{"role": "user", "content": "hi"}])

    assert result.status == "incomplete"
    assert result.incomplete_reason == mapped
    assert result.message == "par"  # partial text is surfaced, not swallowed


async def test_completed_response_has_completed_status() -> None:
    service = make_service(response())

    result = await service.complete([{"role": "user", "content": "hi"}])

    assert result.status == "completed"
    assert result.incomplete_reason is None


async def test_failed_response_raises_translated_error() -> None:
    failed = response(status="failed", text="")
    failed.error = SimpleNamespace(code="rate_limit_exceeded", message="slow down")
    service = make_service(failed)

    with pytest.raises(UpstreamThrottledError):
        await service.complete([{"role": "user", "content": "hi"}])


async def test_usage_carries_reasoning_token_detail() -> None:
    service = make_service(response())

    result = await service.complete([{"role": "user", "content": "hi"}])

    assert result.usage is not None
    assert result.usage.reasoning_tokens == 64


# --- orchestrator: keep/discard commit rules mirror the stream terminal ---


class ScriptedChat(FakeChatService):
    """First turn succeeds; later turns reply from the script."""

    def __init__(self, scripted: ChatResult) -> None:
        super().__init__()
        self._scripted = scripted
        self._calls = 0

    async def complete(self, items):  # type: ignore[no-untyped-def]
        self._calls += 1
        if self._calls == 1:
            return await super().complete(items)
        return self._scripted


def make_conversation_service(
    scripted: ChatResult,
) -> tuple[ConversationChatService, InMemoryConversationStore]:
    store = InMemoryConversationStore()
    return ConversationChatService(ScriptedChat(scripted), store), store


async def test_max_output_tokens_truncation_commits_the_partial_turn() -> None:
    scripted = ChatResult(message="par", status="incomplete", incomplete_reason="max_output_tokens")
    service, store = make_conversation_service(scripted)

    conversation_id, _ = await service.complete("one", None)
    _, result = await service.complete("two", conversation_id)

    assert result.status == "incomplete"
    conversation = await store.get(conversation_id)
    assert conversation is not None
    # turn 1 (2 messages) + truncated turn 2: user + partial assistant reply.
    assert [(m.role, m.content) for m in conversation.messages][-2:] == [
        ("user", "two"),
        ("assistant", "par"),
    ]


async def test_content_filter_truncation_leaves_no_trace() -> None:
    scripted = ChatResult(message="bad", status="incomplete", incomplete_reason="content_filter")
    service, store = make_conversation_service(scripted)

    conversation_id, _ = await service.complete("one", None)
    _, result = await service.complete("two", conversation_id)

    assert result.status == "incomplete"
    assert result.incomplete_reason == "content_filter"
    conversation = await store.get(conversation_id)
    assert conversation is not None
    # The discarded turn left nothing: transcript still holds only turn 1.
    assert len(conversation.messages) == 2


async def test_empty_completed_reply_is_still_an_upstream_failure() -> None:
    scripted = ChatResult(message="", status="completed")
    service, _ = make_conversation_service(scripted)

    conversation_id, _ = await service.complete("one", None)
    with pytest.raises(UpstreamServiceError):
        await service.complete("two", conversation_id)


async def test_empty_max_output_tokens_reply_commits_the_user_turn_only() -> None:
    # All budget burned on hidden reasoning, no visible text: the turn is
    # keepable per the Day 6 rule, so the user message still enters history.
    scripted = ChatResult(message="", status="incomplete", incomplete_reason="max_output_tokens")
    service, store = make_conversation_service(scripted)

    conversation_id, _ = await service.complete("one", None)
    _, result = await service.complete("two", conversation_id)

    assert result.status == "incomplete"
    conversation = await store.get(conversation_id)
    assert conversation is not None
    assert [(m.role, m.content) for m in conversation.messages][-1:] == [("user", "two")]


# --- API contract ---


def test_chat_response_reports_completed_status(client: TestClient) -> None:
    body = client.post("/api/v1/chat", json={"message": "ping"}).json()

    assert body["status"] == "completed"
    assert body["incomplete_reason"] is None


def test_chat_response_reports_truncation(client: TestClient) -> None:
    scripted = ChatResult(message="par", status="incomplete", incomplete_reason="max_output_tokens")
    service = ConversationChatService(ScriptedChat(scripted), InMemoryConversationStore())
    app.dependency_overrides[get_conversation_service] = lambda: service

    client.post("/api/v1/chat", json={"message": "one"})
    body = client.post("/api/v1/chat", json={"message": "two"}).json()

    assert body["status"] == "incomplete"
    assert body["incomplete_reason"] == "max_output_tokens"
    assert body["message"] == "par"


# --- usage-log scope honesty (review r01 finding 5) ---


def test_no_usage_line_is_logged_without_usage(caplog: pytest.LogCaptureFixture) -> None:
    # Failed events and exceptions reach _log_llm_usage with None (no
    # usage-bearing terminal): the documented behavior is silence, not a
    # fabricated number — reconciliation belongs to Cost Management.
    import logging

    with caplog.at_level(logging.INFO):
        _log_llm_usage(None)

    assert not [r for r in caplog.records if "llm usage" in r.getMessage()]
