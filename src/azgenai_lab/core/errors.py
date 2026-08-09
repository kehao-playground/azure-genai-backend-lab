import logging
from typing import TYPE_CHECKING, Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from azgenai_lab.core.audit import (
    agent_base_fields,
    agent_rejected_event,
    chat_base_fields,
    chat_rejected_event,
    duration_since,
    emit_audit_event,
    rag_base_fields,
    rag_rejected_event,
)

if TYPE_CHECKING:
    # core.audit never imports this module (see its module docstring); the
    # reverse is fine, and TYPE_CHECKING keeps it from being a runtime edge.
    from azgenai_lab.core.audit import (
        AgentAuditTerminalSnapshot,
        AgentRunRejected,
        ChatCommitSnapshot,
        ChatTurnRejected,
        RagQueryRejected,
    )

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """The one error shape every non-2xx response uses (Day 3 contract).

    ``correlation_id`` is non-optional: the correlation middleware runs outside
    every exception handler, so an envelope without it is a bug, not a case.
    """

    error: ErrorDetail
    correlation_id: str


class UpstreamError(Exception):
    """Backend dependency failure translated into the client-facing error
    contract — the LLM upstream and, since Day 7, conversation storage.

    Adapters raise these instead of leaking SDK exceptions, so the API layer
    never imports the SDK. ``upstream_detail`` may contain endpoint or
    deployment details — it goes to the log, never into the response.
    """

    status_code: int = 502
    code: str = "upstream_error"
    message: str = "The upstream LLM service failed."

    def __init__(self, upstream_detail: str | None = None) -> None:
        super().__init__(self.message)
        self.upstream_detail = upstream_detail


class ConfigurationError(UpstreamError):
    """Our deployment is broken (bad key, wrong deployment name) — not the client's fault."""

    status_code = 500
    code = "configuration_error"
    message = "The service is misconfigured; this request cannot succeed."


class ContentFilteredError(UpstreamError):
    """The prompt was blocked by the content filter — a client-owned upstream 4xx."""

    status_code = 400
    code = "content_filtered"
    message = "The message was blocked by the content filter."


class InvalidInputError(UpstreamError):
    """The upstream model rejected the input itself (e.g. context length) — client-owned."""

    status_code = 400
    code = "invalid_input"
    message = "The upstream model rejected the input (for example, it exceeds the context window)."


class ContextLengthExceededError(InvalidInputError):
    """The upstream model rejected the input specifically for context length.

    Same wire contract as ``InvalidInputError`` (400 ``invalid_input``) — on
    ``/chat`` the caller composed the prompt, so a too-long prompt really is
    caller-owned. The subtype exists so that boundaries where the *server*
    composed the prompt (``/rag``: bounded question + server-selected
    sources) can reclassify this one failure as server-owned without
    touching any other upstream-400 semantics (Day 14 review r08)."""


class StorageError(UpstreamError):
    """Conversation storage failed — our dependency, not the client's fault.

    Raised after inference has already happened (and consumed tokens): the reply
    exists but could not be committed. Mapping it through the shared error
    machinery keeps both contracts intact — HTTP 500 envelope before a
    response is returned, SSE ``error`` terminal after a 200 (review r01
    finding 3). Retrying such a failure repeats inference and billing.
    """

    status_code = 500
    code = "storage_error"
    message = "Conversation storage failed; the turn was not saved."


class StorageCommitError(StorageError):
    """Commit-path storage failure (base), distinct from the load path's bare
    ``StorageError`` (Day 22). A finalizer keys ``provider_call_attempted`` off
    this subtype — the load path never reached the provider, the commit path
    always did — never off ``__cause__`` or an exception message, which are
    both log-only and not part of the typed contract.
    """


class ChatStorageCommitError(StorageCommitError):
    """A ``/chat`` turn's commit failed after the provider already replied.

    ``audit_snapshot`` is required, not optional: every commit call site has
    terminal data in hand (the reply that just arrived), so there is no case
    where this can legitimately be raised without it — a caller that cannot
    supply one has a different bug.
    """

    def __init__(
        self, upstream_detail: str | None = None, *, audit_snapshot: "ChatCommitSnapshot"
    ) -> None:
        super().__init__(upstream_detail)
        self.audit_snapshot = audit_snapshot


