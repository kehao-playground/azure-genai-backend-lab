import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from azgenai_lab.api import agent, chat, health, rag, streaming
from azgenai_lab.api.principal import build_entra_resolver, build_initial_resolver
from azgenai_lab.core.config import get_settings
from azgenai_lab.core.correlation import correlation_id_middleware
from azgenai_lab.core.errors import (
    ErrorEnvelope,
    UpstreamError,
    http_exception_handler,
    upstream_error_handler,
    validation_error_handler,
)
from azgenai_lab.core.keyed_lock import KeyedLock
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.services.agent_turn import build_agent_turn_service
from azgenai_lab.services.conversation import build_conversation_service
from azgenai_lab.services.conversation_store import build_conversation_store
from azgenai_lab.services.rag import build_rag_service

# Documents the real 422 shape: validation errors go through the envelope too.
_VALIDATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorEnvelope, "description": "Validation Error"}
}

logger = logging.getLogger(__name__)

# The shutdown order, and the app.state attribute each closer lives on.
# tests/unit/test_service_lifecycle.py's _CLOSE_ORDER asserts this exact
# order too and must stay in sync with it.
_LIFESPAN_CLOSERS: tuple[tuple[str, str], ...] = (
    ("principal resolver", "principal_resolver"),
    ("conversation service", "conversation_service"),
    ("rag service", "rag_service"),
    ("agent turn service", "agent_turn_service"),
)


async def _close_with_budget(app: FastAPI, budget_seconds: float) -> None:
    """Close the four app-wide clients under one shared shutdown budget.

    AsyncOpenAI and the httpx clients behind search and JWKS have no other
    shutdown path in a running API process, and would otherwise leak
    connections until the process exits (Day 14 review finding 4) — hence
    one aclose() per closer, every time. What changed (Day 23 review A1) is
    that a hang in any one of them used to strand the rest indefinitely:
    uvicorn's own --timeout-graceful-shutdown bounds request drain only and
    finishes before this function even starts, and Container Apps SIGKILLs
    the process 30s after SIGTERM regardless of what this code is doing.

    The budget is one deadline shared across all four closers, not a
    per-closer timeout: the thing that has to fit inside the remaining grace
    period is the *sum* of however long cleanup takes, and four independent
    per-closer allowances could still add up to more than that. This bounds
    *cooperative* delay only: a closer that keeps doing real work (e.g.
    behind `asyncio.shield`) after being cancelled still takes however long
    that work takes — measured a 1.05s overrun against a 0.05s budget from a
    closer built to do exactly that (Day 23 review, second wave).

    Every closer still runs, in order, even if an earlier one times out or
    raises. This deliberately does *not* preserve the exception behaviour of
    the nested try/finally chain it replaces: that chain was last-wins — an
    exception from a later closer replaced an earlier in-flight one as the
    propagated exception, keeping the earlier one only as `__context__`
    (verified empirically: closers 1 and 3 both raising propagated closer
    3's exception with closer 1's chained into `__context__`). This
    implementation is first-wins instead, by design — but to make up for no
    longer keeping the others in `__context__`, every closer's failure
    (timeout or not, first or not) is logged, so a second or third real
    close failure is never silently dropped even though only the first is
    re-raised.

    Uses `asyncio.timeout` rather than `asyncio.wait_for` so a closer's own
    `TimeoutError` (e.g. from a socket/SSL teardown) can be told apart from
    the budget itself expiring: since Python 3.11, `asyncio.TimeoutError`
    *is* the builtin `TimeoutError`, so `wait_for`'s single `except
    TimeoutError` could not distinguish "the closer raised this" from "I
    cancelled the closer" and would have silently swallowed the former as a
    logged-and-ignored timeout instead of a real failure.
    """
    deadline = time.monotonic() + budget_seconds
    first_exception: Exception | None = None
    for label, attr in _LIFESPAN_CLOSERS:
        # Clamped to 0, never left negative: an already-exhausted budget
        # must still be handed to the timeout machinery rather than skipped
        # outright — "never tried" and "timed out" are different facts, and
        # the log line below is the only observable proof of the
        # difference. (A closer with no internal await point of its own,
        # unlike every real closer in this app, can still complete
        # synchronously even at a zero remaining budget: asyncio.timeout
        # only interrupts at a suspension point, and one that never
        # suspends never gives it the chance.)
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            closer = getattr(app.state, attr)
            async with asyncio.timeout(remaining) as cm:
                await closer.aclose()
        except TimeoutError as exc:
            if cm.expired():
                logger.warning("shutdown cleanup timed out closer=%s", label)
            else:
                # The closer raised its own TimeoutError before the budget
                # actually expired -- a real failure, not a budget timeout.
                logger.warning(
                    "shutdown cleanup closer=%s raised %s: %s", label, type(exc).__name__, exc
                )
                if first_exception is None:
                    first_exception = exc
        except Exception as exc:
            # Class name always present, unlike str(exc): some exceptions
            # (e.g. a bare ConnectionResetError()) stringify to empty,
            # which would otherwise leave this line with no diagnostic
            # content at all.
            logger.warning(
                "shutdown cleanup closer=%s raised %s: %s", label, type(exc).__name__, exc
            )
            if first_exception is None:
                first_exception = exc
    if first_exception is not None:
        raise first_exception


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    try:
        if settings.auth_mode == "entra":
            # Replaces the UninitializedResolver installed by create_app().
            # The only startup work either mode does: headers mode already
            # has a working adapter, which is what lets the bare-TestClient
            # entry points (tests/bdd/environment.py) serve without a lifespan.
            app.state.principal_resolver = await build_entra_resolver(settings)
        yield
    finally:
        # Runs for both paths: normal shutdown, and a startup failure that
        # never reached `yield`.
        await _close_with_budget(app, settings.shutdown_cleanup_budget_seconds)


def create_app() -> FastAPI:
    settings = get_settings()
    # Must run before any request handling: without this, configure_logging()
    # is defined but never called, so INFO logs (including the per-LLM-call
    # prompt_name/prompt_version/correlation_id line) are silently dropped
    # under a plain `uvicorn` run.
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Azure GenAI Backend Lab",
        description="Production-minded Azure GenAI backend patterns with Python and FastAPI.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # Built at startup, not per request: misconfiguration crashes here, not on request #1.
    app.state.settings = settings
    # Synchronous and offline, so an app is fully usable in headers mode the
    # moment it is constructed; in Entra mode this is the sentinel the
    # lifespan replaces, never a header-trust fallback.
    app.state.principal_resolver = build_initial_resolver(settings)
    shared_store = build_conversation_store(settings)
    shared_locks: KeyedLock[tuple[str, str]] = KeyedLock()
    app.state.conversation_service = build_conversation_service(
        settings, store=shared_store, locks=shared_locks
    )
    app.state.rag_service = build_rag_service(settings)
    app.state.agent_turn_service = build_agent_turn_service(
        settings, store=shared_store, locks=shared_locks
    )

    app.middleware("http")(correlation_id_middleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(UpstreamError, upstream_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.include_router(health.router)
    app.include_router(chat.router, prefix="/api/v1", responses=_VALIDATION_RESPONSES)
    # The streaming router declares its own 422 (with explicit application/json
    # content): merging the shared model-based entry here would re-attach the
    # route's text/event-stream media type to it (review r03).
    app.include_router(streaming.router, prefix="/api/v1")
    app.include_router(rag.router, prefix="/api/v1", responses=_VALIDATION_RESPONSES)
    app.include_router(agent.router, prefix="/api/v1", responses=_VALIDATION_RESPONSES)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "docs": "/docs", "health": "/health"}

    return app


app = create_app()
