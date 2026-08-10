"""SSE streaming endpoint (Day 6).

Wire vocabulary — ours, not the upstream's:

- ``message.delta``  ``{"text": "..."}``
- ``message.done``   ``{"status": "completed" | "incomplete",
  "incomplete_reason"?: "max_output_tokens" | "content_filter" | "other",
  "usage"?: {"input_tokens", "output_tokens", "total_tokens"},
  "correlation_id": "..."}``
- ``error``          the Day 3 error envelope, verbatim

Contract: clients must ignore unknown event names (future events are additive).
When the client stays connected and the stream ends normally it receives
exactly one terminal event (``message.done`` or ``error``); EOF without a
terminal must be treated as a failure. The serializer below enforces that
guarantee on our side; nothing can guarantee delivery across a dead socket.

Two-phase error boundary: ``open_stream`` is awaited *before* the
``StreamingResponse`` is built, so pre-stream upstream failures raise here and
keep their HTTP status codes (Day 5 mapping). Only failures after the 200 has
been sent travel as ``error`` events.
"""

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from azgenai_lab.api.chat import (
    conversation_not_found,
    get_conversation_service,
    token_budget_exceeded,
)
from azgenai_lab.api.principal import require_principal
from azgenai_lab.core.audit import (
    AuditAttribution,
    ChatEventBase,
    ChatTurnSuccess,
    chat_base_fields,
    chat_failure_event,
    chat_rejected_event,
    chat_success_event,
    duration_since,
    emit_audit_event,
)
from azgenai_lab.core.errors import UpstreamError, UpstreamServiceError, chat_upstream_audit_args
from azgenai_lab.models.principal import Principal
from azgenai_lab.services.azure_openai import StreamDone, TextDelta
from azgenai_lab.services.conversation import (
    ConversationChatService,
    ConversationNotFoundError,
    TokenBudgetExceededError,
    turn_commits,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

_SSE_EXAMPLE = (
    'event: message.delta\ndata: {"text": "pon"}\n\n'
    'event: message.delta\ndata: {"text": "g"}\n\n'
    'event: message.done\ndata: {"status": "completed", "correlation_id": "..."}\n\n'
)

# The response_class media_type (text/event-stream) would otherwise leak onto
# these documented error responses (review r03): errors here are plain JSON
# envelopes, so their content is declared explicitly instead of via ``model``.
_ENVELOPE_CONTENT: dict[str, Any] = {
    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}
}

# Same upstream mapping as /chat, but on this endpoint it only applies before
# the stream starts; after the 200, failures arrive as SSE ``error`` events.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "content": _ENVELOPE_CONTENT,
        "description": "Input rejected before the stream starts: content filter or invalid input",
    },
    401: {"content": _ENVELOPE_CONTENT, "description": "Missing or invalid credentials"},
    403: {
        "content": _ENVELOPE_CONTENT,
        "description": "Authenticated credential lacks required API permission",
    },
    404: {"content": _ENVELOPE_CONTENT, "description": "Unknown conversation_id"},
    422: {"content": _ENVELOPE_CONTENT, "description": "Validation Error"},
    429: {
        "content": _ENVELOPE_CONTENT,
        "description": "Conversation token budget exhausted (checked before the stream starts)",
    },
    500: {
        "content": _ENVELOPE_CONTENT,
        "description": "Service misconfiguration or storage failure",
    },
    502: {"content": _ENVELOPE_CONTENT, "description": "Upstream LLM service failure"},
    503: {"content": _ENVELOPE_CONTENT, "description": "Upstream capacity exhausted"},
    504: {"content": _ENVELOPE_CONTENT, "description": "Upstream timeout"},
}

