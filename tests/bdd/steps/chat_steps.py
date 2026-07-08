from behave import given, when


@given("a valid chat request")
def step_valid_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello", "conversation_id": "local-test"}


@when("I submit the request to the chat endpoint")
def step_submit_chat_request(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/chat", json=context.payload)
