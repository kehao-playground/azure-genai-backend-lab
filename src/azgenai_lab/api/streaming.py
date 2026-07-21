"""SSE streaming endpoint (Day 6).

Wire vocabulary — ours, not the upstream's:

- ``message.delta``  ``{"text": "..."}``
- ``message.done``   ``{"status": "completed" | "incomplete",
  "incomplete_reason"?: "max_output_tokens" | "content_filter" | "other",
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

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from azgenai_lab.api.chat import get_chat_service
from azgenai_lab.core.errors import ErrorEnvelope, UpstreamError, UpstreamServiceError
from azgenai_lab.services.azure_openai import ChatService, StreamDone, TextDelta

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

_SSE_EXAMPLE = (
    'event: message.delta\ndata: {"text": "pon"}\n\n'
    'event: message.delta\ndata: {"text": "g"}\n\n'
    'event: message.done\ndata: {"status": "completed", "correlation_id": "..."}\n\n'
)

# Same upstream mapping as /chat, but on this endpoint it only applies before
# the stream starts; after the 200, failures arrive as SSE ``error`` events.
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorEnvelope,
        "description": "Input rejected before the stream starts: content filter or invalid input",
    },
    500: {"model": ErrorEnvelope, "description": "Service misconfiguration"},
    502: {"model": ErrorEnvelope, "description": "Upstream LLM service failure"},
    503: {"model": ErrorEnvelope, "description": "Upstream capacity exhausted"},
    504: {"model": ErrorEnvelope, "description": "Upstream timeout"},
}

_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Server-Sent Events stream. Event vocabulary: `message.delta` "
            "(`{text}`), `message.done` (`{status, incomplete_reason?, "
            "correlation_id}`), `error` (the error envelope). Exactly one "
            "terminal event (`message.done` or `error`) ends a normally "
            "closed stream; clients must treat EOF without a terminal as a "
            "failure and must ignore unknown event names. OpenAPI cannot "
            "express these ordering invariants — the BDD feature "
            "`streaming_response.feature` is the executable contract."
        ),
        "content": {"text/event-stream": {"schema": {"type": "string"}, "example": _SSE_EXAMPLE}},
    },
    **_ERROR_RESPONSES,
}


class StreamingChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = Field(
        default=None,
        description="Reserved for conversation state (Day 7); accepted but ignored today.",
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


@router.post("/chat/stream", response_class=EventStreamResponse, responses=_STREAM_RESPONSES)
async def stream_chat(
    payload: StreamingChatRequest,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> EventStreamResponse:
    # Two-phase boundary: pre-stream failures raise here → HTTP envelope.
    events = await service.open_stream(payload.message)
    return EventStreamResponse(
        _render_sse(events, request.state.correlation_id),
        headers={"Cache-Control": "no-cache"},
    )
