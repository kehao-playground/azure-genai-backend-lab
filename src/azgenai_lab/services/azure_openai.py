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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

import openai
from openai import AsyncOpenAI, AsyncStream
from openai.types.responses import ResponseStreamEvent

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


IncompleteReason = Literal["max_output_tokens", "content_filter", "other"]


@dataclass(frozen=True)
class TextDelta:
    """One increment of output text; concatenation order is arrival order."""

    text: str


@dataclass(frozen=True)
class StreamDone:
    """Successful terminal event. ``incomplete`` still means usable transport:

    the client decides what to do with the partial text based on
    ``incomplete_reason`` (keep it for ``max_output_tokens``, discard or mask
    it for ``content_filter``, treat it as unusable when ``other``).
    """

    status: Literal["completed", "incomplete"]
    incomplete_reason: IncompleteReason | None = None


ChatStreamEvent = TextDelta | StreamDone


class ChatService(Protocol):
    async def complete(self, message: str) -> ChatResult: ...

    async def open_stream(self, message: str) -> AsyncIterator[ChatStreamEvent]: ...


class FakeChatService:
    """Deterministic stand-in so development and tests never touch Azure."""

    async def complete(self, message: str) -> ChatResult:
        return ChatResult(message=f"[fake-llm] {message}", model="fake")

    async def open_stream(self, message: str) -> AsyncIterator[ChatStreamEvent]:
        async def stream() -> AsyncIterator[ChatStreamEvent]:
            yield TextDelta("[fake-llm] ")
            yield TextDelta(message)
            yield StreamDone(status="completed")

        return stream()


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


def _translate_failed_event(code: str | None, detail: str) -> UpstreamError:
    """``response.failed`` / ``error`` arrive as typed events, not exceptions."""
    if code == "rate_limit_exceeded":
        return UpstreamThrottledError(detail)
    return UpstreamServiceError(detail)


async def _translate_stream(
    stream: AsyncStream[ResponseStreamEvent],
) -> AsyncIterator[ChatStreamEvent]:
    """Translate upstream typed events into domain events; nothing else leaks out.

    Exactly one of three endings: StreamDone is yielded, an UpstreamError is
    raised, or upstream EOFs without a terminal (the API layer treats that as
    a failure). The upstream stream is always closed — including when the
    consumer stops early (client disconnect), which is what stops the meter.
    """
    try:
        async for event in stream:
            if event.type == "response.output_text.delta":
                yield TextDelta(event.delta)
            elif event.type == "response.completed":
                yield StreamDone(status="completed")
                return
            elif event.type == "response.incomplete":
                details = event.response.incomplete_details
                reason = details.reason if details else None
                mapped: IncompleteReason
                if reason == "max_output_tokens" or reason == "content_filter":
                    mapped = reason
                else:
                    mapped = "other"
                yield StreamDone(status="incomplete", incomplete_reason=mapped)
                return
            elif event.type == "response.failed":
                error = event.response.error
                detail = f"{error.code}: {error.message}" if error else "response.failed"
                raise _translate_failed_event(error.code if error else None, detail)
            elif event.type == "error":
                detail = f"error event: {event.code}: {event.message}"
                raise _translate_failed_event(event.code, detail)
    except openai.OpenAIError as exc:
        raise _translate_upstream_error(exc) from exc
    finally:
        await stream.close()


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

    async def open_stream(self, message: str) -> AsyncIterator[ChatStreamEvent]:
        # Eager open: this await is the two-phase error boundary. Failures here
        # (401/429/timeout…) raise before any byte reaches the client, so they
        # keep their HTTP status codes; only failures after this point are
        # mid-stream and must travel as SSE ``error`` events.
        try:
            stream = await self._client.responses.create(
                model=self._deployment_name,  # still the deployment name
                input=message,
                store=False,  # state ownership stays with us (Day 7)
                stream=True,
            )
        except openai.OpenAIError as exc:
            raise _translate_upstream_error(exc) from exc
        return _translate_stream(stream)


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
