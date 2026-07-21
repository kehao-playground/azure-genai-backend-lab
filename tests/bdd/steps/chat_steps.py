from collections.abc import Sequence

from behave import given, when

from azgenai_lab.api.chat import get_conversation_service
from azgenai_lab.core.errors import InvalidInputError
from azgenai_lab.main import app
from azgenai_lab.models.chat import Message
from azgenai_lab.services.conversation import ConversationChatService


@given("a valid chat request")
def step_valid_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello"}


@given("a chat request with an empty message")
def step_empty_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": ""}


class RejectingChatService:
    """Stands in for the adapter after it translated an upstream input rejection."""

    async def complete(self, messages: Sequence[Message]) -> object:
        raise InvalidInputError("upstream detail")


@given("the upstream model rejects the input")
def step_upstream_rejects_input(context) -> None:  # type: ignore[no-untyped-def]
    # Wrap the app's own store so conversations started earlier in the
    # scenario stay visible: only the LLM boundary fails.
    store = app.state.conversation_service._store
    service = ConversationChatService(RejectingChatService(), store)  # type: ignore[arg-type]
    app.dependency_overrides[get_conversation_service] = lambda: service


@when("the upstream model recovers")
def step_upstream_recovers(context) -> None:  # type: ignore[no-untyped-def]
    app.dependency_overrides.pop(get_conversation_service, None)


@when("I submit the request to the chat endpoint")
def step_submit_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/chat", json=context.payload)
