from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from azgenai_lab.core.errors import ErrorEnvelope
from azgenai_lab.services.azure_openai import ChatService

router = APIRouter(tags=["chat"])

# The upstream error contract is part of the API contract: every promised
# status code is documented here so the OpenAPI drift check guards it.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Input rejected: content filter or invalid input"},
    500: {"model": ErrorEnvelope, "description": "Service misconfiguration"},
    502: {"model": ErrorEnvelope, "description": "Upstream LLM service failure"},
    503: {"model": ErrorEnvelope, "description": "Upstream capacity exhausted"},
    504: {"model": ErrorEnvelope, "description": "Upstream timeout"},
}


def get_chat_service(request: Request) -> ChatService:
    """Resolve the app-wide service built once at startup (fail fast on bad config)."""
    service: ChatService = request.app.state.chat_service
    return service


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = Field(
        default=None,
        description="Reserved for conversation state (Day 7); accepted but ignored today.",
    )


class ChatResponse(BaseModel):
    message: str
    correlation_id: str


@router.post("/chat", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def chat(
    payload: ChatRequest,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    result = await service.complete(payload.message)
    return ChatResponse(message=result.message, correlation_id=request.state.correlation_id)
