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

    azure_search_endpoint: str | None = None
    azure_search_index_name: str | None = None

    use_fake_llm: bool = Field(default=True)
    use_fake_search: bool = Field(default=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
