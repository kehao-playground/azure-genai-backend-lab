import hashlib
import inspect
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.core.errors import (
    ConfigurationError,
    ContentFilteredError,
    ContextLengthExceededError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts.loader import PromptTemplate, load_prompt
from azgenai_lab.services.azure_openai import (
    AzureOpenAIChatService,
    FakeChatService,
    StreamDone,
    _fake_output_item,
    build_chat_service,
)

_PROMPT_TEXT = "You are T."
PROMPT = PromptTemplate(
    name="default_chat",
    version=1,
    description="d",
    text=_PROMPT_TEXT,
    sha256=hashlib.sha256(_PROMPT_TEXT.encode("utf-8")).hexdigest(),
)


def user_items(*texts: str) -> list[ReplayItem]:
    return [{"role": "user", "content": text} for text in texts]


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_settings_build_fake_service() -> None:
    service = build_chat_service(Settings(_env_file=None), prompt=PROMPT)

    assert isinstance(service, FakeChatService)


async def test_fake_service_never_calls_azure() -> None:
    result = await FakeChatService().complete(user_items("hello"))

    assert result.message == "[fake-llm] hello"
    assert result.model_version == "fake"
    assert result.replay_items == (_fake_output_item("[fake-llm] hello"),)


async def test_fake_stream_done_carries_model_version() -> None:
    service = FakeChatService()
    events = [e async for e in await service.open_stream([{"role": "user", "content": "hi"}])]
    done = events[-1]
    assert isinstance(done, StreamDone)
    assert done.model_version == "fake"


async def test_fake_service_makes_received_history_visible() -> None:
    result = await FakeChatService().complete(user_items("one", "two", "three"))

    assert result.message == "[fake-llm] three (history=2)"


def test_real_service_requires_endpoint_and_deployment() -> None:
    # The API-key requirement moved into resolve_aoai_auth (Task 2); this
    # pre-check now only owns endpoint + deployment, so the message must not
    # mention AZURE_OPENAI_API_KEY.
    settings = Settings(_env_file=None, use_fake_llm=False)

    with pytest.raises(
        ValueError,
        match="USE_FAKE_LLM=false requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT_NAME",
    ):
        build_chat_service(settings, prompt=PROMPT)


def test_real_service_endpoint_and_deployment_present_still_requires_a_key() -> None:
    # Endpoint + deployment satisfy the pre-check; the missing-key error now
    # comes from the resolver, not this module's own check.
    settings = Settings(
        _env_file=None,
        use_fake_llm=False,
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_deployment_name="chat-mini",
    )

    with pytest.raises(ValueError, match="AZURE_OPENAI_AUTH=api_key requires AZURE_OPENAI_API_KEY"):
        build_chat_service(settings, prompt=PROMPT)


async def test_real_service_api_key_mode_aclose_is_a_safe_no_op_and_still_closes_client() -> None:
    # api_key mode never mints a credential; aclose() must still close the
    # AsyncOpenAI client, and awaiting the resolver's no-op closer must not
    # raise.
    service = build_chat_service(make_real_settings(), prompt=PROMPT)
    assert isinstance(service, AzureOpenAIChatService)

    await service.aclose()

    assert service._client.is_closed()


class _CloseTrackingCredential:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def _patch_entra_credential(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    created: dict[str, object] = {}

    def fake_credential_ctor(client_id: str) -> _CloseTrackingCredential:
        credential = _CloseTrackingCredential(client_id)
        created["credential"] = credential
        return credential

    def fake_provider(credential: object, scope: str) -> Callable[[], Awaitable[str]]:
        created["scope"] = scope

        async def token_callable() -> str:
            return "tok"

        return token_callable

    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.ManagedIdentityCredential",
        fake_credential_ctor,
    )
    monkeypatch.setattr(
        "azgenai_lab.services.azure_openai_auth.get_bearer_token_provider", fake_provider
    )
    return created


def test_real_service_builds_in_entra_mode_with_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _patch_entra_credential(monkeypatch)
    settings = Settings(
        _env_file=None,
        use_fake_llm=False,
        azure_openai_auth="entra",
        azure_client_id="cid",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_deployment_name="chat-mini",
    )

    service = build_chat_service(settings, prompt=PROMPT)

    assert isinstance(service, AzureOpenAIChatService)
    provider = service._client._api_key_provider
    assert provider is not None
    assert inspect.iscoroutinefunction(provider)
    assert created["scope"] == "https://cognitiveservices.azure.com/.default"
    assert isinstance(created["credential"], _CloseTrackingCredential)


async def test_real_service_entra_mode_aclose_closes_credential_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _patch_entra_credential(monkeypatch)
    settings = Settings(
        _env_file=None,
        use_fake_llm=False,
        azure_openai_auth="entra",
        azure_client_id="cid",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_deployment_name="chat-mini",
    )
    service = build_chat_service(settings, prompt=PROMPT)
    assert isinstance(service, AzureOpenAIChatService)
    credential = created["credential"]
    assert isinstance(credential, _CloseTrackingCredential)

    await service.aclose()

    assert credential.close_count == 1
    assert service._client.is_closed()

    await service.aclose()  # idempotent: no second close

    assert credential.close_count == 1


def make_real_settings() -> Settings:
    return Settings(
        _env_file=None,
        use_fake_llm=False,
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key=SecretStr("test-key"),
        azure_openai_deployment_name="chat-mini",
    )


def test_real_service_built_from_complete_settings() -> None:
    service = build_chat_service(make_real_settings(), prompt=PROMPT)

    assert isinstance(service, AzureOpenAIChatService)


def test_real_client_uses_configured_timeout_not_sdk_default() -> None:
    service = build_chat_service(make_real_settings(), prompt=PROMPT)

    assert isinstance(service, AzureOpenAIChatService)
    assert service._client.timeout == 30.0
    assert service._client.max_retries == 2


class StubOutputItem:
    """Mimics an SDK output item: only model_dump is used at the boundary."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self._payload)


REASONING_ITEM = {"type": "reasoning", "encrypted_content": "opaque-blob"}


class StubResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text="pong",
            model="gpt-5-mini-2025-08-07",
            output=[StubOutputItem(REASONING_ITEM)],
            usage=SimpleNamespace(input_tokens=12, output_tokens=3, total_tokens=15),
            status="completed",
            incomplete_details=None,
            error=None,
        )


def make_stub_client() -> tuple[AsyncOpenAI, StubResponses]:
    responses = StubResponses()
    client = SimpleNamespace(responses=responses)
    return cast(AsyncOpenAI, client), responses


async def test_real_service_sends_deployment_name_and_replay_items_verbatim() -> None:
    client, responses = make_stub_client()
    service = AzureOpenAIChatService(
        client, deployment_name="chat-mini", prompt=PROMPT, max_output_tokens=1000
    )

    replay_context = [
        {"role": "user", "content": "hello"},
        REASONING_ITEM,
        {"role": "user", "content": "again"},
    ]
    result = await service.complete(replay_context)

    assert responses.calls[0]["model"] == "chat-mini"
    assert responses.calls[0]["input"] == replay_context
    assert result.message == "pong"
    assert result.model_version == "gpt-5-mini-2025-08-07"
    # The response's output items come back as the next turn's replay context.
    assert result.replay_items == (REASONING_ITEM,)


async def test_real_service_never_stores_and_requests_encrypted_reasoning() -> None:
    client, responses = make_stub_client()
    service = AzureOpenAIChatService(
        client, deployment_name="chat-mini", prompt=PROMPT, max_output_tokens=1000
    )

    await service.complete(user_items("hello"))

    assert responses.calls[0]["store"] is False
    # store=False + reasoning model: without this include, replayed history
    # silently loses reasoning context (review r01 finding 1).
    assert responses.calls[0]["include"] == ["reasoning.encrypted_content"]
    # Day 9: the per-call output cap travels on every request.
    assert responses.calls[0]["max_output_tokens"] == 1000


async def test_real_service_reports_provider_usage() -> None:
    client, _ = make_stub_client()
    service = AzureOpenAIChatService(
        client, deployment_name="chat-mini", prompt=PROMPT, max_output_tokens=1000
    )

    result = await service.complete(user_items("hello"))

    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (
        12,
        3,
        15,
    )


def make_status_error(
    error_cls: type[openai.APIStatusError], status_code: int, code: str | None = None
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    response = httpx.Response(status_code, request=request)
    body = {"code": code} if code else None
    return error_cls("upstream detail", response=response, body=body)


TIMEOUT_REQUEST = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")


@pytest.mark.parametrize(
    ("sdk_error", "expected"),
    [
        (openai.APITimeoutError(request=TIMEOUT_REQUEST), UpstreamTimeoutError),
        (make_status_error(openai.RateLimitError, 429), UpstreamThrottledError),
        (make_status_error(openai.AuthenticationError, 401), ConfigurationError),
        (make_status_error(openai.PermissionDeniedError, 403), ConfigurationError),
        (make_status_error(openai.NotFoundError, 404), ConfigurationError),
        (
            make_status_error(openai.BadRequestError, 400, code="content_filter"),
            ContentFilteredError,
        ),
        (
            make_status_error(openai.BadRequestError, 400, code="context_length_exceeded"),
            ContextLengthExceededError,
        ),
        (make_status_error(openai.BadRequestError, 400), UpstreamServiceError),
        (make_status_error(openai.InternalServerError, 500), UpstreamServiceError),
        (
            openai.APIConnectionError(message="boom", request=TIMEOUT_REQUEST),
            UpstreamServiceError,
        ),
    ],
)
async def test_sdk_errors_are_translated_at_the_adapter_boundary(
    sdk_error: openai.OpenAIError, expected: type[UpstreamError]
) -> None:
    client, responses = make_stub_client()

    async def raise_sdk_error(**kwargs: Any) -> Any:
        raise sdk_error

    responses.create = raise_sdk_error  # type: ignore[method-assign]
    service = AzureOpenAIChatService(
        client, deployment_name="chat-mini", prompt=PROMPT, max_output_tokens=1000
    )

    with pytest.raises(expected) as excinfo:
        await service.complete(user_items("hello"))

    assert excinfo.value.upstream_detail  # original text kept for the log, not the client
    assert excinfo.value.__cause__ is sdk_error


async def test_fake_marks_prompt_delivery() -> None:
    service = FakeChatService(prompt=PROMPT)
    result = await service.complete([{"role": "user", "content": "ping"}])
    assert "(prompt=default_chat@1)" in result.message


async def test_fake_without_prompt_keeps_legacy_output() -> None:
    service = FakeChatService()
    result = await service.complete([{"role": "user", "content": "ping"}])
    assert result.message == "[fake-llm] ping"


async def test_build_chat_service_wires_prompt() -> None:
    service = build_chat_service(make_settings(use_fake_llm=True), prompt=PROMPT)
    result = await service.complete([{"role": "user", "content": "ping"}])
    assert f"(prompt={PROMPT.name}@{PROMPT.version})" in result.message


async def test_real_service_sends_prompt_as_instructions() -> None:
    client, responses = make_stub_client()
    service = AzureOpenAIChatService(
        client, deployment_name="chat-mini", prompt=PROMPT, max_output_tokens=1000
    )

    await service.complete(user_items("hello"))

    assert responses.calls[0]["instructions"] == PROMPT.text


async def test_build_chat_service_wires_whichever_prompt_instance_the_caller_loaded() -> None:
    # build_chat_service no longer decides which template to load (Day 22):
    # the caller (build_conversation_service / build_rag_service) loads once
    # and hands the instance in — this proves the adapter carries whatever
    # instance it was given, not a name it resolved itself.
    rag_prompt = load_prompt("rag_answer")
    rag_service = build_chat_service(make_settings(use_fake_llm=True), prompt=rag_prompt)
    result = await rag_service.complete([{"role": "user", "content": "q"}])
    assert f"prompt=rag_answer@{rag_prompt.version}" in result.message
