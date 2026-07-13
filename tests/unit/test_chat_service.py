import pytest
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
