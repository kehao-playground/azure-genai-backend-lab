import dataclasses
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from azgenai_lab.api.principal import require_principal
from azgenai_lab.core.audit import (
    chat_base_fields,
    chat_failure_event,
    chat_rejected_event,
    chat_success_event,
    duration_since,
    emit_audit_event,
)
from azgenai_lab.core.errors import ErrorEnvelope, UpstreamError, chat_upstream_audit_args
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.conversation import (
    ConversationChatService,
    ConversationNotFoundError,
    TokenBudgetExceededError,
    turn_commits,
)

router = APIRouter(tags=["chat"])

# The upstream error contract is part of the API contract: every promised
# status code is documented here so the OpenAPI drift check guards it.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Input rejected: content filter or invalid input"},
    401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials"},
    403: {
        "model": ErrorEnvelope,
        "description": "Authenticated credential lacks required API permission",
    },
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
            "Provider-reported tokens for this turn — a request-level usage "
            "signal for attribution and guardrails, not a billing record; "
            "null only if the provider omitted its usage block."
        ),
    )
    status: Literal["completed", "incomplete"] = Field(
        default="completed",
        description=(
            "Mirror of the stream terminal: `incomplete` means the reply was "
            "truncated — see incomplete_reason for what the client may keep."
        ),
    )
    incomplete_reason: Literal["max_output_tokens", "content_filter", "other"] | None = Field(
        default=None,
        description=(
            "Set only when status is `incomplete`. Same client rules as the "
            "Day 6 SSE vocabulary: keep the partial text for "
            "`max_output_tokens`; discard or mask it for `content_filter`; "
            "treat it as unusable for `other`. For `content_filter`/`other` "
            "the turn is not committed — on a first turn the returned "
            "conversation_id never comes into existence."
        ),
    )


@router.post("/chat", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def chat(
    payload: ChatRequest,
    request: Request,
    service: Annotated[ConversationChatService, Depends(get_conversation_service)],
    principal: Annotated[Principal, Depends(require_principal, scope="request")],
) -> ChatResponse:
    # Exactly one chat.turn event per call: every branch below either emits
    # and raises/returns, or falls through to the single success emission at
    # the end — there is no path that returns without emitting. A non-
    # UpstreamError exception (a bug, not a contract case) is deliberately
    # not caught here: it propagates with no event, per spec.
    audit_start: float = request.state.audit_start
    base = chat_base_fields(
        tenant_id=principal.tenant_id, user_id=principal.user_id,
        correlation_id=request.state.correlation_id,
        conversation_id=payload.conversation_id, streaming=False,
    )
    attribution = service.audit_attribution
    if attribution is None:
        # A success event requires attribution (the schema enforces it); a
        # service reaching /chat without one is a composition bug, not a
        # request case — fail loudly rather than emit an event the schema
        # would reject anyway. build_conversation_service() always supplies
        # one; only direct, non-composed constructions (tests/tools that
        # never emit) may legitimately omit it.
        raise RuntimeError(
            "ConversationChatService.audit_attribution is required to serve /chat"
        )
    try:
        conversation_id, result = await service.complete(
            payload.message, payload.conversation_id, principal=principal
        )
    except ConversationNotFoundError:
        emit_audit_event(chat_rejected_event(
            base=base, duration_ms=duration_since(audit_start),
            error_code="conversation_not_found",
        ))
        raise conversation_not_found() from None
    except TokenBudgetExceededError as exc:
        emit_audit_event(chat_rejected_event(
            base=base, duration_ms=duration_since(audit_start),
            error_code="token_budget_exceeded", spent=exc.spent, budget=exc.budget,
        ))
        raise token_budget_exceeded() from None
    except UpstreamError as exc:
        outcome, code, attempted, snapshot = chat_upstream_audit_args(exc)
        emit_audit_event(chat_failure_event(
            base=base, duration_ms=duration_since(audit_start), outcome=outcome,
            error_code=code, provider_call_attempted=attempted,
            attribution=attribution, snapshot=snapshot,
        ))
        raise
    base = dataclasses.replace(base, conversation_id=conversation_id)
    emit_audit_event(chat_success_event(
        base=base, duration_ms=duration_since(audit_start), attribution=attribution,
        model_version=result.model_version, usage=result.usage,
        status=result.status, incomplete_reason=result.incomplete_reason,
        committed=turn_commits(result.status, result.incomplete_reason),
    ))
    return ChatResponse(
        message=result.message,
        conversation_id=conversation_id,
        correlation_id=request.state.correlation_id,
        usage=result.usage,
        status=result.status,
        incomplete_reason=result.incomplete_reason,
    )
