from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    message: str
    correlation_id: str


@router.post("/chat", response_model=ChatResponse)
async def chat(_: ChatRequest) -> ChatResponse:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "not_implemented",
            "message": "Chat API will be implemented in Day 5.",
        },
    )
