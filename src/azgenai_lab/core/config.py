from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
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
    # gt=0: zero or negative caps are deployment mistakes — fail at startup,
    # not as a confusing upstream 400 (Day 9 review r01 finding 6).
    llm_max_output_tokens: int = Field(default=1000, gt=0)
    # Per-conversation lifetime budget in provider-reported tokens (input +
    # output across all committed turns). Checked before inference: an
    # exhausted conversation is rejected with 429 token_budget_exceeded
    # without touching the upstream. None is the only way to disable the
    # guardrail; zero or negative values fail startup validation.
    conversation_token_budget: int | None = Field(default=50_000, gt=0)

    # Chunk sizing is measured in characters, not tokens. Day 9 settled the
    # series position: meter what the provider reports, do not estimate. The
    # service's own generally-available text splitter measures characters too
    # (token splitting is preview only), and the real safety margin is the
    # embedding model's 8,192-token input ceiling, which 2,000 characters sits
    # well under in any language.
    chunk_max_chars: int = Field(default=2000, gt=0)
    # Overlap applies only within one oversized section (see services/chunking).
    chunk_overlap_chars: int = Field(default=500, ge=0)

    azure_openai_embedding_deployment: str | None = None

    azure_search_endpoint: str | None = None
    # Admin key: creating an index, uploading and deleting documents all need
    # management capability. Production should use Microsoft Entra ID / RBAC
    # with roles split by read/write responsibility; the key keeps the lab's
    # ephemeral sessions cheap to configure. Never logged, never returned.
    azure_search_admin_key: SecretStr | None = None

    use_fake_llm: bool = Field(default=True)
    use_fake_search: bool = Field(default=True)
    use_fake_embeddings: bool = Field(default=True)

    # How many chunks retrieval hands to generation. Small on purpose: every
    # hit is prompt input the model must read and the caller pays for
    # (Day 9 metering); top=5 of 2,000-char chunks is ~10k chars of context.
    # Recall/precision tuning belongs to retrieval evaluation, not this knob.
    # Upper bound (Day 14 review finding 2): DEFAULT_VECTOR_K=50 is the
    # vector leg's candidate pool (models/search.py), so top above 50 asks
    # generation to read more chunks than retrieval's own vector leg ever
    # offers. It also bounds assembled context to <= 50 * 2,000 chars =
    # ~100k chars plus the question, keeping context growth predictable
    # instead of an unbounded multiple of chunk_max_chars.
    rag_top: int = Field(default=5, gt=0, le=50)

    @model_validator(mode="after")
    def _overlap_must_leave_room_to_advance(self) -> "Settings":
        if self.chunk_overlap_chars * 2 >= self.chunk_max_chars:
            raise ValueError(
                "chunk_overlap_chars must be less than half of chunk_max_chars "
                f"(got {self.chunk_overlap_chars} against {self.chunk_max_chars})"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
