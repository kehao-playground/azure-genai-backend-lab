from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.services.azure_openai import (
    AzureOpenAIChatService,
    FakeChatService,
    build_chat_service,
)


def test_default_settings_build_fake_service() -> None:
    service = build_chat_service(Settings(_env_file=None))

    assert isinstance(service, FakeChatService)


async def test_fake_service_never_calls_azure() -> None:
    result = await FakeChatService().complete("hello")

    assert result.message == "[fake-llm] hello"
    assert result.model == "fake"


def test_real_service_requires_endpoint_key_and_deployment() -> None:
    settings = Settings(_env_file=None, use_fake_llm=False)

    with pytest.raises(ValueError, match="USE_FAKE_LLM=false requires"):
        build_chat_service(settings)


def test_real_service_built_from_complete_settings() -> None:
    settings = Settings(
        _env_file=None,
        use_fake_llm=False,
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key=SecretStr("test-key"),
        azure_openai_deployment_name="chat-mini",
    )

    service = build_chat_service(settings)

    assert isinstance(service, AzureOpenAIChatService)


class StubCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))],
            model="gpt-5-mini-2025-08-07",
        )


def make_stub_client() -> tuple[AsyncOpenAI, StubCompletions]:
    completions = StubCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return cast(AsyncOpenAI, client), completions


async def test_real_service_sends_deployment_name_as_model() -> None:
    client, completions = make_stub_client()
    service = AzureOpenAIChatService(client, deployment_name="chat-mini")

    result = await service.complete("hello")

    assert completions.calls[0]["model"] == "chat-mini"
    assert completions.calls[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert result.message == "pong"
    assert result.model == "gpt-5-mini-2025-08-07"


async def test_real_service_maps_empty_content_to_empty_string() -> None:
    client, completions = make_stub_client()

    async def create_empty(**kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
            model="gpt-5-mini-2025-08-07",
        )

    completions.create = create_empty  # type: ignore[method-assign]
    service = AzureOpenAIChatService(client, deployment_name="chat-mini")

    result = await service.complete("hello")

    assert result.message == ""
