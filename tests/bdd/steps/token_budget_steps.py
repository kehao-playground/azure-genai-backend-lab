from behave import given, then, when

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.main import app
from azgenai_lab.services.conversation import ConversationChatService


@given("a conversation token budget of {budget:d} tokens")
def step_small_token_budget(context, budget: int) -> None:  # type: ignore[no-untyped-def]
    # Same fake chat service and same store as the app — only the budget
    # shrinks, so turns committed in this scenario count against it.
    current = app.state.conversation_service
    service = ConversationChatService(current._chat_service, current._store, token_budget=budget)
    app.dependency_overrides[get_conversation_service] = lambda: service


@when('I submit a new chat message "{message}"')
def step_submit_new_chat_message(context, message: str) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/chat", json={"message": message})


@then("the response JSON should report token usage")
def step_response_reports_usage(context) -> None:  # type: ignore[no-untyped-def]
    usage = context.response.json()["usage"]
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["total_tokens"] > 0


@then("the message.done event should report token usage")
def step_message_done_reports_usage(context) -> None:  # type: ignore[no-untyped-def]
    assert "message.done" in context.response.text
    assert '"usage"' in context.response.text
    assert '"total_tokens"' in context.response.text
