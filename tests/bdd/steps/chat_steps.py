from collections.abc import Sequence

from behave import given, then, when

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import InvalidInputError
from azgenai_lab.main import app
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.services.conversation import ConversationChatService


@given("a valid chat request")
def step_valid_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello"}


@given("a chat request with an empty message")
def step_empty_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": ""}


class RejectingChatService:
    """Stands in for the adapter after it translated an upstream input rejection."""

    async def complete(self, items: Sequence[ReplayItem]) -> object:
        raise InvalidInputError("upstream detail")


@given("the upstream model rejects the input")
def step_upstream_rejects_input(context) -> None:  # type: ignore[no-untyped-def]
    # Wrap the app's own store so conversations started earlier in the
    # scenario stay visible: only the LLM boundary fails. Attribution is the
    # app's own too — the failure event still needs it, since the provider
    # boundary was reached.
    store = app.state.conversation_service._store
    service = ConversationChatService(
        RejectingChatService(),  # type: ignore[arg-type]
        store,
        audit_attribution=app.state.conversation_service.audit_attribution,
    )
    app.dependency_overrides[get_conversation_service] = lambda: service


@when("the upstream model recovers")
def step_upstream_recovers(context) -> None:  # type: ignore[no-untyped-def]
    app.dependency_overrides.pop(get_conversation_service, None)


@given('the caller sends the correlation id "{value}"')
def step_caller_correlation_id(context, value: str) -> None:  # type: ignore[no-untyped-def]
    context.sent_correlation_id = value


@when("I submit the request to the chat endpoint")
def step_submit_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    headers = {}
    sent = getattr(context, "sent_correlation_id", None)
    if sent is not None:
        headers["X-Correlation-Id"] = sent
    context.response = context.client.post(
        "/api/v1/chat", json=context.payload, headers=headers
    )


@then("the echoed correlation id should differ from the one sent")
def step_echoed_correlation_differs(context) -> None:  # type: ignore[no-untyped-def]
    # Day 27: an unusable value is replaced rather than refused. The request
    # still succeeds -- the error contract did not change -- but the id the
    # caller gets back is the backend's own.
    assert context.response is not None
    echoed = context.response.headers["X-Correlation-Id"]
    assert echoed != context.sent_correlation_id
    assert context.response.json()["correlation_id"] == echoed


@then('the echoed correlation id should be "{value}"')
def step_echoed_correlation_equals(context, value: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response is not None
    assert context.response.headers["X-Correlation-Id"] == value
    assert context.response.json()["correlation_id"] == value


class TruncatingChatService:
    """Stands in for the adapter after upstream hit max_output_tokens."""

    async def complete(self, items: Sequence[ReplayItem]) -> object:
        from azgenai_lab.services.azure_openai import ChatResult

        return ChatResult(message="par", status="incomplete", incomplete_reason="max_output_tokens")


@given("the upstream truncates the reply at the output token cap")
def step_upstream_truncates(context) -> None:  # type: ignore[no-untyped-def]
    store = app.state.conversation_service._store
    service = ConversationChatService(
        TruncatingChatService(),  # type: ignore[arg-type]
        store,
        audit_attribution=app.state.conversation_service.audit_attribution,
    )
    app.dependency_overrides[get_conversation_service] = lambda: service


@then('the response JSON should report status "{status}" with reason "{reason}"')
def step_response_status_and_reason(context, status: str, reason: str) -> None:  # type: ignore[no-untyped-def]
    body = context.response.json()
    assert body["status"] == status
    assert body["incomplete_reason"] == reason
