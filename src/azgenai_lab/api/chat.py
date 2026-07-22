from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from azgenai_lab.core.errors import ErrorEnvelope
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.conversation import (
    ConversationChatService,
    ConversationNotFoundError,
    TokenBudgetExceededError,
)

router = APIRouter(tags=["chat"])

# The upstream error contract is part of the API contract: every promised
# status code is documented here so the OpenAPI drift check guards it.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Input rejected: content filter or invalid input"},
    404: {"model": ErrorEnvelope, "description": "Unknown conversation_id"},
    429: {"model": ErrorEnvelope, "description": "Conversation token budget exhausted"},
    500: {"model": ErrorEnvelope, "description": "Service misconfiguration or storage failure"},
    502: {"model": ErrorEnvelope, "description": "Upstream LLM service failure"},
    503: {"model": ErrorEnvelope, "description": "Upstream capacity exhausted"},
    504: {"model": ErrorEnvelope, "description": "Upstream timeout"},
}


def get_conversation_service(request: Request) -> ConversationChatService:
    """Resolve the app-wide service built once at startup (fail fast on bad config)."""
    service: ConversationChatService = request.app.state.conversation_service
    return service


def conversation_not_found() -> HTTPException:
    """404 through the shared envelope. "Unknown" deliberately covers both
    never-issued and lost ids: the in-memory store forgets on restart, and a
    persistent store will expire conversations — the client reaction (start a
    new conversation) is the same."""
    return HTTPException(
        status_code=404,
        detail={
            "code": "conversation_not_found",
            "message": "Unknown conversation_id; start a new conversation by omitting it.",
        },
    )


def token_budget_exceeded() -> HTTPException:
    """429 through the shared envelope. The budget is per conversation and
    does not replenish over time, so there is no Retry-After: the remedy is a
    new conversation, not waiting."""
    return HTTPException(
        status_code=429,
        detail={
            "code": "token_budget_exceeded",
            "message": ("This conversation's token budget is exhausted; start a new conversation."),
        },
    )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = Field(
        default=None,
        description=(
            "Continues an existing conversation. Omit to start a new one; the "
            "response returns the id to send on the next turn. Unknown ids are "
            "rejected with 404 conversation_not_found."
        ),
    )


class ChatResponse(BaseModel):
    message: str
    conversation_id: str
    correlation_id: str
    usage: TokenUsage | None = Field(
        default=None,
        description=(
            "Billed tokens for this turn as reported upstream; null only if "
            "the provider omitted its usage block."
        ),
    )


@router.post("/chat", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def chat(
    payload: ChatRequest,
    request: Request,
    service: Annotated[ConversationChatService, Depends(get_conversation_service)],
) -> ChatResponse:
    try:
        conversation_id, result = await service.complete(payload.message, payload.conversation_id)
    except ConversationNotFoundError:
        raise conversation_not_found() from None
    except TokenBudgetExceededError:
        raise token_budget_exceeded() from None
    return ChatResponse(
        message=result.message,
        conversation_id=conversation_id,
        correlation_id=request.state.correlation_id,
        usage=result.usage,
    )
