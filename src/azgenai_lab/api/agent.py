"""POST /api/v1/agent: a conversation-integrated agent turn (Day 18).

The router is field transport only: every semantic decision (scope, budget,
stop mapping, fallback boundary) happened in AgentTurnService or below."""

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from azgenai_lab.api.chat import conversation_not_found, token_budget_exceeded
from azgenai_lab.api.principal import require_principal
from azgenai_lab.core.errors import ErrorEnvelope
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.agent_framework import AgentTaskTooLargeError
from azgenai_lab.services.agent_turn import AgentTurnService
from azgenai_lab.services.conversation import (
    ConversationNotFoundError,
    TokenBudgetExceededError,
)

router = APIRouter(tags=["agent"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Task exceeds the byte boundary"},
    401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials"},
    403: {
        "model": ErrorEnvelope,
        "description": "Authenticated credential lacks required API permission",
    },
    404: {"model": ErrorEnvelope, "description": "Unknown conversation"},
    429: {"model": ErrorEnvelope, "description": "Conversation token budget exhausted"},
    500: {"model": ErrorEnvelope, "description": "Conversation storage failed"},
    502: {"model": ErrorEnvelope, "description": "Agent run failed upstream"},
}


def get_agent_turn_service(request: Request) -> AgentTurnService:
    return cast(AgentTurnService, request.app.state.agent_turn_service)


class AgentRequest(BaseModel):
    task: str = Field(
        description=(
            "The agent's task. Whitespace-only is rejected as 422; the "
            "4,000-UTF-8-byte boundary is enforced by the service as 400."
        )
    )
    conversation_id: str | None = Field(
        default=None,
        description=(
            "Continues an existing conversation (the agent sees prior "
            "user/assistant turns, and its own turn is appended). Omit to "
            "start a new one. Unknown ids are rejected with 404."
        ),
    )

    @field_validator("task")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("task must not be empty or whitespace-only")
        return stripped


class AgentToolCallModel(BaseModel):
    tool_name: str
    arguments: dict[str, Any] | None = Field(
        description="Parsed tool arguments; null when the model emitted unparseable JSON."
    )
    round_index: int
    executed: bool


class AgentResponse(BaseModel):
    answer: str = Field(
        description=(
            "May be empty when incomplete_reason is set: the run hit a limit "
            "before the model authored a final text."
        )
    )
    conversation_id: str
    correlation_id: str
    usage: TokenUsage | None = None
    status: Literal["completed", "incomplete"]
    incomplete_reason: Literal["tool_call_limit", "iteration_limit"] | None = Field(
        default=None,
        description=(
            "Additive extension of the Day 6 vocabulary: tool_call_limit — "
            "the tool budget forced the final answer; iteration_limit — the "
            "model-call budget did. For both the answer is keepable."
        ),
    )
    model_call_count: int
    tool_calls: list[AgentToolCallModel] = Field(
        description=(
            "Execution trace: tool names, parsed arguments and rounds. Tool "
            "outputs are deliberately absent — they reached the model, not "
            "the wire."
        )
    )


@router.post("/agent", response_model=AgentResponse, responses=_ERROR_RESPONSES)
async def agent_turn(
    payload: AgentRequest,
    request: Request,
    service: Annotated[AgentTurnService, Depends(get_agent_turn_service)],
    principal: Annotated[Principal, Depends(require_principal, scope="request")],
) -> AgentResponse:
    try:
        conversation_id, result = await service.run_turn(
            payload.task, payload.conversation_id, principal=principal
        )
    except AgentTaskTooLargeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_input", "message": str(exc)},
        ) from None
    except ConversationNotFoundError:
        raise conversation_not_found() from None
    except TokenBudgetExceededError:
        raise token_budget_exceeded() from None
    return AgentResponse(
        answer=result.answer,
        conversation_id=conversation_id,
        correlation_id=request.state.correlation_id,
        usage=result.usage,
        status=result.status,
        incomplete_reason=result.incomplete_reason,
        model_call_count=result.model_call_count,
        tool_calls=[
            AgentToolCallModel(
                tool_name=call.tool_name,
                arguments=dict(call.arguments) if call.arguments is not None else None,
                round_index=call.round_index,
                executed=call.executed,
            )
            for call in result.tool_calls
        ],
    )