_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Server-Sent Events stream. Event vocabulary: `message.delta` "
            "(`{text}`), `message.done` (`{status, incomplete_reason?, "
            "usage?, correlation_id}`; `usage` carries the turn's provider-reported "
            "input/output/total tokens), `error` (the error envelope). Exactly one "
            "terminal event (`message.done` or `error`) ends a normally "
            "closed stream; clients must treat EOF without a terminal as a "
            "failure and must ignore unknown event names. OpenAPI cannot "
            "express these ordering invariants — the BDD feature "
            "`streaming_response.feature` is the executable contract."
        ),
        "headers": {
            "X-Conversation-Id": {
                "description": (
                    "The conversation this stream belongs to; send it as "
                    "conversation_id on the next turn. On a first turn the id "
                    "is provisional: the turn commits only with a keepable "
                    "terminal (`message.done` completed or max_output_tokens) "
                    "— after `error`, content_filter/other, or a disconnect, "
                    "discard it and start a new conversation."
                ),
                "schema": {"type": "string"},
            }
        },
        "content": {"text/event-stream": {"schema": {"type": "string"}, "example": _SSE_EXAMPLE}},
    },
    **_ERROR_RESPONSES,
}


class StreamingChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = Field(
        default=None,
        description=(
            "Continues an existing conversation. Omit to start a new one; the "
            "id comes back in the X-Conversation-Id response header "
            "(provisional until a keepable message.done — see the 200 header "
            "description). Unknown ids are rejected with 404 "
            "conversation_not_found."
        ),
    )


class EventStreamResponse(StreamingResponse):
    """Declares text/event-stream at the class level so OpenAPI documents the
    200 with the real media type instead of an application/json placeholder."""

    media_type = "text/event-stream"


def _sse(event: str, data: dict[str, Any]) -> str:
    # ensure_ascii=False: SSE is UTF-8 by spec; keep CJK output readable.
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _error_event(code: str, message: str, correlation_id: str) -> str:
    return _sse(
        "error",
        {"error": {"code": code, "message": message}, "correlation_id": correlation_id},
    )


async def _render_sse(
    events: AsyncIterator[TextDelta | StreamDone], correlation_id: str
) -> AsyncIterator[str]:
    """Serialize domain events, enforcing the exactly-one-terminal guarantee."""
    try:
        async for event in events:
            if isinstance(event, TextDelta):
                yield _sse("message.delta", {"text": event.text})
            else:
                data: dict[str, Any] = {"status": event.status, "correlation_id": correlation_id}
                if event.status == "incomplete":
                    data["incomplete_reason"] = event.incomplete_reason or "other"
                if event.usage is not None:
                    # Additive field (Day 9): clients that predate it must
                    # ignore unknown fields, per the Day 6 contract.
                    data["usage"] = event.usage.model_dump(exclude_none=True)
                yield _sse("message.done", data)
                return  # terminal sent: no further event may follow
    except UpstreamError as exc:
        logger.warning(
            "mid-stream upstream failure code=%s correlation_id=%s detail=%s",
            exc.code,
            correlation_id,
            exc.upstream_detail,
        )
        yield _error_event(exc.code, exc.message, correlation_id)
        return
    # Upstream EOF without a terminal event: the contract still owes the
    # client exactly one terminal, so the gap itself is an upstream failure.
    logger.warning(
        "upstream stream ended without a terminal event correlation_id=%s", correlation_id
    )
    fallback = UpstreamServiceError()
    yield _error_event(fallback.code, fallback.message, correlation_id)


