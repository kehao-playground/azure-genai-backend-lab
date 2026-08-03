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
        # never reached `yield`. Each close is isolated so one failure cannot
        # strand the remaining app-wide clients (Day 14 review finding 4) —
        # AsyncOpenAI and the httpx clients behind search and JWKS have no
        # other shutdown path in a running API process, and would otherwise
        # leak connections until the process exits.
        try:
            await app.state.principal_resolver.aclose()
        finally:
            try:
                await app.state.conversation_service.aclose()
            finally:
                try:
                    await app.state.rag_service.aclose()
                finally:
                    await app.state.agent_turn_service.aclose()


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
