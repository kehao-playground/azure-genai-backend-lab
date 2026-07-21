from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from azgenai_lab.services.azure_openai import ChatService

router = APIRouter(tags=["chat"])


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


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> ChatResponse:
    result = await service.complete(payload.message)
    return ChatResponse(message=result.message, correlation_id=request.state.correlation_id)