async def _audit_observed(
    events: AsyncIterator[TextDelta | StreamDone],
    *,
    base: ChatEventBase,
    attribution: AuditAttribution,
    audit_start: float,
) -> AsyncIterator[TextDelta | StreamDone]:
    """Post-transfer audit ownership (spec §3): once iteration has started, the
    200 is already committed to the client, so this generator — not the
    endpoint finalizer — owns the terminal ``chat.turn`` event. Exception
    routing (r6-2): ``UpstreamError`` -> emit + raise (the Day 6 mid-stream
    case, same classification as /chat via ``chat_upstream_audit_args``);
    ``GeneratorExit``/``CancelledError`` -> cancellation (consumer close or
    task cancellation; ``client_disconnect`` names the usual source, not a
    proven attribution), two-state emit + raise (see below); any other
    exception is an out-of-contract bug — NO event, the original exception
    propagates untouched, so a genuine programmer error is never misrecorded
    as a cancellation or a clean success.

    Cancellation two-state: the store commits the turn *before* the terminal
    event is yielded (services/conversation.py's ``_commit_on_done``), so
    once ``seen_done`` is set the commit decision is already made and
    unrelated to whether the client actually received the frame — the audit
    log records commit truth, not delivery truth. Before that point, nothing
    was committed.
    """
    seen_done: StreamDone | None = None

    def _terminal_success() -> ChatTurnSuccess:
        assert seen_done is not None
        return chat_success_event(
            base=base, duration_ms=duration_since(audit_start), attribution=attribution,
            model_version=seen_done.model_version, usage=seen_done.usage,
            status=seen_done.status, incomplete_reason=seen_done.incomplete_reason,
            committed=turn_commits(seen_done.status, seen_done.incomplete_reason),
        )

    try:
        async for event in events:
            if isinstance(event, StreamDone):
                seen_done = event
            yield event
    except UpstreamError as exc:
        outcome, code, attempted, snapshot = chat_upstream_audit_args(exc)
        emit_audit_event(chat_failure_event(
            base=base, duration_ms=duration_since(audit_start), outcome=outcome,
            error_code=code, provider_call_attempted=attempted,
            attribution=attribution, snapshot=snapshot,
        ))
        raise
    except (GeneratorExit, asyncio.CancelledError):
        if seen_done is not None:
            emit_audit_event(_terminal_success())
        else:
            emit_audit_event(chat_failure_event(
                base=base, duration_ms=duration_since(audit_start), outcome="error",
                error_code="client_disconnect", provider_call_attempted=True,
                attribution=attribution,
            ))
        raise
    else:
        # Unreachable through this endpoint on the healthy-200 path:
        # `_render_sse` `return`s right after yielding `message.done`
        # without pulling a next value, so this generator is left suspended
        # at its own `yield` and never reaches loop completion — the
        # `GeneratorExit` branch above emits success instead, once asyncio's
        # async-generator finalizer eventually closes it (see
        # docs/audit-logging.md's "known gap" section). Kept for
        # direct-iteration callers (e.g. the generator-level tests that
        # drive this async generator to natural exhaustion) and as a
        # correct fallback should the endpoint's consumption pattern ever
        # change.
        if seen_done is not None:
            emit_audit_event(_terminal_success())
        else:  # upstream EOF without a terminal: still an upstream failure
            emit_audit_event(chat_failure_event(
                base=base, duration_ms=duration_since(audit_start), outcome="error",
                error_code="upstream_error", provider_call_attempted=True,
                attribution=attribution,
            ))


@router.post("/chat/stream", response_class=EventStreamResponse, responses=_STREAM_RESPONSES)
async def stream_chat(
    payload: StreamingChatRequest,
    request: Request,
    service: Annotated[ConversationChatService, Depends(get_conversation_service)],
    principal: Annotated[Principal, Depends(require_principal, scope="request")],
) -> EventStreamResponse:
    # Two-phase audit ownership (spec §3): this finalizer owns pre-stream
    # failures the same way /chat's finalizer does (api/chat.py) — before the
    # 200 is sent, they are still ordinary HTTP failures through the Day 5
    # envelope. Once open_stream() returns an iterator, ownership moves to
    # _audit_observed, wrapped around it below.
    audit_start: float = request.state.audit_start
    base = chat_base_fields(
        tenant_id=principal.tenant_id, user_id=principal.user_id,
        correlation_id=request.state.correlation_id,
        conversation_id=payload.conversation_id, streaming=True,
    )
    attribution = service.audit_attribution
    if attribution is None:
        # Same composition invariant as /chat: build_conversation_service()
        # always supplies one; a service reaching this endpoint without one
        # is a wiring bug, not a request case (see api/chat.py's identical
        # guard for the rationale).
        raise RuntimeError(
            "ConversationChatService.audit_attribution is required to serve /chat/stream"
        )
    try:
        conversation_id, events = await service.open_stream(
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
    observed = _audit_observed(
        events, base=base, attribution=attribution, audit_start=audit_start
    )
    # The id travels as a header because it must reach the client before the
    # body: SSE consumers read it at response time, not from an event.
    return EventStreamResponse(
        _render_sse(observed, request.state.correlation_id),
        headers={"Cache-Control": "no-cache", "X-Conversation-Id": conversation_id},
    )
