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
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Sequence
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
    ContextLengthExceededError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
    upstream_outcome,
)
from azgenai_lab.core.telemetry import (
    ATTR_FINISH_REASONS,
    FAKE_DEPLOYMENT,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    OwnedLlmSpan,
    instrumented_httpx_client,
    llm_span,
    set_outcome,
    set_usage_attributes,
    start_owned_llm_span,
)
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.azure_openai_auth import resolve_aoai_auth

logger = logging.getLogger(__name__)


def _log_llm_call(prompt: PromptTemplate | None, streaming: bool) -> None:
    # Attribution over metrics: incidents must be able to answer "which
    # prompt version was live on this request?" without asking git.
    #
    # correlation_id is still read and interpolated into this line's message
    # text (the "correlation_id=%s" placeholder below) even though
    # core.logging's record factory (Day 14 review finding 5) now stamps
    # record.correlation_id on every record from the same ContextVar:
    # test_prompt_logging.py pins the literal rendered text, and the
    # rendered message is what incident response actually greps, not just
    # the record's extra attributes. It is no longer duplicated into
    # `extra` — the factory already sets record.correlation_id, and passing
    # it again through `extra` raises `KeyError: Attempt to overwrite` since
    # Python's LogRecord.__init__ (via the factory) has already set that
    # attribute before `extra` is applied.
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
        },
    )


def _log_llm_usage(usage: TokenUsage | None) -> None:
    # Cost attribution lives in the same place as prompt attribution (Day 8),
    # joinable on correlation_id. Scope honestly stated: this line exists only
    # for calls that returned a usage-bearing terminal (non-streaming success,
    # stream completed/incomplete). Failed events, SDK exceptions and client
    # disconnects may still have incurred billable processing upstream with no
    # line here — reconciliation against Cost Management is the authority.
    if usage is None:
        return
    correlation_id = correlation_id_var.get()
    logger.info(
        "llm usage input_tokens=%s output_tokens=%s reasoning_tokens=%s total_tokens=%s "
        "correlation_id=%s",
        usage.input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
        correlation_id,
        extra={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
        },
    )


def _extract_usage(usage: Any) -> TokenUsage | None:
    if usage is None:
        return None
    details = getattr(usage, "output_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        reasoning_tokens=reasoning,
    )


@dataclass(frozen=True)
class ChatResult:
    message: str
    model_version: str | None = None
    # The response's output items (assistant messages, encrypted reasoning,
    # future tool calls) as opaque dicts — the replay context for the next turn.
    replay_items: tuple[ReplayItem, ...] = ()
    # Provider-reported tokens for this call; None only when the provider
    # omitted the usage block.
    usage: TokenUsage | None = None
    # Non-streaming mirror of the stream terminal (Day 9 review r01 finding 2):
    # an incomplete response is a truncation the client must be told about,
    # never disguised as normal success.
    status: Literal["completed", "incomplete"] = "completed"
    incomplete_reason: "IncompleteReason | None" = None


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
    # Provider-reported tokens for the whole stream, from the terminal
    # response's usage block — deltas carry no usage; only the terminal
    # reports the turn's count.
    usage: TokenUsage | None = None
    # The model that actually served this request, per the provider's own
    # terminal response — null, never fabricated, if the provider omitted it.
    model_version: str | None = None


ChatStreamEvent = TextDelta | StreamDone


class ChatService(Protocol):
    """One inference call over the full replay context (oldest first).

    ``items`` are provider-shaped input items: user turns as role/content
    dicts plus prior responses' output items resent verbatim.
    """

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult: ...

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]: ...

    async def aclose(self) -> None: ...


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
        reasoning_tokens=0,  # the fake never reasons; 0 proves the field is wired
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
            model_version="fake",
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
                status="completed",
                replay_items=(_fake_output_item(reply),),
                usage=usage,
                model_version="fake",
            )

        return stream()

    async def aclose(self) -> None:
        """Nothing owned; the fake never opens a client."""


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
            # Subtype, not the generic InvalidInputError: the adapter cannot
            # know who composed the prompt, so it preserves the distinction
            # and lets each boundary assign ownership (/chat: caller 400;
            # /rag: server 500 rag_context_overflow — Day 14 r08).
            return ContextLengthExceededError(str(exc))
        # Unknown 400: don't guess whose fault it is — neither "fix your input"
        # nor "we are misconfigured" is provable. Log it, report upstream failure.
        return UpstreamServiceError(str(exc))
    return UpstreamServiceError(str(exc))


