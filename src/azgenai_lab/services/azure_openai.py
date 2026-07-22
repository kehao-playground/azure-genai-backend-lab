"""Azure OpenAI chat adapter (v1 GA API, Responses API).

Uses the plain ``openai.AsyncOpenAI`` client against ``<endpoint>/openai/v1/`` —
no ``api-version`` and no Azure-specific client since the v1 GA API (2025-08).
The ``model`` argument is the *deployment name*, not the model name.

Calls go through the Responses API with ``store=False``: conversation state
stays in this application (Day 7), not in Azure's default 30-day retention.
Stateless multi-turn therefore replays provider items, not just visible text:
requests ask for ``include=["reasoning.encrypted_content"]`` and results carry
the response output items back as opaque :data:`ReplayItem` dicts, so the next
turn can resend them verbatim — dropping them would silently lose reasoning
context between turns (review r01 finding 1).

SDK exceptions are translated into :class:`UpstreamError` subclasses at this
boundary, so the API layer never imports ``openai``.

Fake vs. real is selected once in :func:`build_chat_service`; handlers depend
only on the :class:`ChatService` protocol.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import openai
from openai import AsyncOpenAI, AsyncStream
from openai.types.responses import ResponseInputParam, ResponseStreamEvent

from azgenai_lab.core.config import Settings
from azgenai_lab.core.correlation import correlation_id_var
from azgenai_lab.core.errors import (
    ConfigurationError,
    ContentFilteredError,
    InvalidInputError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts.loader import PromptTemplate, load_prompt

logger = logging.getLogger(__name__)


def _log_llm_call(prompt: PromptTemplate | None, streaming: bool) -> None:
    # Attribution over metrics: incidents must be able to answer "which
    # prompt version was live on this request?" without asking git.
    prompt_name = prompt.name if prompt else None
    prompt_version = prompt.version if prompt else None
    prompt_sha256 = prompt.sha256 if prompt else None
    prompt_sha256_prefix = prompt_sha256[:12] if prompt_sha256 else None
    correlation_id = correlation_id_var.get()
    logger.info(
        "llm call streaming=%s prompt_name=%s prompt_version=%s prompt_sha256=%s correlation_id=%s",
        streaming,
        prompt_name,
        prompt_version,
        prompt_sha256_prefix,
        correlation_id,
        extra={
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
            "correlation_id": correlation_id,
        },
    )


def _log_llm_usage(usage: TokenUsage | None) -> None:
    # Cost attribution lives in the same place as prompt attribution (Day 8):
    # one log line per billed call, joinable on correlation_id. These are the
    # provider-reported numbers the invoice is built from, not an estimate.
    if usage is None:
        return
    correlation_id = correlation_id_var.get()
    logger.info(
        "llm usage input_tokens=%s output_tokens=%s total_tokens=%s correlation_id=%s",
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        correlation_id,
        extra={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "correlation_id": correlation_id,
        },
    )


def _extract_usage(usage: Any) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
    )


@dataclass(frozen=True)
class ChatResult:
    message: str
    model: str | None = None
    # The response's output items (assistant messages, encrypted reasoning,
    # future tool calls) as opaque dicts — the replay context for the next turn.
    replay_items: tuple[ReplayItem, ...] = ()
    # Billed tokens for this call, as reported upstream; None only when the
    # provider omitted the usage block.
    usage: TokenUsage | None = None


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
    ``replay_items`` carries the terminal response's output items for the
    conversation layer; it never reaches the wire.
    """

    status: Literal["completed", "incomplete"]
    incomplete_reason: IncompleteReason | None = None
    replay_items: tuple[ReplayItem, ...] = ()
    # Billed tokens for the whole stream, from the terminal response's usage
    # block — deltas carry no usage; only the terminal settles the bill.
    usage: TokenUsage | None = None


ChatStreamEvent = TextDelta | StreamDone


class ChatService(Protocol):
    """One inference call over the full replay context (oldest first).

    ``items`` are provider-shaped input items: user turns as role/content
    dicts plus prior responses' output items resent verbatim.
    """

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult: ...

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]: ...


def _fake_reply(items: Sequence[ReplayItem], prompt: PromptTemplate | None) -> str:
    # The history marker makes state visible to contract tests: a fake can't
    # answer "what did I say earlier?", but it can prove the history arrived.
    last = str(items[-1].get("content", ""))
    reply = f"[fake-llm] {last}"
    markers = []
    if len(items) > 1:
        markers.append(f"history={len(items) - 1}")
    if prompt is not None:
        # Proves through the API that the composition path carried the
        # prompt into the adapter — the fake never talks to Azure.
        markers.append(f"prompt={prompt.name}@{prompt.version}")
    if markers:
        reply += f" ({', '.join(markers)})"
    return reply


def _fake_output_item(text: str) -> ReplayItem:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _fake_usage(items: Sequence[ReplayItem]) -> TokenUsage:
    # Deterministic and history-proportional: tests can prove the usage
    # pipeline is wired (and that input grows with the replay context)
    # without a tokenizer.
    input_tokens = 10 * len(items)
    output_tokens = 5
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


