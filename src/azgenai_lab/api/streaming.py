from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(tags=["streaming"])


class StreamingChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None


@router.post("/chat/stream")
async def stream_chat(_: StreamingChatRequest) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "not_implemented",
            "message": "Streaming API will be implemented in Day 6.",
        },
    )
