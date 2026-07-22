from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "azure-genai-backend-lab"
    app_env: str = "local"
    log_level: str = "INFO"

    # v1 GA API (2025-08): plain OpenAI client against <endpoint>/openai/v1/, no api-version
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_deployment_name: str | None = None

    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Hard cap per call, passed as max_output_tokens on every request: an
    # unbounded reply is the single fastest way to burn budget. Streams that
    # hit it end with message.done incomplete/max_output_tokens (Day 6).
    llm_max_output_tokens: int = 1000
    # Per-conversation lifetime budget in billed tokens (input + output across
    # all committed turns). Checked before inference: an exhausted conversation
    # is rejected with 429 token_budget_exceeded without touching the upstream.
    # None disables the guardrail.
    conversation_token_budget: int | None = 50_000

    azure_search_endpoint: str | None = None
    azure_search_index_name: str | None = None

    use_fake_llm: bool = Field(default=True)
    use_fake_search: bool = Field(default=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
