from behave import given, then, when

from azgenai_lab.api.agent import get_agent_turn_service
from azgenai_lab.main import app
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.agent_framework import AgentRunResult
from azgenai_lab.services.agent_turn import AgentTurnService


@when('I submit an agent task "{task}"')
def step_submit_agent_task(context, task: str) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post("/api/v1/agent", json={"task": task})


@when("I submit an agent task in the same conversation")
def step_submit_agent_task_in_conversation(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/agent",
        json={"task": "recap", "conversation_id": context.conversation_id},
    )


@when("I submit an agent task in an unknown conversation")
def step_submit_agent_task_unknown(context) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/agent", json={"task": "recap", "conversation_id": "never-issued"}
    )


@when('I submit an agent task in the same conversation as group "{group}"')
def step_submit_agent_task_as_group(context, group: str) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/agent",
        json={"task": "recap", "conversation_id": context.conversation_id},
        headers={"X-Tenant-Id": "t1", "X-User-Id": "u1", "X-Group-Ids": group},
    )


@given("a conversation whose token budget is exhausted")
def step_budget_exhausted_conversation(context) -> None:  # type: ignore[no-untyped-def]
    response = context.client.post("/api/v1/chat", json={"message": "Hello"})
    assert response.status_code == 200
    context.conversation_id = response.json()["conversation_id"]
    # Same store, tighter gate: replace the agent service with one whose
    # budget is below the tokens the first turn already committed.
    current: AgentTurnService = app.state.agent_turn_service
    limited = AgentTurnService(
        current._agent_service, current._store, token_budget=1, locks=current._locks
    )
    app.dependency_overrides[get_agent_turn_service] = lambda: limited


class _LimitStoppingAgent:
    """Stands in for the adapter after a run that hit the tool-call limit:
    the fallback was already stripped, so the answer is legitimately empty."""

    async def run(self, task, history, *, principal):  # type: ignore[no-untyped-def]
        return AgentRunResult(
            answer="",
            model_call_count=3,
            tool_round_count=2,
            tool_call_count=2,
            refused_call_count=0,
            stop_reason="function_call_limit",
            limit_reasons=frozenset({"function_call_limit"}),
            tool_calls=(),
            usage=TokenUsage(
                input_tokens=30, output_tokens=15, total_tokens=45, reasoning_tokens=6
            ),
            per_round=None,
        )

    async def aclose(self) -> None:
        return None


@given("the agent service stops at the tool-call limit")
def step_agent_stops_at_limit(context) -> None:  # type: ignore[no-untyped-def]
    current: AgentTurnService = app.state.agent_turn_service
    service = AgentTurnService(
        _LimitStoppingAgent(), current._store, locks=current._locks
    )
    app.dependency_overrides[get_agent_turn_service] = lambda: service


@then('the agent answer should include the marker "{marker}"')
def step_agent_answer_marker(context, marker: str) -> None:  # type: ignore[no-untyped-def]
    assert marker in context.response.json()["answer"]


@then('the response JSON field "{field}" should be "{value}"')
def step_response_field_equals(context, field: str, value: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.json()[field] == value


@then('the response JSON field "{field}" should be empty')
def step_response_field_empty(context, field: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.json()[field] == ""