class AgentStorageCommitError(StorageCommitError):
    """An ``/agent`` run's commit failed after the provider already ran.

    ``audit_snapshot`` is required for the same reason ``ChatStorageCommitError``'s
    is: the commit call site (``AgentTurnService._commit``) has the run's
    terminal snapshot in hand — the run that just completed — so there is no
    case where this can legitimately be raised without one.
    """

    def __init__(
        self, upstream_detail: str | None = None, *,
        audit_snapshot: "AgentAuditTerminalSnapshot",
    ) -> None:
        super().__init__(upstream_detail)
        self.audit_snapshot = audit_snapshot


class UpstreamThrottledError(UpstreamError):
    """Upstream capacity is exhausted — our quota problem, not the client's request rate."""

    status_code = 503
    code = "upstream_throttled"
    message = "Upstream capacity is exhausted; retry later."


class UpstreamTimeoutError(UpstreamError):
    status_code = 504
    code = "upstream_timeout"
    message = "The upstream LLM call timed out."


class UpstreamServiceError(UpstreamError):
    status_code = 502
    code = "upstream_error"
    message = "The upstream LLM service failed."


class AgentRunUpstreamError(UpstreamServiceError):
    """An agent run failed at the framework/provider boundary
    (``AgentRunError`` from ``services/agent_framework.py``). Same wire
    contract as ``UpstreamServiceError`` (502 ``upstream_error``) — the
    subtype exists only to carry the terminal snapshot the adapter had in
    hand at the point of failure (spec §4b: success and error snapshots
    share one shape), required for the same reason the storage-commit
    errors' is: every raise site has one, a caller that cannot supply one
    has a different bug.
    """

    def __init__(
        self, upstream_detail: str | None = None, *,
        audit_snapshot: "AgentAuditTerminalSnapshot",
    ) -> None:
        super().__init__(upstream_detail)
        self.audit_snapshot = audit_snapshot


def upstream_outcome(exc: UpstreamError) -> Literal["rejected", "error"]:
    """4xx is a rejection of this request; anything else (5xx) is a failure
    that was not this caller's fault, even if a client retry might still
    trigger it again — the split an audit outcome field needs."""
    return "rejected" if 400 <= exc.status_code < 500 else "error"


def chat_upstream_audit_args(
    exc: UpstreamError,
) -> tuple[Literal["rejected", "error"], str, bool, "ChatCommitSnapshot | None"]:
    """(outcome, error_code, provider_call_attempted, snapshot) — the pure
    exception -> primitive classification for any chat-turn caller that needs
    to call ``core.audit.chat_failure_event`` without core.audit importing
    this module. Public for two callers, not one: ``/chat`` (non-streaming)
    and ``/chat/stream`` both raise this same exception hierarchy out of the
    same ``ConversationChatService``, so both finalizers classify a failure
    the same way — the streaming finalizer is not a special case here, it is
    the second normal user. A ``ChatStorageCommitError`` means the provider
    ran and only the commit failed (attempted=true, its snapshot carried
    through); a bare ``StorageError`` means the load path never reached the
    provider (attempted=false); anything else is an upstream failure that did
    reach the provider (attempted=true, no snapshot — there was no commit to
    carry one from).
    """
    if isinstance(exc, ChatStorageCommitError):
        return upstream_outcome(exc), exc.code, True, exc.audit_snapshot
    if isinstance(exc, StorageError):
        return upstream_outcome(exc), exc.code, False, None
    return upstream_outcome(exc), exc.code, True, None


