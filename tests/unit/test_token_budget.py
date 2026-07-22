"""Token budget guardrail (Day 9).

Two mechanisms, one goal — bounded spend:

- Per-call output cap: ``max_output_tokens`` travels on every upstream call
  (asserted in the adapter tests).
- Per-conversation lifetime budget: a post-paid ledger of billed tokens,
  committed atomically with each turn; the check runs *before* inference so an
  exhausted conversation costs nothing further upstream, and rejection maps to
  429 ``token_budget_exceeded`` through the shared envelope.

The ledger records committed turns only: a failed turn is billed upstream but
leaves no trace here — that gap is a deliberate consequence of turn-commit
semantics (Day 7) and is asserted, not hidden.
"""

import pytest
from fastapi.testclient import TestClient

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import UpstreamServiceError
from azgenai_lab.main import app
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.azure_openai import FakeChatService, StreamDone, TextDelta
from azgenai_lab.services.conversation import (
    ConversationChatService,
    TokenBudgetExceededError,
)
from azgenai_lab.services.conversation_store import InMemoryConversationStore


def make_service(
    budget: int | None, chat: FakeChatService | None = None
) -> tuple[ConversationChatService, InMemoryConversationStore]:
    store = InMemoryConversationStore()
    return ConversationChatService(chat or FakeChatService(), store, token_budget=budget), store


# --- fake usage: the deterministic numbers the wiring proofs below rely on ---


async def test_fake_usage_is_history_proportional() -> None:
    result = await FakeChatService().complete([{"role": "user", "content": "hi"}])

    assert result.usage == TokenUsage(
        input_tokens=10, output_tokens=5, total_tokens=15, reasoning_tokens=0
    )


# --- ledger: usage commits with the turn, atomically ---


async def test_committed_turns_accumulate_billed_tokens() -> None:
    service, store = make_service(budget=None)

    conversation_id, _ = await service.complete("one", None)
    await service.complete("two", conversation_id)

    conversation = await store.get(conversation_id)
    assert conversation is not None
    # turn 1: 1 input item -> 15; turn 2: 3 items (user+assistant+user) -> 35.
    assert conversation.total_tokens == 50


async def test_stream_commit_records_the_terminal_usage() -> None:
    service, store = make_service(budget=None)

    conversation_id, events = await service.open_stream("one", None)
    async for _ in events:
        pass

    conversation = await store.get(conversation_id)
    assert conversation is not None
    assert conversation.total_tokens == 15


async def test_failed_turn_leaves_no_ledger_trace() -> None:
    class EmptyReplyChat(FakeChatService):
        """Succeeds on the first turn, returns an empty (failed) reply after."""

        calls = 0

        async def complete(self, items):  # type: ignore[no-untyped-def]
            EmptyReplyChat.calls += 1
            result = await super().complete(items)
            if EmptyReplyChat.calls == 1:
                return result
            return type(result)(message="", usage=result.usage)

    service, store = make_service(budget=None, chat=EmptyReplyChat())

    conversation_id, _ = await service.complete("one", None)
    with pytest.raises(UpstreamServiceError):
        await service.complete("two", conversation_id)

    conversation = await store.get(conversation_id)
    assert conversation is not None
    # The failed turn was billed upstream, but turn-commit semantics win:
    # nothing — transcript, replay items, or tokens — entered the log.
    assert conversation.total_tokens == 15


# --- guardrail: the check fires between turns, before inference ---


async def test_exhausted_budget_rejects_before_calling_upstream() -> None:
    class CountingChat(FakeChatService):
        calls = 0

        async def complete(self, items):  # type: ignore[no-untyped-def]
            CountingChat.calls += 1
            return await super().complete(items)

    chat = CountingChat()
    service, _ = make_service(budget=10, chat=chat)

    conversation_id, _ = await service.complete("one", None)  # 15 committed
    with pytest.raises(TokenBudgetExceededError) as excinfo:
        await service.complete("two", conversation_id)

    assert CountingChat.calls == 1  # the rejected turn never reached upstream
    assert excinfo.value.spent == 15
    assert excinfo.value.budget == 10


async def test_stream_budget_rejection_is_pre_stream() -> None:
    service, _ = make_service(budget=10)

    conversation_id, events = await service.open_stream("one", None)
    async for _ in events:
        pass

    with pytest.raises(TokenBudgetExceededError):
        await service.open_stream("two", conversation_id)


async def test_none_budget_disables_the_guardrail() -> None:
    service, _ = make_service(budget=None)

    conversation_id, _ = await service.complete("one", None)
    _, result = await service.complete("two", conversation_id)

    assert result.message


async def test_a_new_conversation_is_not_affected_by_an_exhausted_one() -> None:
    service, _ = make_service(budget=10)

    exhausted_id, _ = await service.complete("one", None)
    with pytest.raises(TokenBudgetExceededError):
        await service.complete("two", exhausted_id)

    fresh_id, result = await service.complete("hello", None)
    assert fresh_id != exhausted_id
    assert result.message


# --- API contract: 429 envelope and usage in responses ---


def _install(service: ConversationChatService) -> None:
    app.dependency_overrides[get_conversation_service] = lambda: service


def test_chat_response_carries_usage(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "ping"})

    assert response.status_code == 200
    assert response.json()["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "reasoning_tokens": 0,
    }


def test_exhausted_conversation_maps_to_429_envelope(client: TestClient) -> None:
    service, _ = make_service(budget=10)
    _install(service)

    first = client.post("/api/v1/chat", json={"message": "one"})
    conversation_id = first.json()["conversation_id"]
    second = client.post(
        "/api/v1/chat", json={"message": "two", "conversation_id": conversation_id}
    )

    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "token_budget_exceeded"
    assert body["correlation_id"]


def test_streaming_message_done_carries_usage(client: TestClient) -> None:
    response = client.post("/api/v1/chat/stream", json={"message": "ping"})

    assert response.status_code == 200
    assert (
        '"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, '
        '"reasoning_tokens": 0}' in response.text
    )


def test_streaming_exhausted_conversation_maps_to_429_envelope(client: TestClient) -> None:
    service, _ = make_service(budget=10)
    _install(service)

    first = client.post("/api/v1/chat/stream", json={"message": "one"})
    conversation_id = first.headers["X-Conversation-Id"]
    second = client.post(
        "/api/v1/chat/stream", json={"message": "two", "conversation_id": conversation_id}
    )

    # Pre-stream rejection: a plain HTTP envelope, never a 200-then-error.
    assert second.status_code == 429
    assert second.headers["content-type"].startswith("application/json")
    assert second.json()["error"]["code"] == "token_budget_exceeded"


async def test_stream_done_without_usage_omits_the_field() -> None:
    # Defensive: a provider omitting usage must not serialize "usage": null.
    from azgenai_lab.api.streaming import _render_sse

    async def events():  # type: ignore[no-untyped-def]
        yield TextDelta("x")
        yield StreamDone(status="completed")

    text = "".join([chunk async for chunk in _render_sse(events(), "cid")])
    assert '"usage"' not in text
