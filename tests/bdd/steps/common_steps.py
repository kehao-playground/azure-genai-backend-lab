from behave import then


@then("the response status code should be {status_code:d}")
def step_response_status_code(context, status_code: int) -> None:  # type: ignore[no-untyped-def]
    assert context.response is not None
    assert context.response.status_code == status_code


@then('the response JSON should contain error "{error}"')
def step_response_json_error(context, error: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response is not None
    assert context.response.json()["error"]["code"] == error
