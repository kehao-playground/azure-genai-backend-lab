from behave import given, then


@given("the token budget policy is not implemented")
def step_token_budget_not_implemented(context) -> None:  # type: ignore[no-untyped-def]
    context.token_budget_pending = True


@then("the token budget scenario should be marked pending")
def step_token_budget_pending(context) -> None:  # type: ignore[no-untyped-def]
    assert context.token_budget_pending is True
