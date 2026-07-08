from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(tags=["rag"])


class RagRequest(BaseModel):
    question: str = Field(min_length=1)
    tenant_id: str | None = None


class RagResponse(BaseModel):
    answer: str
    sources: list[str]
    correlation_id: str


@router.post("/rag", response_model=RagResponse)
async def rag(_: RagRequest) -> RagResponse:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "not_implemented",
            "message": "RAG API will be implemented in Day 14.",
        },
    )
