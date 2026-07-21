from behave import given, when

from azgenai_lab.api.chat import get_chat_service
from azgenai_lab.core.errors import InvalidInputError
from azgenai_lab.main import app


@given("a valid chat request")
def step_valid_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello", "conversation_id": "local-test"}


@given("a chat request with an empty message")
def step_empty_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": ""}


class RejectingChatService:
    """Stands in for the adapter after it translated an upstream input rejection."""

    async def complete(self, message: str) -> object:
        raise InvalidInputError("upstream detail")


@given("the upstream model rejects the input")
def step_upstream_rejects_input(context) -> None:  # type: ignore[no-untyped-def]
    app.dependency_overrides[get_chat_service] = RejectingChatService


@when("I submit the request to the chat endpoint")
def step_submit_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/chat", json=context.payload)
