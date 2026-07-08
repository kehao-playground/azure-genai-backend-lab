from behave import given, when


@given("a valid RAG request")
def step_valid_rag_request(context) -> None:  # type: ignore[no-untyped-def]
    context.payload = {"question": "What is this project?", "tenant_id": "local-test"}


@when("I submit the request to the RAG endpoint")
def step_submit_rag_request(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/rag", json=context.payload)
