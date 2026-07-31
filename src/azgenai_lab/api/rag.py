from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from azgenai_lab.api.principal import require_principal
from azgenai_lab.core.errors import ErrorEnvelope
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.rag import RagService

router = APIRouter(tags=["rag"])

# The upstream error contract is part of the API contract: every promised
# status code is documented here so the OpenAPI drift check guards it.
# Codes verified against core/errors.py, services/azure_openai.py,
# services/embeddings.py, and services/azure_search.py as actually
# reachable from RagService.answer() -> Retriever.retrieve() (embed then
# search) -> ChatService.complete() (Day 14 review finding 10). StorageError
# ("storage_error") is deliberately not listed: it is only raised by
# services/conversation.py, which the /rag path never calls.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {
        "model": ErrorEnvelope,
        "description": "unauthorized (missing or malformed identity headers)",
    },
    400: {
        "model": ErrorEnvelope,
        "description": (
            "content_filtered (the question was blocked by the content filter); "
            "invalid_input is deliberately absent — a provider context-length "
            "rejection on this route is server-owned and surfaces as 500 "
            "rag_context_overflow, because the prompt is composed from "
            "server-selected sources, not caller input"
        ),
    },
    500: {
        "model": ErrorEnvelope,
        "description": (
            "configuration_error (our deployment is misconfigured), "
            "embedding_rejected (the embeddings service rejected the input), "
            "search_request_rejected (the search service rejected the request), or "
            "rag_context_overflow (the retrieved context could not fit the model "
            "input budget — either the highest-ranked hit alone exceeds the local "
            "byte guardrail, or the provider rejected the composed prompt for "
            "context length despite it)"
        ),
    },
    502: {
        "model": ErrorEnvelope,
        "description": "upstream_error (the upstream LLM service failed)",
    },
    503: {
        "model": ErrorEnvelope,
        "description": (
            "upstream_throttled (upstream LLM capacity is exhausted) or "
            "search_unavailable (the search service is unavailable)"
        ),
    },
    504: {
        "model": ErrorEnvelope,
        "description": "upstream_timeout (the upstream LLM call timed out)",
    },
}


def get_rag_service(request: Request) -> RagService:
    """Resolve the app-wide service built once at startup (fail fast on bad config)."""
    service: RagService = request.app.state.rag_service
    return service


class RagRequest(BaseModel):
    # 2,000 characters is the Day 12 conservative char proxy, with a
    # universal (not sampled) bound: a UTF-8 character is at most 4 bytes, so
    # 2,000 chars <= 8,000 bytes; since a BPE token decodes to at least one
    # UTF-8 byte, token count <= byte count, so 2,000 chars is <= 8,000
    # tokens, under the embedding model's 8,192-token input ceiling for any
    # input. A within-contract question can therefore never trigger an
    # upstream embedding-size 400 (Day 14 review finding 2 — that
    # misclassification is why embedding rejects stay classified as
    # service-side 500 elsewhere in this stack).
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
    score: float = Field(
        description=(
            "Retrieval ranking signal. In the hybrid path this is an RRF fusion score: it "
            "orders results and is NOT a similarity, confidence, or grounding probability; do "
            "not apply thresholds to it."
        )
    )
    reranker_score: float | None = Field(
        default=None,
        description=(
            "Semantic reranker score (0.0-4.0) when the semantic ranker is enabled, "
            "null otherwise."
        ),
    )


class AnsweredRagResponse(BaseModel):
    status: Literal["answered"]
    answer: str
    sources: Annotated[list[RagSource], Field(min_length=1)]
    usage: TokenUsage | None = Field(
        description="Provider may omit usage even when answered."
    )
    incomplete_reason: Literal["max_output_tokens", "content_filter", "other"] | None
    correlation_id: str


class NoAnswerRagResponse(BaseModel):
    status: Literal["no_answer"]
    answer: None = None
    sources: Annotated[list[RagSource], Field(max_length=0)] = Field(
        default_factory=list
    )
    usage: None = None
    incomplete_reason: None = None
    correlation_id: str


RagResponse = Annotated[
    AnsweredRagResponse | NoAnswerRagResponse, Field(discriminator="status")
]


@router.post("/rag", response_model=RagResponse, responses=_ERROR_RESPONSES)
async def rag(
    payload: RagRequest,
    request: Request,
    service: Annotated[RagService, Depends(get_rag_service)],
    principal: Annotated[Principal, Depends(require_principal, scope="request")],
) -> AnsweredRagResponse | NoAnswerRagResponse:
    result = await service.answer(payload.question, principal)
    correlation_id: str = request.state.correlation_id
    if result.status == "no_answer":
        return NoAnswerRagResponse(status="no_answer", correlation_id=correlation_id)
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
    assert result.answer is not None  # guaranteed by RagAnswer.__post_init__
    return AnsweredRagResponse(
        status="answered",
        answer=result.answer,
        sources=sources,
        usage=result.usage,
        incomplete_reason=result.incomplete_reason,
        correlation_id=correlation_id,
    )