def agent_upstream_audit_args(
    exc: UpstreamError,
) -> tuple[Literal["rejected", "error"], str, bool, "AgentAuditTerminalSnapshot | None"]:
    """(outcome, error_code, provider_call_attempted, snapshot) — the agent
    equivalent of ``chat_upstream_audit_args``, same classification order for
    the same reason: ``AgentStorageCommitError`` (a ``StorageCommitError``
    subclass) must be checked before the bare ``StorageError`` case or every
    commit failure would fall into the load-path branch instead. A
    ``AgentStorageCommitError`` means the run completed and only the commit
    failed (attempted=true, its snapshot carried through); a bare
    ``StorageError`` means the load path never reached the provider
    (attempted=false, no run ever started); an ``AgentRunUpstreamError`` means
    the run itself failed at the framework/provider boundary (attempted=true,
    its degraded snapshot carried through); anything else reached the
    provider with no snapshot to carry (attempted=true, snapshot=None).
    """
    if isinstance(exc, AgentStorageCommitError):
        return upstream_outcome(exc), exc.code, True, exc.audit_snapshot
    if isinstance(exc, StorageError):
        return upstream_outcome(exc), exc.code, False, None
    if isinstance(exc, AgentRunUpstreamError):
        return upstream_outcome(exc), exc.code, True, exc.audit_snapshot
    return upstream_outcome(exc), exc.code, True, None


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if isinstance(exc.detail, dict):
        error = exc.detail
    else:
        error = {"code": "http_error", "message": str(exc.detail)}
    correlation_id: str = request.state.correlation_id
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error, "correlation_id": correlation_id},
        # Preserves headers set on the raised HTTPException (e.g. the 401
        # WWW-Authenticate challenge from require_principal) — without this,
        # the envelope is right but the challenge header silently vanishes.
        headers=getattr(exc, "headers", None),
    )


# event, streaming — one entry per route this handler covers. A route
# missing from this map (or a path FastAPI never dispatched, e.g. a typo)
# gets no 422 audit event, same as the unauthenticated case below: silence
# over fabrication.
_AUDIT_422_ROUTES: dict[str, tuple[str, bool]] = {
    "/api/v1/chat": ("chat.turn", False),
    "/api/v1/chat/stream": ("chat.turn", True),
    "/api/v1/rag": ("rag.query", False),
    "/api/v1/agent": ("agent.run", False),
}


def _emit_422_event(request: Request) -> None:
    """A malformed-JSON body fails validation before FastAPI runs the route's
    dependencies — including `require_principal` — so there is no resolved
    identity and no `request.state.audit_start` (set by the correlation
    middleware, which does run, but the two are checked together defensively
    rather than assuming one implies the other). `request.state.principal`
    is stashed by `require_principal` (api/principal.py) precisely so this
    handler can tell "authenticated but bad body" (emit a rejected event)
    apart from "never authenticated" (emit nothing) — an event claiming an
    identity that was never verified would be a fabrication, not a record.
    """
    principal = getattr(request.state, "principal", None)
    route = _AUDIT_422_ROUTES.get(request.url.path)
    start = getattr(request.state, "audit_start", None)
    if principal is None or route is None or start is None:
        return
    event_type, streaming = route
    duration = duration_since(start)
    event: "ChatTurnRejected | RagQueryRejected | AgentRunRejected"
    if event_type == "chat.turn":
        event = chat_rejected_event(
            base=chat_base_fields(
                tenant_id=principal.tenant_id, user_id=principal.user_id,
                correlation_id=request.state.correlation_id,
                conversation_id=None, streaming=streaming,
            ),
            duration_ms=duration, error_code="validation_error",
        )
    elif event_type == "rag.query":
        event = rag_rejected_event(
            base=rag_base_fields(
                tenant_id=principal.tenant_id, user_id=principal.user_id,
                correlation_id=request.state.correlation_id,
            ),
            duration_ms=duration, error_code="validation_error",
        )
    else:
        event = agent_rejected_event(
            base=agent_base_fields(
                tenant_id=principal.tenant_id, user_id=principal.user_id,
                correlation_id=request.state.correlation_id, conversation_id=None,
            ),
            duration_ms=duration, error_code="validation_error",
        )
    emit_audit_event(event)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    _emit_422_event(request)
    message = "; ".join(
        f"{'.'.join(str(loc) for loc in error['loc'] if loc != 'body')}: {error['msg']}"
        for error in exc.errors()
    )
    correlation_id: str = request.state.correlation_id
    return JSONResponse(
        status_code=422,
        content={
            "error": {"code": "validation_error", "message": message},
            "correlation_id": correlation_id,
        },
    )


async def upstream_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, UpstreamError)
    correlation_id: str = request.state.correlation_id
    logger.warning(
        "upstream failure code=%s correlation_id=%s detail=%s",
        exc.code,
        correlation_id,
        exc.upstream_detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message},
            "correlation_id": correlation_id,
        },
    )
