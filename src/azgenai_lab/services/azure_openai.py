"""Azure OpenAI chat adapter (v1 GA API).

Uses the plain ``openai.AsyncOpenAI`` client against ``<endpoint>/openai/v1/`` —
no ``api-version`` and no Azure-specific client since the v1 GA API (2025-08).
The ``model`` argument is the *deployment name*, not the model name.

Fake vs. real is selected once in :func:`build_chat_service`; handlers depend
only on the :class:`ChatService` protocol.
"""

from dataclasses import dataclass
from typing import Protocol

from openai import AsyncOpenAI

from azgenai_lab.core.config import Settings


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


class AzureOpenAIChatService:
    def __init__(self, client: AsyncOpenAI, deployment_name: str) -> None:
        self._client = client
        self._deployment_name = deployment_name

    async def complete(self, message: str) -> ChatResult:
        response = await self._client.chat.completions.create(
            model=self._deployment_name,
            messages=[{"role": "user", "content": message}],
        )
        return ChatResult(
            message=response.choices[0].message.content or "",
            model=response.model,
        )


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
    )
    return AzureOpenAIChatService(client, settings.azure_openai_deployment_name)
