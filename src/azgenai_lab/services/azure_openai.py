from dataclasses import dataclass


@dataclass(frozen=True)
class ChatResult:
    message: str
    model: str | None = None


class AzureOpenAIChatService:
    """Azure OpenAI adapter placeholder.

    The real implementation will be introduced in the Azure OpenAI article.
    """

    async def complete(self, message: str) -> ChatResult:
        return ChatResult(message=f"[fake-llm] {message}", model="fake")
