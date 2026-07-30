from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from azgenai_lab.core.errors import ErrorEnvelope
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.services.rag import RagService

router = APIRouter(tags=["rag"])

# The upstream error contract is part of the API contract: every promised
# status code is documented here so the OpenAPI drift check guards it.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Content filtered"},
    500: {"model": ErrorEnvelope, "description": "Configuration error"},
    502: {"model": ErrorEnvelope, "description": "Upstream service failed"},
    503: {"model": ErrorEnvelope, "description": "Upstream throttled or unavailable"},
    504: {"model": ErrorEnvelope, "description": "Upstream timeout"},
}


def get_rag_service(request: Request) -> RagService:
    """Resolve the app-wide service built once at startup (fail fast on bad config)."""
    service: RagService = request.app.state.rag_service
    return service


class RagRequest(BaseModel):
    # 2,000 characters is the Day 12 conservative char proxy: the worst
    # measured density (2,572 tokens per 2,000 chars, zh) still stays under
    # the embedding model's 8,192-token input ceiling, so a within-contract
    # question can never trigger an upstream embedding-size 400 (Day 14
    # review finding 2 — that misclassification is why embedding rejects
    # stay classified as service-side 500 elsewhere in this stack).
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def _question_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be empty or whitespace-only")
        return stripped


class RagSource(BaseModel):
    number: int
    chunk_id: str
    title: str
    heading_path: str
    score: float
    reranker_score: float | None = None


class RagResponse(BaseModel):
    answer: str | None
    status: Literal["answered", "no_answer"]
    incomplete_reason: Literal["max_output_tokens", "content_filter", "other"] | None = None
    sources: list[RagSource]
    usage: TokenUsage | None = None
    correlation_id: str


@router.post("/rag", response_model=RagResponse, responses=_ERROR_RESPONSES)
async def rag(
    payload: RagRequest,
    request: Request,
    service: Annotated[RagService, Depends(get_rag_service)],
) -> RagResponse:
    result = await service.answer(payload.question)
    sources = [
        RagSource(
            number=number,
            chunk_id=hit.chunk_id,
            title=hit.title,
            heading_path=hit.heading_path,
            score=hit.score,
            reranker_score=hit.reranker_score,
        )
        for number, hit in enumerate(result.hits, start=1)
    ]
    return RagResponse(
        answer=result.answer,
        status=result.status,
        incomplete_reason=result.incomplete_reason,
        sources=sources,
        usage=result.usage,
        correlation_id=request.state.correlation_id,
    )
