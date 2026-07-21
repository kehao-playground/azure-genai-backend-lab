"""Azure OpenAI chat adapter (v1 GA API, Responses API).

Uses the plain ``openai.AsyncOpenAI`` client against ``<endpoint>/openai/v1/`` —
no ``api-version`` and no Azure-specific client since the v1 GA API (2025-08).
The ``model`` argument is the *deployment name*, not the model name.

Calls go through the Responses API with ``store=False``: conversation state
stays in this application (Day 7), not in Azure's default 30-day retention.
SDK exceptions are translated into :class:`UpstreamError` subclasses at this
boundary, so the API layer never imports ``openai``.

Fake vs. real is selected once in :func:`build_chat_service`; handlers depend
only on the :class:`ChatService` protocol.
"""

from dataclasses import dataclass
from typing import Protocol

import openai
from openai import AsyncOpenAI

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import (
    ConfigurationError,
    ContentFilteredError,
    InvalidInputError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)


@dataclass(frozen=True)
class ChatResult:
    message: str
    model: str | None = None


class ChatService(Protocol):
    async def complete(self, message: str) -> ChatResult: ...


class FakeChatService:
    """Deterministic stand-in so development and tests never touch Azure."""

    async def complete(self, message: str) -> ChatResult:
        return ChatResult(message=f"[fake-llm] {message}", model="fake")


def _translate_upstream_error(exc: openai.OpenAIError) -> UpstreamError:
    if isinstance(exc, openai.APITimeoutError):
        return UpstreamTimeoutError(str(exc))
    if isinstance(exc, openai.RateLimitError):
        return UpstreamThrottledError(str(exc))
    if isinstance(
        exc,
        openai.AuthenticationError | openai.PermissionDeniedError | openai.NotFoundError,
    ):
        return ConfigurationError(str(exc))
    if isinstance(exc, openai.BadRequestError):
        if exc.code == "content_filter":
            return ContentFilteredError(str(exc))
        if exc.code == "context_length_exceeded":
            return InvalidInputError(str(exc))
        # Unknown 400: don't guess whose fault it is — neither "fix your input"
        # nor "we are misconfigured" is provable. Log it, report upstream failure.
        return UpstreamServiceError(str(exc))
    return UpstreamServiceError(str(exc))


class AzureOpenAIChatService:
    def __init__(self, client: AsyncOpenAI, deployment_name: str) -> None:
        self._client = client
        self._deployment_name = deployment_name

    async def complete(self, message: str) -> ChatResult:
        try:
            response = await self._client.responses.create(
                model=self._deployment_name,  # still the deployment name
                input=message,
                store=False,  # state ownership stays with us (Day 7)
            )
        except openai.OpenAIError as exc:
            raise _translate_upstream_error(exc) from exc
        return ChatResult(message=response.output_text, model=response.model)


def build_chat_service(settings: Settings) -> ChatService:
    """Composition point: the only place that decides fake vs. real."""
    if settings.use_fake_llm:
        return FakeChatService()
    if not (
        settings.azure_openai_endpoint
        and settings.azure_openai_api_key
        and settings.azure_openai_deployment_name
    ):
        raise ValueError(
            "USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT, "
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT_NAME"
        )
    client = AsyncOpenAI(
        api_key=settings.azure_openai_api_key.get_secret_value(),
        base_url=settings.azure_openai_endpoint.rstrip("/") + "/openai/v1/",
        timeout=settings.llm_timeout_seconds,  # per attempt (default 30s), not end-to-end
        max_retries=settings.llm_max_retries,  # explicit policy; the SDK default is 2
    )
    return AzureOpenAIChatService(client, settings.azure_openai_deployment_name)
