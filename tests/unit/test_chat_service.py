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
    InvalidInputError,
    UpstreamError,
    UpstreamServiceError,
    UpstreamThrottledError,
    UpstreamTimeoutError,
)
from azgenai_lab.models.conversation import ReplayItem
from azgenai_lab.prompts.loader import PromptTemplate
from azgenai_lab.services.azure_openai import (
    AzureOpenAIChatService,
    FakeChatService,
    _fake_output_item,
    build_chat_service,
)

PROMPT = PromptTemplate(name="default_chat", version=1, description="d", text="You are T.")


def user_items(*texts: str) -> list[ReplayItem]:
    return [{"role": "user", "content": text} for text in texts]


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_settings_build_fake_service() -> None:
    service = build_chat_service(Settings(_env_file=None))

    assert isinstance(service, FakeChatService)


async def test_fake_service_never_calls_azure() -> None:
    result = await FakeChatService().complete(user_items("hello"))

    assert result.message == "[fake-llm] hello"
    assert result.model == "fake"
    assert result.replay_items == (_fake_output_item("[fake-llm] hello"),)


async def test_fake_service_makes_received_history_visible() -> None:
    result = await FakeChatService().complete(user_items("one", "two", "three"))

    assert result.message == "[fake-llm] three (history=2)"


def test_real_service_requires_endpoint_key_and_deployment() -> None:
    settings = Settings(_env_file=None, use_fake_llm=False)

    with pytest.raises(ValueError, match="USE_FAKE_LLM=false requires"):
        build_chat_service(settings)


def make_real_settings() -> Settings:
    return Settings(
        _env_file=None,
        use_fake_llm=False,
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key=SecretStr("test-key"),
        azure_openai_deployment_name="chat-mini",
    )


def test_real_service_built_from_complete_settings() -> None:
    service = build_chat_service(make_real_settings())

    assert isinstance(service, AzureOpenAIChatService)


def test_real_client_uses_configured_timeout_not_sdk_default() -> None:
    service = build_chat_service(make_real_settings())

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
        )


def make_stub_client() -> tuple[AsyncOpenAI, StubResponses]:
    responses = StubResponses()
    client = SimpleNamespace(responses=responses)
    return cast(AsyncOpenAI, client), responses


async def test_real_service_sends_deployment_name_and_replay_items_verbatim() -> None:
    client, responses = make_stub_client()
    service = AzureOpenAIChatService(client, deployment_name="chat-mini", prompt=PROMPT)

    replay_context = [
        {"role": "user", "content": "hello"},
        REASONING_ITEM,
        {"role": "user", "content": "again"},
    ]
    result = await service.complete(replay_context)

    assert responses.calls[0]["model"] == "chat-mini"
    assert responses.calls[0]["input"] == replay_context
    assert result.message == "pong"
    assert result.model == "gpt-5-mini-2025-08-07"
    # The response's output items come back as the next turn's replay context.
    assert result.replay_items == (REASONING_ITEM,)


async def test_real_service_never_stores_and_requests_encrypted_reasoning() -> None:
    client, responses = make_stub_client()
    service = AzureOpenAIChatService(client, deployment_name="chat-mini", prompt=PROMPT)

    await service.complete(user_items("hello"))

    assert responses.calls[0]["store"] is False
    # store=False + reasoning model: without this include, replayed history
    # silently loses reasoning context (review r01 finding 1).
    assert responses.calls[0]["include"] == ["reasoning.encrypted_content"]


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
            InvalidInputError,
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
    service = AzureOpenAIChatService(client, deployment_name="chat-mini", prompt=PROMPT)

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
    service = build_chat_service(make_settings(use_fake_llm=True))
    result = await service.complete([{"role": "user", "content": "ping"}])
    assert "(prompt=default_chat@1)" in result.message


async def test_real_service_sends_prompt_as_instructions() -> None:
    client, responses = make_stub_client()
    service = AzureOpenAIChatService(client, deployment_name="chat-mini", prompt=PROMPT)

    await service.complete(user_items("hello"))

    assert responses.calls[0]["instructions"] == PROMPT.text
