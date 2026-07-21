from behave import given, then, when


@given("a conversation with one completed turn")
def step_conversation_with_turn(context) -> None:  # type: ignore[no-untyped-def]
    response = context.client.post("/api/v1/chat", json={"message": "Hello"})
    assert response.status_code == 200
    context.conversation_id = response.json()["conversation_id"]


@given("a chat request with an unknown conversation id")
def step_unknown_conversation_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"message": "Hello", "conversation_id": "never-issued"}


@when("I submit a follow-up message in the same conversation")
def step_submit_follow_up(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/chat",
        json={"message": "And again", "conversation_id": context.conversation_id},
    )


@when("I stream a follow-up message in the same conversation")
def step_stream_follow_up(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/chat/stream",
        json={"message": "And again", "conversation_id": context.conversation_id},
    )


@then('the reply should include the marker "{marker}"')
def step_reply_includes_marker(context, marker: str) -> None:  # type: ignore[no-untyped-def]
    assert marker in context.response.json()["message"]


@then('the streaming response should include header "{header}"')
def step_streaming_response_header(context, header: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.headers.get(header)


@then('the streamed text should include the marker "{marker}"')
def step_streamed_text_marker(context, marker: str) -> None:  # type: ignore[no-untyped-def]
    assert marker in context.response.text
