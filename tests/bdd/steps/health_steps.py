from behave import given, then, when


@given("the API service is running")
def step_api_service_running(context) -> None:  # type: ignore[no-untyped-def]
    assert context.client is not None


@when("I request the health endpoint")
def step_request_health(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.get("/health")


@then('the response JSON should contain status "{status}"')
def step_health_status(context, status: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response is not None
    assert context.response.json()["status"] == status
