from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from azgenai_lab.api import chat, health, rag, streaming
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
from azgenai_lab.services.conversation import build_conversation_service
from azgenai_lab.services.conversation_store import build_conversation_store
from azgenai_lab.services.rag import build_rag_service

# Documents the real 422 shape: validation errors go through the envelope too.
_VALIDATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorEnvelope, "description": "Validation Error"}
}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Built at startup, not per request: misconfiguration crashes here, not on
    # request #1. Kept in the lifespan (rather than moved into it entirely)
    # so app.state is populated before startup completes either way.
    yield
    # Long-lived owned clients (AsyncOpenAI, the search httpx.AsyncClient)
    # have no other shutdown path in a running API process (Day 14 review
    # finding 4) — without this, they leak connections until the process exits.
    await app.state.conversation_service.aclose()
    await app.state.rag_service.aclose()


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
    shared_store = build_conversation_store(settings)
    shared_locks: KeyedLock[tuple[str, str]] = KeyedLock()
    app.state.conversation_service = build_conversation_service(
        settings, store=shared_store, locks=shared_locks
    )
    app.state.rag_service = build_rag_service(settings)

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

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "docs": "/docs", "health": "/health"}

    return app


app = create_app()
