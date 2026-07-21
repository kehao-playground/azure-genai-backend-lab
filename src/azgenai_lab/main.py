from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from azgenai_lab.api import chat, health, rag, streaming
from azgenai_lab.core.config import get_settings
from azgenai_lab.core.correlation import correlation_id_middleware
from azgenai_lab.core.errors import (
    UpstreamError,
    http_exception_handler,
    upstream_error_handler,
)
from azgenai_lab.services.azure_openai import build_chat_service


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Azure GenAI Backend Lab",
        description="Production-minded Azure GenAI backend patterns with Python and FastAPI.",
        version="0.1.0",
    )

    # Built at startup, not per request: misconfiguration crashes here, not on request #1.
    app.state.chat_service = build_chat_service(settings)

    app.middleware("http")(correlation_id_middleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(UpstreamError, upstream_error_handler)

    app.include_router(health.router)
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(streaming.router, prefix="/api/v1")
    app.include_router(rag.router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "docs": "/docs", "health": "/health"}

    return app


app = create_app()
