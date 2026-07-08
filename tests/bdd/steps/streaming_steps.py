from behave import given, when


@given("a valid streaming chat request")
def step_valid_streaming_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello", "conversation_id": "local-test"}


@when("I submit the request to the streaming endpoint")
def step_submit_streaming_request(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/chat/stream", json=context.payload)