class FakeChatService:
    """Deterministic stand-in so development and tests never touch Azure."""

    def __init__(self, prompt: PromptTemplate | None = None) -> None:
        self._prompt = prompt

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        _log_llm_call(self._prompt, streaming=False)
        reply = _fake_reply(items, self._prompt)
        usage = _fake_usage(items)
        _log_llm_usage(usage)
        return ChatResult(
            message=reply,
            model="fake",
            replay_items=(_fake_output_item(reply),),
            usage=usage,
        )

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        _log_llm_call(self._prompt, streaming=True)
        reply = _fake_reply(items, self._prompt)
        usage = _fake_usage(items)

        async def stream() -> AsyncIterator[ChatStreamEvent]:
            yield TextDelta("[fake-llm] ")
            yield TextDelta(reply.removeprefix("[fake-llm] "))
            _log_llm_usage(usage)
            yield StreamDone(
                status="completed", replay_items=(_fake_output_item(reply),), usage=usage
            )

        return stream()


def _to_input(items: Sequence[ReplayItem]) -> ResponseInputParam:
    # The cast is because replay items are opaque dicts at our boundary, not
    # the SDK's TypedDict union.
    return cast(ResponseInputParam, list(items))


def _dump_output_items(output: Sequence[Any]) -> tuple[ReplayItem, ...]:
    # ``mode="json"`` keeps items JSON-serializable for any persistent store;
    # ``exclude_none`` trims noise but keeps encrypted reasoning content.
    return tuple(item.model_dump(mode="json", exclude_none=True) for item in output)


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
                usage = _extract_usage(event.response.usage)
                _log_llm_usage(usage)
                yield StreamDone(
                    status="completed",
                    replay_items=_dump_output_items(event.response.output),
                    usage=usage,
                )
                return
            elif event.type == "response.incomplete":
                details = event.response.incomplete_details
                reason = details.reason if details else None
                mapped: IncompleteReason
                if reason == "max_output_tokens" or reason == "content_filter":
                    mapped = reason
                else:
                    mapped = "other"
                # Incomplete is still billed: the meter ran up to the cutoff.
                usage = _extract_usage(event.response.usage)
                _log_llm_usage(usage)
                yield StreamDone(
                    status="incomplete",
                    incomplete_reason=mapped,
                    replay_items=_dump_output_items(event.response.output),
                    usage=usage,
                )
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
    def __init__(
        self,
        client: AsyncOpenAI,
        deployment_name: str,
        prompt: PromptTemplate,
        max_output_tokens: int,
    ) -> None:
        self._client = client
        self._deployment_name = deployment_name
        self._prompt = prompt
        self._max_output_tokens = max_output_tokens

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        _log_llm_call(self._prompt, streaming=False)
        try:
            response = await self._client.responses.create(
                model=self._deployment_name,  # still the deployment name
                input=_to_input(items),
                # system prompt travels per call, never in history (Day 8)
                instructions=self._prompt.text,
                store=False,  # state ownership stays with us: ConversationStore (Day 7)
                # Stateless multi-turn with a reasoning model: without this,
                # reasoning items come back without content and the replayed
                # history loses reasoning context (review r01 finding 1).
                include=["reasoning.encrypted_content"],
                # Hard per-call output cap (Day 9): an unbounded reply is the
                # fastest way to burn budget. Hitting it yields an incomplete
                # response, not an error.
                max_output_tokens=self._max_output_tokens,
            )
        except openai.OpenAIError as exc:
            raise _translate_upstream_error(exc) from exc
        usage = _extract_usage(response.usage)
        _log_llm_usage(usage)
        return ChatResult(
            message=response.output_text,
            model=response.model,
            replay_items=_dump_output_items(response.output),
            usage=usage,
        )

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        _log_llm_call(self._prompt, streaming=True)
        # Eager open: this await is the two-phase error boundary. Failures here
        # (401/429/timeout…) raise before any byte reaches the client, so they
        # keep their HTTP status codes; only failures after this point are
        # mid-stream and must travel as SSE ``error`` events.
        try:
            stream = await self._client.responses.create(
                model=self._deployment_name,  # still the deployment name
                input=_to_input(items),
                # system prompt travels per call, never in history (Day 8)
                instructions=self._prompt.text,
                store=False,  # state ownership stays with us: ConversationStore (Day 7)
                include=["reasoning.encrypted_content"],  # see complete()
                max_output_tokens=self._max_output_tokens,  # see complete()
                stream=True,
            )
        except openai.OpenAIError as exc:
            raise _translate_upstream_error(exc) from exc
        return _translate_stream(stream)


def build_chat_service(settings: Settings) -> ChatService:
    """Composition point: the only place that decides fake vs. real."""
    # Fail fast: a malformed template must kill startup, not the first request.
    prompt = load_prompt("default_chat")
    if settings.use_fake_llm:
        return FakeChatService(prompt=prompt)
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
    return AzureOpenAIChatService(
        client,
        settings.azure_openai_deployment_name,
        prompt,
        max_output_tokens=settings.llm_max_output_tokens,
    )