def _map_incomplete_reason(reason: str | None) -> IncompleteReason:
    if reason == "max_output_tokens":
        return "max_output_tokens"
    if reason == "content_filter":
        return "content_filter"
    return "other"


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
                    model_version=event.response.model,
                )
                return
            elif event.type == "response.incomplete":
                details = event.response.incomplete_details
                mapped = _map_incomplete_reason(details.reason if details else None)
                # Incomplete still consumed tokens: the meter ran to the cutoff.
                usage = _extract_usage(event.response.usage)
                _log_llm_usage(usage)
                yield StreamDone(
                    status="incomplete",
                    incomplete_reason=mapped,
                    replay_items=_dump_output_items(event.response.output),
                    usage=usage,
                    model_version=event.response.model,
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


async def _no_op_aclose() -> None:
    """Default for direct construction (this file's own stub-client tests):
    build_chat_service() always supplies the resolver's real ``aclose``
    instead."""
    return None


class AzureOpenAIChatService:
    def __init__(
        self,
        client: AsyncOpenAI,
        deployment_name: str,
        prompt: PromptTemplate,
        max_output_tokens: int,
        credential_aclose: Callable[[], Coroutine[Any, Any, None]] = _no_op_aclose,
    ) -> None:
        self._client = client
        self._deployment_name = deployment_name
        self._prompt = prompt
        self._max_output_tokens = max_output_tokens
        # build_chat_service() constructs this AsyncOpenAI and hands it in
        # here: ownership transfers to this adapter, so it — not the caller —
        # is responsible for closing it. Guarded so a double aclose() (e.g.
        # both direct and via a composing service) is safe.
        self._closed = False
        # entra mode only (Day 24): closes the ManagedIdentityCredential
        # backing `client`'s callable api_key. A no-op in api_key mode. It
        # owns its own aiohttp session and leaks a ResourceWarning at
        # teardown if never closed — see azure_openai_auth.py's docstring.
        self._credential_aclose = credential_aclose

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
        # Non-streaming responses carry the same terminal states as streams;
        # ignoring response.status here would disguise a truncated reply as
        # normal success (Day 9 review r01 finding 2).
        if response.status == "failed":
            error = response.error
            detail = f"{error.code}: {error.message}" if error else "response failed"
            raise _translate_failed_event(error.code if error else None, detail)
        usage = _extract_usage(response.usage)
        _log_llm_usage(usage)
        if response.status == "incomplete":
            details = response.incomplete_details
            return ChatResult(
                message=response.output_text,
                model_version=response.model,
                replay_items=_dump_output_items(response.output),
                usage=usage,
                status="incomplete",
                incomplete_reason=_map_incomplete_reason(details.reason if details else None),
            )
        return ChatResult(
            message=response.output_text,
            model_version=response.model,
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

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A credential-close failure must not strand the client, and vice
        # versa (same isolation discipline as the rest of this app's closers,
        # Day 14 review finding 4). No None-check needed: `_credential_aclose`
        # is always a valid awaitable, a no-op in api_key mode.
        try:
            await self._client.close()
        finally:
            await self._credential_aclose()


async def _traced_stream(
    events: AsyncIterator[ChatStreamEvent], owned: OwnedLlmSpan
) -> AsyncGenerator[ChatStreamEvent]:
    """Carry the owned span across the body iteration and close it on any exit.

    `finally` rather than a happy-path close: consumer close, task
    cancellation, upstream EOF and an exception all have to end the span, and
    only one of those is the normal one.
    """
    try:
        async for event in events:
            owned.record_first_chunk()
            if isinstance(event, StreamDone):
                # The terminal carries what the span most needs: provider-
                # reported usage, and whether the model actually finished.
                owned.record_terminal(
                    event.usage,
                    OUTCOME_SUCCESS,
                )
            yield event
    finally:
        # No terminal seen means the stream ended without one -- EOF, a
        # disconnect before the model finished, or a cancellation. aclose()
        # records that as an error rather than inferring success from silence.
        await owned.aclose()


class _TracedStream:
    """An async iterator, not a bare generator, so the span closes even if the
    stream is never iterated.

    Measured rather than assumed: an async generator that has never been
    started does not run its ``finally`` on ``aclose()`` -- there is no
    suspended frame to throw ``GeneratorExit`` into -- so a generator-only
    design leaks the span in exactly the case where nothing ever consumed the
    stream. Two tests pin that: one closes before the first iteration, one
    never iterates at all.

    Both closes are called on the way out. ``OwnedLlmSpan.aclose`` is
    idempotent precisely so this is safe: whichever path got there first keeps
    its recorded outcome.
    """

    def __init__(self, events: AsyncIterator[ChatStreamEvent], owned: OwnedLlmSpan) -> None:
        self._inner: AsyncGenerator[ChatStreamEvent] = _traced_stream(events, owned)
        self._owned = owned

    def __aiter__(self) -> AsyncIterator[ChatStreamEvent]:
        return self

    async def __anext__(self) -> ChatStreamEvent:
        return await self._inner.__anext__()

    async def aclose(self) -> None:
        await self._inner.aclose()
        # Idempotent, and the only close that runs when the generator above
        # was never started.
        await self._owned.aclose()


class TracingChatService:
    """Wraps any ``ChatService`` in one semantic span per model call.

    A decorator at the composition point rather than code inside each adapter,
    which is the decision Day 27 had to make and is worth recording. The
    adapters are not the only implementations of this protocol: the test suite
    substitutes the same narrow ``complete``/``open_stream``/``aclose`` shape
    (``tests/unit/audit_helpers.py``'s RaisingChatService, and others), and an
    adapter-internal span would leave every one of those paths -- which is to
    say every failure path under test -- producing no span at all. Wrapping the
    protocol covers real, fake and stand-in alike.

    The deployment name is fixed at construction because a span has to be named
    before the call it measures, so it cannot be read off the result. For fake
    adapters that name is the ``"fake"`` sentinel, matching the ``model_version``
    the fake already reports and the precedent Day 22 set with
    ``provider_call_attempted``: the field records that the adapter boundary was
    reached, not that Azure was.

    ``open_stream`` deliberately passes straight through here. A streaming call's
    span outlives the method that starts it -- it has to stay open across the
    body iteration -- so it needs an owner rather than a context manager, which
    is a separate piece of machinery.
    """

    def __init__(self, inner: ChatService, deployment: str) -> None:
        self._inner = inner
        self._deployment = deployment

    @property
    def inner(self) -> ChatService:
        """The wrapped adapter. Exposed so composition tests can still say
        which adapter was selected rather than only that something was."""
        return self._inner

    async def complete(self, items: Sequence[ReplayItem]) -> ChatResult:
        with llm_span(self._deployment) as span:
            try:
                result = await self._inner.complete(items)
            except UpstreamError as exc:
                # upstream_outcome, not a hardcoded "error": a 4xx is this
                # caller's request being rejected and a 5xx is a failure that
                # was not their fault. Day 22's audit outcome field already
                # makes that split, and a second copy of the rule here would
                # be a second thing to keep in sync.
                set_outcome(span, upstream_outcome(exc), exc.code)
                raise
            set_usage_attributes(span, result.usage)
            span.set_attribute(
                ATTR_FINISH_REASONS,
                # Day 6's incomplete_reason is the same fact under this repo's
                # own name; the convention wants a list.
                [result.incomplete_reason or result.status],
            )
            set_outcome(span, OUTCOME_SUCCESS)
            return result

    async def open_stream(self, items: Sequence[ReplayItem]) -> AsyncIterator[ChatStreamEvent]:
        """Open the stream under a span this method does not close.

        `async def` to match the protocol: Day 6 made this an eager await so a
        pre-stream failure raises before the StreamingResponse exists. That
        two-phase boundary is why the span is closed in two places here --
        before the iterator exists, this method owns the failure; after it,
        the wrapper generator does.

        The owner stays inside this decorator rather than being returned
        alongside the iterator. Threading it out would change the protocol, the
        conversation service, the endpoint, and every test double implementing
        the same shape -- for a value only this layer ever reads.
        """
        owned = start_owned_llm_span(self._deployment)
        try:
            events = await self._inner.open_stream(items)
        except UpstreamError as exc:
            # Pre-stream: no byte has reached the client, so this is still an
            # ordinary HTTP failure and no StreamingResponse will ever exist
            # to close the span for us.
            owned.record_terminal(None, upstream_outcome(exc), exc.code)
            await owned.aclose()
            raise
        except BaseException:
            owned.record_terminal(None, OUTCOME_ERROR, "upstream_error")
            await owned.aclose()
            raise
        return _TracedStream(events, owned)



    async def aclose(self) -> None:
        await self._inner.aclose()


def build_chat_service(settings: Settings, *, prompt: PromptTemplate) -> ChatService:
    """Composition point: fake vs. real. The prompt instance is loaded once by
    the caller (fail-fast on a malformed template, same as before) and shared
    with the audit attribution built from the same instance (Day 22) — "same
    file, same sha256" is not "same object", and the attribution must
    describe the prompt this adapter actually holds.
    """
    if settings.use_fake_llm:
        return TracingChatService(FakeChatService(prompt=prompt), FAKE_DEPLOYMENT)
    if not (settings.azure_openai_endpoint and settings.azure_openai_deployment_name):
        raise ValueError(
            "USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT_NAME"
        )
    auth = resolve_aoai_auth(settings)  # raises ValueError per mode (Day 24 keyless)
    client = AsyncOpenAI(
        api_key=auth.api_key,  # str | async Callable — matches AsyncOpenAI's own type
        base_url=settings.azure_openai_endpoint.rstrip("/") + "/openai/v1/",
        timeout=settings.llm_timeout_seconds,  # per attempt (default 30s), not end-to-end
        max_retries=settings.llm_max_retries,  # explicit policy; the SDK default is 2
        # The SDK would otherwise build its own httpx client, which nothing
        # instruments: the distro bundles no httpx instrumentation, so this is
        # the only way the upstream call appears as a dependency at all.
        http_client=instrumented_httpx_client(timeout=settings.llm_timeout_seconds),
    )
    return TracingChatService(
        AzureOpenAIChatService(
            client,
            settings.azure_openai_deployment_name,
            prompt,
            max_output_tokens=settings.llm_max_output_tokens,
            credential_aclose=auth.aclose,
        ),
        settings.azure_openai_deployment_name,
    )
